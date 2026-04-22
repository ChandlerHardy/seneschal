"""Tests for app.py concurrency primitives and put_file dead-letter path.

B1: `_per_pr_lock` must serialize threads within the same Python process.
    Linux `flock(2)` is per-open-file-description, so threading.Lock must
    layer above fcntl to catch same-process races.

B2: `put_file` on attempt==2 with 409 must raise RuntimeError (so the
    orchestrator's `_attempt_changelog_commit` classifies it as
    `"conflict"` and dead-letters the changelog entry), not HTTPError.
"""

import os
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# App.py needs ANTHROPIC_API_KEY at import time; set a placeholder.
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import app  # noqa: E402


# --------------------------------------------------------------------------
# B1: _per_pr_lock must serialize concurrent threads in the same process.
# --------------------------------------------------------------------------


def test_per_pr_lock_serializes_same_process_threads(tmp_path, monkeypatch):
    """Two threads entering `_per_pr_lock` for the same (owner, repo, pr)
    must NOT run the critical section concurrently. Without the
    threading.Lock layer, Linux `flock` is per-fd so both acquire LOCK_EX
    simultaneously and the concurrency guarantee is broken.

    Simulates the race by spawning 2 threads, each of which enters the
    lock and sleeps briefly while incrementing a shared counter. If the
    lock is broken the counter's max observed value will be 2; if the
    lock holds, it stays at 1.
    """
    monkeypatch.setattr(app, "_PER_PR_LOCK_DIR", str(tmp_path / "locks"))

    concurrent = {"current": 0, "max": 0}
    guard = threading.Lock()

    def _worker():
        with app._per_pr_lock("o", "r", 42):
            with guard:
                concurrent["current"] += 1
                if concurrent["current"] > concurrent["max"]:
                    concurrent["max"] = concurrent["current"]
            # Force the race window wide — without the lock, the other
            # thread has ~50ms to enter before we decrement.
            time.sleep(0.05)
            with guard:
                concurrent["current"] -= 1

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert concurrent["max"] == 1, (
        f"Lock allowed {concurrent['max']} threads inside the critical "
        "section simultaneously — the threading.Lock layer above fcntl "
        "is missing or broken."
    )


def test_per_pr_lock_different_prs_do_not_block_each_other(tmp_path, monkeypatch):
    """Lock keying: different PRs must not serialize against each other,
    otherwise one slow post-merge step stalls unrelated deliveries."""
    monkeypatch.setattr(app, "_PER_PR_LOCK_DIR", str(tmp_path / "locks"))

    start = time.time()

    def _hold(pr):
        with app._per_pr_lock("o", "r", pr):
            time.sleep(0.1)

    t1 = threading.Thread(target=_hold, args=(1,))
    t2 = threading.Thread(target=_hold, args=(2,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    elapsed = time.time() - start
    # If locks serialized (wrong behavior), elapsed >= 0.2s. If they
    # ran in parallel (correct), it's ~0.1s. Use 0.18s as a margin.
    assert elapsed < 0.18, (
        f"Different-PR locks appear to serialize against each other "
        f"(elapsed={elapsed:.3f}s, expected ~0.1s)"
    )


# --------------------------------------------------------------------------
# B2: put_file must raise RuntimeError on attempt==2 409 (not HTTPError).
# --------------------------------------------------------------------------


def test_put_file_raises_runtime_error_on_exhausted_409_retries(monkeypatch):
    """Previously the `if resp.status_code == 409 and attempt < 2` guard
    fell through on the third 409 to `resp.raise_for_status()`, raising
    HTTPError — which the orchestrator's `_attempt_changelog_commit`
    classified as `"error"` instead of `"conflict"`, so the dead-letter
    branch was unreachable. Fix: raise RuntimeError explicitly with a
    "retries" sentinel so the conflict path is actually traversable."""
    mock_resp = MagicMock()
    mock_resp.status_code = 409
    mock_resp.raise_for_status = MagicMock()

    mock_session = MagicMock()
    mock_session.put.return_value = mock_resp

    with patch.object(app, "_github_session", return_value=mock_session):
        with patch.object(app, "get_file_sha", return_value="newsha"):
            with pytest.raises(RuntimeError) as exc_info:
                app.put_file(
                    "o", "r", "CHANGELOG.md",
                    content="body",
                    message="msg",
                    branch="main",
                    sha="oldsha",
                    token="tok",
                )

    # The error must carry something the orchestrator's classifier
    # ("retries" / "gave up") can match against — otherwise it defaults
    # to "error" and doesn't dead-letter.
    assert "retries" in str(exc_info.value).lower()


def test_put_file_on_403_raises_push_protected_not_runtime():
    """A 403 on branch-protected main must raise PushProtectedError so
    the orchestrator can fall back to auto-PR mode. This behavior must
    NOT regress when we tighten the 409 path."""
    mock_resp = MagicMock()
    mock_resp.status_code = 403

    mock_session = MagicMock()
    mock_session.put.return_value = mock_resp

    with patch.object(app, "_github_session", return_value=mock_session):
        with pytest.raises(app.PushProtectedError):
            app.put_file(
                "o", "r", "CHANGELOG.md",
                content="body",
                message="msg",
                branch="main",
                sha="oldsha",
                token="tok",
            )


def test_put_file_retries_409_once_then_succeeds(monkeypatch):
    """A single 409 should re-fetch the sha and retry, not give up
    immediately. This guards against regressing the happy retry path
    when we reshape the terminal 409 handling."""
    ok_resp = MagicMock()
    ok_resp.status_code = 201
    ok_resp.json.return_value = {"commit": {"sha": "newcommit"}}

    conflict_resp = MagicMock()
    conflict_resp.status_code = 409

    mock_session = MagicMock()
    # First call: 409. Second call: 201 OK.
    mock_session.put.side_effect = [conflict_resp, ok_resp]

    with patch.object(app, "_github_session", return_value=mock_session):
        with patch.object(app, "get_file_sha", return_value="freshsha") as mock_sha:
            result = app.put_file(
                "o", "r", "CHANGELOG.md",
                content="body",
                message="msg",
                branch="main",
                sha="oldsha",
                token="tok",
            )

    assert result == {"commit": {"sha": "newcommit"}}
    # Sha was re-fetched between the 409 and the retry.
    assert mock_sha.called
    assert mock_session.put.call_count == 2


# --------------------------------------------------------------------------
# W4: The webhook handler for a merge event must return fast (after sig
# verification + payload parse), with the slow repo-sync + config-load
# work happening inside the background thread. A cold clone taking >10s
# must NOT block the handler — GitHub retries on delivery timeout and
# stacks Flask workers on the same PR.
# --------------------------------------------------------------------------


def test_merge_webhook_does_not_sync_synchronously(monkeypatch):
    """Regression guard for W4. The handler code path for
    `pull_request/closed` + `merged=True` used to call
    `get_installation_token`, `ensure_repo_synced`, and `load_from_repo`
    INLINE before queuing the thread. That pulled the cold-clone latency
    onto the critical path. Now those calls happen inside the thread.

    Test: invoke `_handle_pull_request_event` with a merge payload. The
    `ensure_repo_synced` helper must NOT be called during the
    synchronous handler return — only inside the runner, which we
    verify fires asynchronously.
    """
    sync_calls = {"synchronous": False, "total": 0}

    def _fake_sync(*args, **kwargs):
        sync_calls["total"] += 1
        return "/tmp/fake-repo"

    def _fake_token(*args, **kwargs):
        return "tok"

    def _fake_load(*args, **kwargs):
        from repo_config import RepoConfig, PostMergeConfig
        return RepoConfig(post_merge=PostMergeConfig())

    # Patch a sentinel: during the handler call, flip a flag so we know
    # if the sync happened inline vs. later in the thread.
    import threading as _t
    handler_returning = _t.Event()

    def _sync_watcher(*args, **kwargs):
        if not handler_returning.is_set():
            sync_calls["synchronous"] = True
        return _fake_sync(*args, **kwargs)

    with patch.object(app, "ensure_repo_synced", side_effect=_sync_watcher):
        with patch.object(app, "get_installation_token", side_effect=_fake_token):
            with patch.object(app, "load_from_repo", side_effect=_fake_load):
                with patch("post_merge.orchestrator.handle_pr_merged") as mock_handler:
                    mock_handler.return_value = {}
                    payload = {
                        "action": "closed",
                        "pull_request": {
                            "number": 77,
                            "merged": True,
                            "base": {"ref": "main"},
                            "head": {"sha": "deadbeef"},
                            "merge_commit_sha": "feedface",
                        },
                        "repository": {
                            "owner": {"login": "o"},
                            "name": "r",
                        },
                        "installation": {"id": 1},
                    }
                    # Patch Flask's jsonify to return tuples directly so
                    # the test works outside an app context.
                    with patch.object(app, "jsonify", lambda d: d):
                        resp = app._handle_pull_request_event(payload)
                    handler_returning.set()

    # Handler returned before the thread's sync call (if any).
    assert sync_calls["synchronous"] is False, (
        "W4 regression: ensure_repo_synced was called synchronously "
        "inside the webhook handler. Long clones will block the "
        "handler and cause GitHub to retry + pile up workers."
    )
    # Response itself is a 200-ish "queued" dict.
    body = resp[0] if isinstance(resp, tuple) else resp
    assert body.get("status") == "post_merge_queued"
    assert body.get("pr") == 77


# --------------------------------------------------------------------------
# W6: post_review must thread head_sha into save_review so the review-store
# frontmatter gets populated (required by P2's SQLite indexing).
# --------------------------------------------------------------------------


def test_post_review_passes_head_sha_to_save_review(tmp_path, monkeypatch):
    """Previously post_review dropped head_sha entirely — save_review got
    only positional args so every persisted record had head_sha="".
    After W6, head_sha is a keyword arg on post_review and flows into
    the frontmatter."""
    import review_store
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))

    ok_resp = MagicMock()
    ok_resp.status_code = 200
    ok_resp.raise_for_status = MagicMock()
    ok_resp.content = b'{"html_url": "https://x/review/99"}'
    ok_resp.json.return_value = {"html_url": "https://x/review/99"}

    mock_session = MagicMock()
    mock_session.post.return_value = ok_resp

    with patch.object(app, "_github_session", return_value=mock_session):
        app.post_review(
            "o", "r", 7,
            body="APPROVE\n\nLGTM",
            token="tok",
            head_sha="feedc0ffee123",
        )

    rec = review_store.get_review("o/r", 7)
    assert rec is not None
    assert rec.head_sha == "feedc0ffee123"


# --------------------------------------------------------------------------
# W7: post_review's save_review call must happen under _per_pr_lock, so
# it's mutually exclusive with the orchestrator's mark_merged writes on
# the same PR.
# --------------------------------------------------------------------------


def test_post_review_holds_per_pr_lock_during_save(tmp_path, monkeypatch):
    """Regression guard: a push-event review racing a merge event's
    mark_merged could overwrite the frontmatter with a fresh record
    (no merged_at, no followups_filed). Wrapping save_review in the
    same per-PR lock the orchestrator uses makes the two writes
    mutually exclusive."""
    import review_store
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    monkeypatch.setattr(app, "_PER_PR_LOCK_DIR", str(tmp_path / "locks"))

    ok_resp = MagicMock()
    ok_resp.status_code = 200
    ok_resp.raise_for_status = MagicMock()
    ok_resp.content = b'{"html_url": "https://x/r/1"}'
    ok_resp.json.return_value = {"html_url": "https://x/r/1"}

    mock_session = MagicMock()
    mock_session.post.return_value = ok_resp

    # Track whether save_review is called while the lock is held.
    lock_held_during_save = {"value": False}

    # Acquire the same-key lock manually; the post_review call must
    # BLOCK on it. Use a background thread to call post_review, and
    # release the lock from the main thread after a short delay to
    # confirm it was actually blocked.
    acquired_by_test = threading.Event()
    release_test_lock = threading.Event()

    def _hold_outer_lock():
        with app._per_pr_lock("o", "r", 55):
            acquired_by_test.set()
            release_test_lock.wait(timeout=2.0)

    holder = threading.Thread(target=_hold_outer_lock, daemon=True)
    holder.start()
    acquired_by_test.wait(timeout=2.0)

    # post_review should now block trying to acquire the same lock.
    done = threading.Event()

    def _call():
        with patch.object(app, "_github_session", return_value=mock_session):
            app.post_review(
                "o", "r", 55,
                body="APPROVE",
                token="tok",
            )
        done.set()

    caller = threading.Thread(target=_call, daemon=True)
    caller.start()

    # Give caller a chance to block on the lock.
    time.sleep(0.05)
    # If save_review ran without waiting, done would be set already.
    assert not done.is_set(), (
        "post_review's save_review call did NOT wait for _per_pr_lock — "
        "W7 regression: save is not mutually exclusive with mark_merged."
    )

    # Release and let caller proceed.
    release_test_lock.set()
    done.wait(timeout=2.0)
    assert done.is_set(), "post_review never completed after lock release"

    # And the review did get saved.
    rec = review_store.get_review("o/r", 55)
    assert rec is not None
