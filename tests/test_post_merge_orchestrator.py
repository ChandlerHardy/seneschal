"""Tests for post_merge.orchestrator: glue between pure modules + GitHub I/O.

All GitHub I/O is mocked. The orchestrator is the integration point we
control end-to-end here; we test sequencing, idempotence, and fallback
modes (protected main, race-on-release-PR).
"""

import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import review_store  # noqa: E402
from post_merge.orchestrator import (  # noqa: E402
    PushProtectedError,
    handle_pr_merged,
    _PROTECTED_REPOS,
)
from repo_config import PostMergeConfig, RepoConfig  # noqa: E402
from review_store import save_review  # noqa: E402


def _config(**kw):
    pm = PostMergeConfig(**kw)
    return RepoConfig(post_merge=pm)


def _pr_meta(number=42, title="feat: add thing", merged_at="2026-04-21T10:00:00Z"):
    return {
        "number": number,
        "title": title,
        "merged_at": merged_at,
        "html_url": f"https://github.com/o/r/pull/{number}",
        "head": {"sha": "deadbeef"},
        "merge_commit_sha": "feedface",
    }


# --------------------------------------------------------------------------
# Happy path: changelog + followups + mark_merged
# --------------------------------------------------------------------------


def test_handle_pr_merged_updates_changelog(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review("o/r", 42, "APPROVE", "https://x/42", "review body without followups")

    cfg = _config(changelog=True)
    with patch("post_merge.orchestrator.app") as mock_app:
        mock_app.get_installation_token.return_value = "token123"
        mock_app.ensure_repo_synced.return_value = str(tmp_path / "clone")
        os.makedirs(tmp_path / "clone", exist_ok=True)
        # CHANGELOG.md already exists in the clone.
        (tmp_path / "clone" / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n"
        )
        # Mock the github file-API helpers.
        mock_app.put_file = MagicMock(return_value={"commit": {"sha": "abc"}})
        mock_app.get_file_sha = MagicMock(return_value="oldsha")

        result = handle_pr_merged(
            owner="o",
            repo="r",
            pr_number=42,
            installation_id=1,
            pr_meta=_pr_meta(42, "feat: add thing"),
            config=cfg,
        )

    assert result["changelog_updated"] is True
    assert mock_app.put_file.called
    # Confirm the put_file payload contained the new entry.
    call_kwargs = mock_app.put_file.call_args.kwargs
    posted_content = call_kwargs.get("content") or mock_app.put_file.call_args.args[3]
    assert "add thing" in posted_content


def test_handle_pr_merged_files_followups(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review(
        "o/r",
        42,
        "APPROVE",
        "https://x/42",
        "## Review\n\n- [FOLLOWUP] do the thing later\n- [FOLLOWUP] also that\n",
    )

    cfg = _config(followups=True)
    with patch("post_merge.orchestrator.app") as mock_app:
        mock_app.get_installation_token.return_value = "token123"
        mock_app.ensure_repo_synced.return_value = str(tmp_path / "clone")
        # Two issues created.
        mock_app.create_issue = MagicMock(side_effect=[
            {"number": 501}, {"number": 502}
        ])

        result = handle_pr_merged(
            owner="o",
            repo="r",
            pr_number=42,
            installation_id=1,
            pr_meta=_pr_meta(42),
            config=cfg,
        )

    assert sorted(result["followups_filed"]) == [501, 502]
    assert mock_app.create_issue.call_count == 2


def test_handle_pr_merged_marks_review_merged(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review("o/r", 42, "APPROVE", "https://x/42", "body")

    cfg = _config(followups=True)
    with patch("post_merge.orchestrator.app") as mock_app:
        mock_app.get_installation_token.return_value = "tok"
        mock_app.ensure_repo_synced.return_value = str(tmp_path / "clone")
        mock_app.create_issue = MagicMock(return_value={"number": 999})

        handle_pr_merged(
            owner="o",
            repo="r",
            pr_number=42,
            installation_id=1,
            pr_meta=_pr_meta(42, merged_at="2026-04-21T11:00:00Z"),
            config=cfg,
        )

    rec = review_store.get_review("o/r", 42)
    assert rec.merged_at == "2026-04-21T11:00:00Z"


# --------------------------------------------------------------------------
# Idempotence
# --------------------------------------------------------------------------


def test_handle_pr_merged_skips_already_filed_followups(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review(
        "o/r",
        42,
        "APPROVE",
        "https://x/42",
        "- [FOLLOWUP] do the thing later\n",
    )
    # Pre-mark this review as already having filed an issue — include the
    # sanitized title since the orchestrator now dedupes by title, not count.
    review_store.mark_merged(
        "o/r",
        42,
        "2026-04-21T10:00:00Z",
        [501],
        followup_titles=["[seneschal followup] do the thing later"],
    )

    cfg = _config(followups=True)
    with patch("post_merge.orchestrator.app") as mock_app:
        mock_app.get_installation_token.return_value = "tok"
        mock_app.ensure_repo_synced.return_value = str(tmp_path / "clone")
        mock_app.create_issue = MagicMock()

        result = handle_pr_merged(
            owner="o",
            repo="r",
            pr_number=42,
            installation_id=1,
            pr_meta=_pr_meta(42),
            config=cfg,
        )

    # No new issues created because the only followup matches a previous title.
    # Title-based dedupe — the count-based heuristic would fire here since
    # the only parsed followup would be "new" relative to a zero-length set,
    # which is precisely the bug this regression guards.
    assert mock_app.create_issue.call_count == 0
    assert result.get("followups_filed", []) == []


def test_handle_pr_merged_files_new_followup_when_old_one_dropped(tmp_path, monkeypatch):
    """Reviewer edits review: removes the old followup, adds a new one.
    Count stays the same. The old count-based heuristic suppressed the
    new followup; the title-keyed heuristic correctly files it."""
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review(
        "o/r",
        42,
        "APPROVE",
        "https://x/42",
        "- [FOLLOWUP] brand new concern\n",
    )
    # Pre-mark a DIFFERENT followup title as already-filed (count=1).
    review_store.mark_merged(
        "o/r",
        42,
        "2026-04-21T10:00:00Z",
        [501],
        followup_titles=["[seneschal followup] old concern the reviewer has since deleted"],
    )

    cfg = _config(followups=True)
    with patch("post_merge.orchestrator.app") as mock_app:
        mock_app.get_installation_token.return_value = "tok"
        mock_app.ensure_repo_synced.return_value = str(tmp_path / "clone")
        mock_app.create_issue = MagicMock(return_value={"number": 777})

        result = handle_pr_merged(
            owner="o",
            repo="r",
            pr_number=42,
            installation_id=1,
            pr_meta=_pr_meta(42),
            config=cfg,
        )

    # The new "brand new concern" followup IS filed even though the
    # parsed-count equals the already-filed count.
    assert mock_app.create_issue.call_count == 1
    assert result["followups_filed"] == [777]


# --------------------------------------------------------------------------
# Protected main → fall back to auto-PR
# --------------------------------------------------------------------------


def test_handle_pr_merged_falls_back_to_pr_when_protected(tmp_path, monkeypatch):
    # Reset the protection cache between tests.
    _PROTECTED_REPOS.clear()
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review("o/r", 42, "APPROVE", "https://x/42", "body")

    cfg = _config(changelog=True)
    with patch("post_merge.orchestrator.app") as mock_app:
        mock_app.get_installation_token.return_value = "tok"
        clone_dir = tmp_path / "clone"
        clone_dir.mkdir()
        (clone_dir / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n")
        mock_app.ensure_repo_synced.return_value = str(clone_dir)
        mock_app.PushProtectedError = PushProtectedError

        # First put_file (direct to main) raises PushProtectedError;
        # subsequent ones (on the auto-PR branch) succeed.
        call_count = {"n": 0}
        def _put_file(*args, **kwargs):
            call_count["n"] += 1
            branch = kwargs.get("branch") or (args[5] if len(args) > 5 else "")
            if call_count["n"] == 1 and branch == "main":
                raise PushProtectedError("main is protected")
            return {"commit": {"sha": "abc"}}
        mock_app.put_file = MagicMock(side_effect=_put_file)
        mock_app.create_branch = MagicMock(return_value={"ref": "refs/heads/seneschal/changelog-42"})
        mock_app.create_pull_request = MagicMock(return_value={"number": 99})
        mock_app.get_file_sha = MagicMock(return_value="oldsha")
        mock_app.get_default_branch_sha = MagicMock(return_value="defaultsha")

        result = handle_pr_merged(
            owner="o",
            repo="r",
            pr_number=42,
            installation_id=1,
            pr_meta=_pr_meta(42),
            config=cfg,
        )

    assert mock_app.create_branch.called
    assert mock_app.create_pull_request.called
    # The protection cache now remembers o/r is protected (TTL-wrapped
    # entry: (timestamp, protected_bool)).
    entry = _PROTECTED_REPOS.get("o/r")
    assert entry is not None
    _, is_protected = entry
    assert is_protected is True


# --------------------------------------------------------------------------
# Release-PR race: existing seneschal:release PR → amend instead of opening new
# --------------------------------------------------------------------------


def test_handle_pr_merged_amends_existing_release_pr(tmp_path, monkeypatch):
    _PROTECTED_REPOS.clear()
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review("o/r", 42, "APPROVE", "https://x/42", "body")

    cfg = _config(changelog=True, release_threshold="patch")
    with patch("post_merge.orchestrator.app") as mock_app:
        mock_app.get_installation_token.return_value = "tok"
        clone_dir = tmp_path / "clone"
        clone_dir.mkdir()
        (clone_dir / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\n### Fixed\n- earlier ([#41](x))\n"
        )
        mock_app.ensure_repo_synced.return_value = str(clone_dir)
        mock_app.put_file = MagicMock(return_value={"commit": {"sha": "abc"}})
        mock_app.get_file_sha = MagicMock(return_value="oldsha")
        mock_app.get_default_branch_sha = MagicMock(return_value="defaultsha")
        # An existing release PR is open.
        mock_app.find_open_prs_with_label = MagicMock(return_value=[
            {"number": 77, "head": {"ref": "seneschal/release-0.3.0"}},
        ])
        mock_app.create_pull_request = MagicMock()

        result = handle_pr_merged(
            owner="o",
            repo="r",
            pr_number=42,
            installation_id=1,
            pr_meta=_pr_meta(42, "fix: thing"),
            config=cfg,
        )

    # No new release PR created — amended the existing one instead.
    assert mock_app.create_pull_request.called is False or result.get("release_pr") == 77


# --------------------------------------------------------------------------
# All-off config: orchestrator no-ops cleanly
# --------------------------------------------------------------------------


def test_handle_pr_merged_no_ops_on_default_config(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review("o/r", 42, "APPROVE", "", "body")
    cfg = RepoConfig()  # all off
    with patch("post_merge.orchestrator.app") as mock_app:
        result = handle_pr_merged(
            owner="o",
            repo="r",
            pr_number=42,
            installation_id=1,
            pr_meta=_pr_meta(42),
            config=cfg,
        )
    assert result["changelog_updated"] is False
    assert result["followups_filed"] == []
    assert mock_app.create_issue.called is False


# --------------------------------------------------------------------------
# Exception swallowing
# --------------------------------------------------------------------------


def test_handle_pr_merged_swallows_exceptions(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review("o/r", 42, "APPROVE", "", "body")
    cfg = _config(changelog=True)
    with patch("post_merge.orchestrator.app") as mock_app:
        mock_app.get_installation_token.side_effect = RuntimeError("boom")
        # Must not raise.
        result = handle_pr_merged(
            owner="o",
            repo="r",
            pr_number=42,
            installation_id=1,
            pr_meta=_pr_meta(42),
            config=cfg,
        )
    assert "error" in result


# --------------------------------------------------------------------------
# Issue-body sanitization: attacker-controllable review excerpt must not
# fire @-mentions, cross-issue autolinks, tracking-pixel images, or HTML.
# --------------------------------------------------------------------------


def test_handle_pr_merged_sanitizes_issue_body(tmp_path, monkeypatch):
    _PROTECTED_REPOS.clear()
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    # A reviewer body crafted to abuse every vector we neutralize.
    save_review(
        "o/r",
        42,
        "APPROVE",
        "https://x/42",
        (
            "- [FOLLOWUP] investigate leak\n"
            "  ping @admin please also cc @security about #99\n"
            "  ![pixel](https://evil.example/track?sess=x)\n"
            "  <script>alert(1)</script>\n"
        ),
    )

    cfg = _config(followups=True)
    captured = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return {"number": 501}

    with patch("post_merge.orchestrator.app") as mock_app:
        mock_app.get_installation_token.return_value = "tok"
        mock_app.ensure_repo_synced.return_value = str(tmp_path / "clone")
        mock_app.create_issue = MagicMock(side_effect=_capture)

        handle_pr_merged(
            owner="o",
            repo="r",
            pr_number=42,
            installation_id=1,
            pr_meta=_pr_meta(42),
            config=cfg,
        )

    body = captured.get("body") or ""
    # Markdown image syntax stripped (tracking-pixel vector).
    assert "![pixel]" not in body
    assert "evil.example/track" not in body
    # HTML tags stripped.
    assert "<script>" not in body
    assert "</script>" not in body
    # @-mentions neutralized — GitHub's autolinker won't match
    # `@` followed immediately by a zero-width space.
    assert "@admin" not in body
    assert "@security" not in body
    # #-references neutralized too.
    assert "#99" not in body
    # The source PR link survives (we still want the back-reference).
    assert "#42" in body or "pull/42" in body


# --------------------------------------------------------------------------
# Changelog dead-letter queue: put_file retry exhaustion must not silently
# drop the PR's entry.
# --------------------------------------------------------------------------


def test_handle_pr_merged_dead_letters_changelog_on_conflict_exhaustion(tmp_path, monkeypatch):
    _PROTECTED_REPOS.clear()
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review("o/r", 42, "APPROVE", "https://x/42", "body")

    cfg = _config(changelog=True, followup_label="followup")
    with patch("post_merge.orchestrator.app") as mock_app:
        mock_app.get_installation_token.return_value = "tok"
        clone_dir = tmp_path / "clone"
        clone_dir.mkdir()
        (clone_dir / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n")
        mock_app.ensure_repo_synced.return_value = str(clone_dir)
        mock_app.PushProtectedError = PushProtectedError
        mock_app.get_file_sha = MagicMock(return_value="oldsha")
        # put_file exhausts sha-conflict retries.
        mock_app.put_file = MagicMock(
            side_effect=RuntimeError("put_file: gave up after 3 retries on o/r/CHANGELOG.md")
        )
        mock_app.create_issue = MagicMock(return_value={"number": 999})

        result = handle_pr_merged(
            owner="o",
            repo="r",
            pr_number=42,
            installation_id=1,
            pr_meta=_pr_meta(42, "feat: add thing"),
            config=cfg,
        )

    # Dead-letter issue filed exactly once.
    assert mock_app.create_issue.call_count == 1
    call = mock_app.create_issue.call_args
    assert "#42" in (call.kwargs.get("title") or "")
    assert result["changelog_updated"] is False


# --------------------------------------------------------------------------
# Release step fetches PR commits for BREAKING-CHANGE footer detection.
# --------------------------------------------------------------------------


def test_release_step_fetches_commits_for_breaking_detection(tmp_path, monkeypatch):
    _PROTECTED_REPOS.clear()
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review("o/r", 42, "APPROVE", "https://x/42", "body")

    # Threshold is "minor" and the changelog shows only a fix (= patch).
    # Without commit-body scan this stops at patch < minor and returns None.
    # With the scan, a commit body carrying BREAKING CHANGE: must force
    # the bump to major and trigger the release PR.
    cfg = _config(changelog=True, release_threshold="minor")
    with patch("post_merge.orchestrator.app") as mock_app:
        mock_app.get_installation_token.return_value = "tok"
        clone_dir = tmp_path / "clone"
        clone_dir.mkdir()
        (clone_dir / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\n### Fixed\n- a fix ([#40](x))\n"
        )
        mock_app.ensure_repo_synced.return_value = str(clone_dir)
        mock_app.put_file = MagicMock(return_value={"commit": {"sha": "abc"}})
        mock_app.get_file_sha = MagicMock(return_value="oldsha")
        mock_app.get_default_branch_sha = MagicMock(return_value="defaultsha")
        mock_app.find_open_prs_with_label = MagicMock(return_value=[])
        mock_app.get_pr_commits = MagicMock(return_value=[
            {"commit": {"message": "fix: patchy\n\nBREAKING CHANGE: drops config X"}},
        ])
        mock_app.create_pull_request = MagicMock(return_value={"number": 88})
        mock_app.create_branch = MagicMock(return_value={"ref": "refs/heads/x"})

        result = handle_pr_merged(
            owner="o",
            repo="r",
            pr_number=42,
            installation_id=1,
            pr_meta=_pr_meta(42, "fix: tiny"),
            config=cfg,
        )

    # Commit-body scan escalated the bump from patch -> major, which
    # crosses the "minor" threshold and opens a release PR.
    assert mock_app.get_pr_commits.called
    assert result.get("release_pr") == 88


# --------------------------------------------------------------------------
# TTL eviction on the protected-repos cache.
# --------------------------------------------------------------------------


def test_protected_cache_invalidates_after_ttl(monkeypatch):
    from post_merge import orchestrator as orch
    orch._PROTECTED_REPOS.clear()
    orch._mark_protected("o/r", True)
    assert orch._is_protected("o/r") is True

    # Fast-forward past TTL. W1 switched the cache to time.monotonic()
    # so NTP-step jumps can't evict fresh entries or keep stale ones
    # alive forever — the test patches the same clock source.
    orig_mono = orch.time.monotonic
    try:
        orch.time.monotonic = lambda: orig_mono() + orch._PROTECTED_TTL_SEC + 10
        assert orch._is_protected("o/r") is False
        # Entry got evicted, not just expired.
        assert "o/r" not in orch._PROTECTED_REPOS
    finally:
        orch.time.monotonic = orig_mono


# --------------------------------------------------------------------------
# 422 race on release-PR create → fall back to amend.
# --------------------------------------------------------------------------


def test_release_step_422_race_falls_back_to_amend(tmp_path, monkeypatch):
    _PROTECTED_REPOS.clear()
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review("o/r", 42, "APPROVE", "https://x/42", "body")

    cfg = _config(changelog=True, release_threshold="patch")
    # First find_open_prs_with_label returns [] (no existing PR), but the
    # create_pull_request call fails with 422 "already exists" (race), and
    # the retry find_open_prs_with_label returns the PR that opened in the
    # meantime.
    find_calls = {"n": 0}

    def _find(*args, **kwargs):
        find_calls["n"] += 1
        if find_calls["n"] == 1:
            return []
        return [{"number": 77, "head": {"ref": "seneschal/release-0.3.0"}}]

    with patch("post_merge.orchestrator.app") as mock_app:
        mock_app.get_installation_token.return_value = "tok"
        clone_dir = tmp_path / "clone"
        clone_dir.mkdir()
        (clone_dir / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\n### Fixed\n- x ([#1](x))\n"
        )
        mock_app.ensure_repo_synced.return_value = str(clone_dir)
        mock_app.put_file = MagicMock(return_value={"commit": {"sha": "abc"}})
        mock_app.get_file_sha = MagicMock(return_value="oldsha")
        mock_app.get_default_branch_sha = MagicMock(return_value="defaultsha")
        mock_app.find_open_prs_with_label = MagicMock(side_effect=_find)
        mock_app.create_branch = MagicMock(return_value={"ref": "refs/heads/x"})
        mock_app.create_pull_request = MagicMock(
            side_effect=RuntimeError("HTTP 422: A pull request already exists for o:seneschal/release-x")
        )
        mock_app.get_pr_commits = MagicMock(return_value=[])

        result = handle_pr_merged(
            owner="o",
            repo="r",
            pr_number=42,
            installation_id=1,
            pr_meta=_pr_meta(42, "fix: thing"),
            config=cfg,
        )

    # 422 caught → re-check → amend the existing PR #77 instead of raising.
    assert result.get("release_pr") == 77


# --------------------------------------------------------------------------
# Release-PR branch name reflects the computed next version when
# discoverable (W3).
# --------------------------------------------------------------------------


def test_release_pr_branch_uses_next_semver_when_discoverable(tmp_path, monkeypatch):
    _PROTECTED_REPOS.clear()
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review("o/r", 42, "APPROVE", "https://x/42", "body")

    cfg = _config(changelog=True, release_threshold="patch")
    with patch("post_merge.orchestrator.app") as mock_app:
        mock_app.get_installation_token.return_value = "tok"
        clone_dir = tmp_path / "clone"
        clone_dir.mkdir()
        (clone_dir / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\n### Fixed\n- x ([#1](x))\n"
        )
        # Current version = 1.2.3 via pyproject.toml.
        (clone_dir / "pyproject.toml").write_text(
            '[project]\nname = "thing"\nversion = "1.2.3"\n'
        )
        mock_app.ensure_repo_synced.return_value = str(clone_dir)
        mock_app.put_file = MagicMock(return_value={"commit": {"sha": "abc"}})
        mock_app.get_file_sha = MagicMock(return_value="oldsha")
        mock_app.get_default_branch_sha = MagicMock(return_value="defaultsha")
        mock_app.find_open_prs_with_label = MagicMock(return_value=[])
        mock_app.get_pr_commits = MagicMock(return_value=[])
        created = {}

        def _create_branch(owner, repo, new_ref, from_sha, token):
            created["branch"] = new_ref
            return {"ref": f"refs/heads/{new_ref}"}

        mock_app.create_branch = MagicMock(side_effect=_create_branch)
        mock_app.create_pull_request = MagicMock(return_value={"number": 99})

        handle_pr_merged(
            owner="o",
            repo="r",
            pr_number=42,
            installation_id=1,
            pr_meta=_pr_meta(42, "fix: tiny"),
            config=cfg,
        )

    # Next patch version = 1.2.4, so branch should reflect that instead
    # of the old "next" placeholder.
    assert created.get("branch") == "seneschal/release-1.2.4"


def test_release_pr_branch_falls_back_to_pending_when_version_unknown(tmp_path, monkeypatch):
    _PROTECTED_REPOS.clear()
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review("o/r", 42, "APPROVE", "https://x/42", "body")

    cfg = _config(changelog=True, release_threshold="patch")
    with patch("post_merge.orchestrator.app") as mock_app:
        mock_app.get_installation_token.return_value = "tok"
        clone_dir = tmp_path / "clone"
        clone_dir.mkdir()
        # No pyproject, package.json, VERSION, or git tags.
        (clone_dir / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\n### Fixed\n- x ([#1](x))\n"
        )
        mock_app.ensure_repo_synced.return_value = str(clone_dir)
        mock_app.put_file = MagicMock(return_value={"commit": {"sha": "abc"}})
        mock_app.get_file_sha = MagicMock(return_value="oldsha")
        mock_app.get_default_branch_sha = MagicMock(return_value="defaultsha")
        mock_app.find_open_prs_with_label = MagicMock(return_value=[])
        mock_app.get_pr_commits = MagicMock(return_value=[])
        created = {}

        def _create_branch(owner, repo, new_ref, from_sha, token):
            created["branch"] = new_ref
            return {"ref": f"refs/heads/{new_ref}"}

        mock_app.create_branch = MagicMock(side_effect=_create_branch)
        mock_app.create_pull_request = MagicMock(return_value={"number": 99})

        handle_pr_merged(
            owner="o",
            repo="r",
            pr_number=42,
            installation_id=1,
            pr_meta=_pr_meta(42, "fix: tiny"),
            config=cfg,
        )

    # No version source → branch name uses "pending-<bump>" not "next".
    branch = created.get("branch") or ""
    assert branch.startswith("seneschal/release-pending-")
    assert "next" not in branch


# --------------------------------------------------------------------------
# B3: Symlink traversal guard — `_read_local_changelog` and `_current_version`
# must refuse to read through symlinks pointing outside the repo tree.
# --------------------------------------------------------------------------


def test_read_local_changelog_refuses_symlink_outside_repo(tmp_path):
    """Attacker commits `CHANGELOG.md` as a symlink to a host file
    (e.g. ~/seneschal/ch-code-reviewer.pem). Without a guard, the
    orchestrator would read the pem's contents and `put_file` them
    back into the repo, exfiltrating the App's private key.

    The safe helper must return empty string on such a symlink AND log
    the refusal so operators can notice the attempt."""
    from post_merge.orchestrator import _read_local_changelog

    # Set up: a repo directory containing CHANGELOG.md → /etc/passwd.
    repo_dir = tmp_path / "clone"
    repo_dir.mkdir()
    # Target is outside the repo. Use tmp_path (which is outside repo_dir)
    # so the test doesn't depend on /etc/passwd existing & readable.
    outside = tmp_path / "secret.txt"
    outside.write_text("SUPER SECRET HOST CONTENT")
    (repo_dir / "CHANGELOG.md").symlink_to(outside)

    content = _read_local_changelog(str(repo_dir), "CHANGELOG.md")
    assert "SUPER SECRET" not in content
    assert content == ""


def test_read_local_changelog_accepts_regular_file(tmp_path):
    """Sanity: the guard must NOT break the happy path where
    CHANGELOG.md is a regular file inside the repo."""
    from post_merge.orchestrator import _read_local_changelog

    repo_dir = tmp_path / "clone"
    repo_dir.mkdir()
    (repo_dir / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n")

    content = _read_local_changelog(str(repo_dir), "CHANGELOG.md")
    assert "# Changelog" in content
    assert "Unreleased" in content


def test_current_version_refuses_symlink_outside_repo(tmp_path):
    """Analogous to the changelog case: `_current_version` reads
    pyproject.toml / package.json / VERSION. A symlink on any of those
    pointing outside the repo must be refused. Narrower exfil (the
    regex only matches a version string), but still a traversal."""
    from post_merge.orchestrator import _current_version

    repo_dir = tmp_path / "clone"
    repo_dir.mkdir()
    outside = tmp_path / "host-pyproject.toml"
    outside.write_text('[project]\nversion = "99.99.99"\n')
    (repo_dir / "pyproject.toml").symlink_to(outside)

    # Fallback: no other version source present, so if the symlink had
    # been followed we'd get "99.99.99". With the guard, we get None.
    result = _current_version(str(repo_dir))
    assert result != "99.99.99"


def test_safe_open_in_repo_blocks_absolute_outside_path(tmp_path):
    """Even without a symlink: if the `rel_path` itself resolves
    outside the repo (e.g. `../../etc/passwd`), the helper must refuse."""
    from post_merge.orchestrator import _safe_open_in_repo

    repo_dir = tmp_path / "clone"
    repo_dir.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("HOST CONTENT")

    result = _safe_open_in_repo(str(repo_dir), "../outside.txt")
    assert result is None


# --------------------------------------------------------------------------
# W2: _amend_release_pr must re-fetch CHANGELOG from main before writing,
# so a concurrent changelog commit landed between snapshot and amend isn't
# silently overwritten with stale content.
# --------------------------------------------------------------------------


def test_amend_release_pr_refetches_changelog_before_writing(tmp_path, monkeypatch):
    """Race: `_release_step`'s `existing` snapshot was taken before
    `_changelog_step` pushed its entry to main. If `_amend_release_pr`
    writes that stale snapshot back, the just-added entry is dropped
    from the release PR. Fix: re-fetch from the base branch right
    before the put_file."""
    _PROTECTED_REPOS.clear()
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review("o/r", 42, "APPROVE", "https://x/42", "body")

    cfg = _config(changelog=True, release_threshold="patch")
    with patch("post_merge.orchestrator.app") as mock_app:
        mock_app.get_installation_token.return_value = "tok"
        clone_dir = tmp_path / "clone"
        clone_dir.mkdir()
        # Stale local clone — this is the `existing` snapshot the caller
        # passes to _amend_release_pr.
        (clone_dir / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\n### Fixed\n- old ([#40](x))\n"
        )
        mock_app.ensure_repo_synced.return_value = str(clone_dir)
        mock_app.put_file = MagicMock(return_value={"commit": {"sha": "abc"}})
        mock_app.get_file_sha = MagicMock(return_value="oldsha")
        mock_app.get_default_branch_sha = MagicMock(return_value="defaultsha")
        mock_app.find_open_prs_with_label = MagicMock(return_value=[
            {"number": 77, "head": {"ref": "seneschal/release-0.3.0"}},
        ])
        # Fresh content from main — includes a NEW entry pushed between
        # the caller's snapshot and the amend. This is what must land on
        # the release branch, not the stale snapshot.
        fresh_from_main = (
            "# Changelog\n\n## [Unreleased]\n\n"
            "### Fixed\n- old ([#40](x))\n- NEW ([#42](x))\n"
        )
        mock_app.get_file_content = MagicMock(
            return_value=(fresh_from_main, "freshsha")
        )
        mock_app.create_pull_request = MagicMock()
        mock_app.get_pr_commits = MagicMock(return_value=[])

        handle_pr_merged(
            owner="o",
            repo="r",
            pr_number=42,
            installation_id=1,
            pr_meta=_pr_meta(42, "fix: thing"),
            config=cfg,
        )

    # Amend call must have happened.
    assert mock_app.get_file_content.called, (
        "_amend_release_pr didn't re-fetch CHANGELOG from main — "
        "W2 regression: stale snapshot would have been written back."
    )
    # The put_file that wrote the release branch must have used the
    # fresh content, not the stale one. Find that specific call.
    amend_calls = [
        c for c in mock_app.put_file.call_args_list
        if (c.kwargs.get("branch") or "").startswith("seneschal/release")
    ]
    assert amend_calls, "No put_file targeted the release branch"
    written = amend_calls[-1].kwargs.get("content", "")
    assert "NEW ([#42]" in written, (
        "Release branch got stale content; fresh fetch was ignored."
    )


# --------------------------------------------------------------------------
# Blocker 2 (round 3): _amend_release_pr must split fetch + put_file into
# separate try blocks so a fetch failure falls back to the snapshot
# instead of silently aborting the write.
# --------------------------------------------------------------------------


def test_amend_release_pr_falls_back_to_snapshot_when_fetch_fails(tmp_path, monkeypatch):
    """If get_file_content raises (404, network error), _amend_release_pr
    must STILL call put_file using the caller's snapshot content —
    previously fetch + write shared one try block so a fetch failure
    skipped the write entirely, leaving the release PR stale.

    The commit message should also be tagged so reviewers can tell the
    amend was built on a stale snapshot rather than a fresh read.
    """
    from post_merge.orchestrator import _amend_release_pr

    snapshot = (
        "# Changelog\n\n## [Unreleased]\n\n"
        "### Fixed\n- snapshot entry ([#99](x))\n"
    )
    existing_pr = {"number": 77, "head": {"ref": "seneschal/release-0.3.0"}}

    with patch("post_merge.orchestrator.app") as mock_app:
        # get_file_content raises — simulates a 404 / transient 5xx.
        mock_app.get_file_content = MagicMock(
            side_effect=RuntimeError("HTTP 500: upstream")
        )
        mock_app.get_file_sha = MagicMock(return_value="oldsha")
        mock_app.put_file = MagicMock(return_value={"commit": {"sha": "abc"}})

        result = _amend_release_pr(
            owner="o",
            repo="r",
            existing_pr=existing_pr,
            changelog_path="CHANGELOG.md",
            changelog_content=snapshot,
            token="tok",
        )

    # Should still return the PR number.
    assert result == 77
    # put_file MUST have been called despite the fetch failure.
    assert mock_app.put_file.called, (
        "Blocker 2 regression: fetch failure short-circuited the write — "
        "_amend_release_pr should fall back to the snapshot."
    )
    call = mock_app.put_file.call_args
    # Snapshot content was written.
    assert call.kwargs["content"] == snapshot
    # Commit message carries the stale-snapshot tag.
    assert "stale snapshot" in call.kwargs["message"].lower() or "stale" in call.kwargs["message"].lower()


def test_changelog_step_crlf_compare_is_normalized(tmp_path, monkeypatch):
    """W2 unit-level: the `new_content == existing` skip-check must
    normalize the raw CRLF `existing` before comparing against the
    LF-only `new_content` produced by insert_unreleased_entry. We verify
    the normalization directly by inspecting what gets passed to
    put_file — the CRLF-origin file should be rewritten as LF (no
    line-ending churn beyond the new entry itself)."""
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review("o/r", 42, "APPROVE", "https://x/42", "body")

    cfg = _config(changelog=True)
    with patch("post_merge.orchestrator.app") as mock_app:
        mock_app.get_installation_token.return_value = "tok"
        clone_dir = tmp_path / "clone"
        clone_dir.mkdir()
        # Write CHANGELOG with CRLF line endings (Windows-origin).
        existing = (
            "# Changelog\n\n"
            "## [Unreleased]\n\n"
            "### Added\n"
            "- prior entry ([#41](x))\n\n"
        )
        (clone_dir / "CHANGELOG.md").write_bytes(
            existing.replace("\n", "\r\n").encode("utf-8")
        )
        mock_app.ensure_repo_synced.return_value = str(clone_dir)
        mock_app.put_file = MagicMock(return_value={"commit": {"sha": "abc"}})
        mock_app.get_file_sha = MagicMock(return_value="oldsha")

        handle_pr_merged(
            owner="o",
            repo="r",
            pr_number=42,
            installation_id=1,
            pr_meta=_pr_meta(42, "feat: add thing"),
            config=cfg,
        )

    # The content written to GitHub must be LF-only (insert produces
    # LF; compare against normalized existing so the "skip" path is
    # reachable for future dedupe logic).
    assert mock_app.put_file.called
    written = mock_app.put_file.call_args.kwargs.get("content") or ""
    assert "\r\n" not in written, (
        "put_file received CRLF content — W2 fix should normalize "
        "existing to LF on compare + carry LF through the write."
    )


def test_amend_release_pr_uses_fresh_when_fetch_succeeds(tmp_path, monkeypatch):
    """Complement to the snapshot-fallback test: when get_file_content
    returns content, that's what should land on the release branch,
    and the commit message should NOT carry the stale-snapshot tag."""
    from post_merge.orchestrator import _amend_release_pr

    snapshot = (
        "# Changelog\n\n## [Unreleased]\n\n"
        "### Fixed\n- stale ([#40](x))\n"
    )
    fresh = (
        "# Changelog\n\n## [Unreleased]\n\n"
        "### Fixed\n- stale ([#40](x))\n- NEW ([#42](x))\n"
    )
    existing_pr = {"number": 77, "head": {"ref": "seneschal/release-0.3.0"}}

    with patch("post_merge.orchestrator.app") as mock_app:
        mock_app.get_file_content = MagicMock(return_value=(fresh, "freshsha"))
        mock_app.get_file_sha = MagicMock(return_value="oldsha")
        mock_app.put_file = MagicMock(return_value={"commit": {"sha": "abc"}})

        _amend_release_pr(
            owner="o",
            repo="r",
            existing_pr=existing_pr,
            changelog_path="CHANGELOG.md",
            changelog_content=snapshot,
            token="tok",
        )

    call = mock_app.put_file.call_args
    assert call.kwargs["content"] == fresh
    # Fresh fetch succeeded → no stale-snapshot tag.
    assert "stale" not in call.kwargs["message"].lower()


# --------------------------------------------------------------------------
# W3: _is_already_exists_error must not false-positive on unrelated 422s
# with "pull request" substring but no "already exists".
# --------------------------------------------------------------------------


def test_is_already_exists_error_requires_already_exists_substring():
    """A 422 error with "pull request" but NOT "already exists" should
    NOT be treated as an existing-PR race — the previous permissive
    matcher masked real validation failures as "oh the PR is already
    open, just amend", hiding bugs."""
    from post_merge.orchestrator import _is_already_exists_error

    # True positive: contains both "422" and "already exists".
    assert _is_already_exists_error(
        RuntimeError("HTTP 422: A pull request already exists for x")
    ) is True

    # False positive regression: contains "422" and "pull request" but
    # not "already exists" — must NOT match.
    assert _is_already_exists_error(
        RuntimeError("HTTP 422: pull request body is invalid")
    ) is False
    assert _is_already_exists_error(
        RuntimeError("HTTP 422: pull request field 'head' not found")
    ) is False


def test_release_pr_body_uses_render_release_notes(tmp_path, monkeypatch):
    """W8: the release PR body should be built via render_release_notes
    (structured `## [<version>] - <date>` section) rather than the
    previous hand-rolled one-liner. The PR description is the easiest
    place for a reviewer to see what's going into the tagged release,
    so the structure matters."""
    _PROTECTED_REPOS.clear()
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review("o/r", 42, "APPROVE", "https://x/42", "body")

    cfg = _config(changelog=True, release_threshold="patch")
    captured_body = {}

    def _capture_create(**kwargs):
        captured_body["body"] = kwargs.get("body") or ""
        return {"number": 99}

    with patch("post_merge.orchestrator.app") as mock_app:
        mock_app.get_installation_token.return_value = "tok"
        clone_dir = tmp_path / "clone"
        clone_dir.mkdir()
        # Version discoverable → render_release_notes should fire.
        (clone_dir / "pyproject.toml").write_text(
            '[project]\nname = "thing"\nversion = "0.2.3"\n'
        )
        (clone_dir / "CHANGELOG.md").write_text(
            "# Changelog\n\n"
            "## [Unreleased]\n\n"
            "### Fixed\n- broken thing ([#40](x))\n"
        )
        mock_app.ensure_repo_synced.return_value = str(clone_dir)
        mock_app.put_file = MagicMock(return_value={"commit": {"sha": "abc"}})
        mock_app.get_file_sha = MagicMock(return_value="oldsha")
        mock_app.get_default_branch_sha = MagicMock(return_value="defaultsha")
        mock_app.find_open_prs_with_label = MagicMock(return_value=[])
        mock_app.get_pr_commits = MagicMock(return_value=[])
        mock_app.create_branch = MagicMock(return_value={"ref": "refs/heads/x"})
        mock_app.create_pull_request = MagicMock(side_effect=_capture_create)

        handle_pr_merged(
            owner="o",
            repo="r",
            pr_number=42,
            installation_id=1,
            pr_meta=_pr_meta(42, "fix: tiny"),
            config=cfg,
        )

    body = captured_body.get("body", "")
    # Structured release-notes section is present.
    assert "Release notes preview" in body
    # The new version (0.2.3 + patch = 0.2.4) appears in the rendered header.
    assert "## [0.2.4]" in body
    # The Unreleased header itself is replaced — don't leak "Unreleased"
    # inside the release-notes block (the wrapper text can still mention
    # "## [Unreleased] entries warrant...").
    # Count the notes-preview section and confirm it doesn't echo the
    # Unreleased literal header.
    after_preview = body.split("Release notes preview", 1)[1] if "Release notes preview" in body else ""
    assert "## [Unreleased]" not in after_preview
    # The underlying bullet survives.
    assert "broken thing" in body


def test_release_pr_body_falls_back_when_version_unknown(tmp_path, monkeypatch):
    """If current_version returns None, render_release_notes can't be
    called (no next_version target). Fall back to a minimal hand-rolled
    body so the PR still opens."""
    _PROTECTED_REPOS.clear()
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review("o/r", 42, "APPROVE", "https://x/42", "body")

    cfg = _config(changelog=True, release_threshold="patch")
    captured_body = {}

    def _capture_create(**kwargs):
        captured_body["body"] = kwargs.get("body") or ""
        return {"number": 99}

    with patch("post_merge.orchestrator.app") as mock_app:
        mock_app.get_installation_token.return_value = "tok"
        clone_dir = tmp_path / "clone"
        clone_dir.mkdir()
        # No version source.
        (clone_dir / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\n### Fixed\n- x ([#1](x))\n"
        )
        mock_app.ensure_repo_synced.return_value = str(clone_dir)
        mock_app.put_file = MagicMock(return_value={"commit": {"sha": "abc"}})
        mock_app.get_file_sha = MagicMock(return_value="oldsha")
        mock_app.get_default_branch_sha = MagicMock(return_value="defaultsha")
        mock_app.find_open_prs_with_label = MagicMock(return_value=[])
        mock_app.get_pr_commits = MagicMock(return_value=[])
        mock_app.create_branch = MagicMock(return_value={"ref": "refs/heads/x"})
        mock_app.create_pull_request = MagicMock(side_effect=_capture_create)

        handle_pr_merged(
            owner="o",
            repo="r",
            pr_number=42,
            installation_id=1,
            pr_meta=_pr_meta(42, "fix: tiny"),
            config=cfg,
        )

    body = captured_body.get("body", "")
    assert "warrant a `patch` bump" in body
    # No structured notes preview when version is unknown.
    assert "Release notes preview" not in body


def test_strip_md_images_handles_nested_parens():
    """Minor cleanup: `_IMAGE_MD_RE = r"!\\[.*\\]\\([^)]*\\)"` stops at
    the first `)` so a URL with nested parens `![alt](http://x/a(b).png)`
    would mis-strip only the first half, leaving `b).png)` visible in
    the issue body where the tracking-pixel URL could still leak."""
    from post_merge.orchestrator import _strip_md_images

    # Simple case still works.
    assert _strip_md_images("text ![alt](http://x) more") == "text [image removed] more"
    # Nested parens: the balanced matcher must find the OUTER `)`.
    out = _strip_md_images("hi ![alt](http://x/a(b).png) bye")
    assert out == "hi [image removed] bye"
    assert "a(b).png" not in out
    # Multiple images on one line.
    out = _strip_md_images("![a](x) and ![b](y(z))")
    assert "[image removed]" in out
    assert "y(z)" not in out
    # No image at all: pass through.
    assert _strip_md_images("just text") == "just text"
    # Malformed (unbalanced): return verbatim rather than throwing.
    assert "![alt](http" in _strip_md_images("![alt](http")


def test_followups_continue_past_transient_create_issue_failure(tmp_path, monkeypatch):
    """W6: if create_issue fails for followup #2 (e.g. transient 500),
    the orchestrator must STILL attempt followup #3 + persist whatever
    succeeded. Previous behavior was `break` on the first failure,
    silently dropping all subsequent followups — which was then masked
    by title-keyed dedupe on retry: the missing ones NEVER got filed
    because mark_merged had already recorded partial success.

    Here we simulate: 3 followups; #1 succeeds, #2 raises, #3 succeeds.
    Expected: 2 issues filed, 2 titles persisted.
    """
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review(
        "o/r",
        50,
        "APPROVE",
        "https://x/50",
        "## Review\n\n"
        "- [FOLLOWUP] first thing\n"
        "- [FOLLOWUP] second thing\n"
        "- [FOLLOWUP] third thing\n",
    )

    cfg = _config(followups=True)

    call_count = {"n": 0}

    def _flaky_create_issue(**kwargs):
        call_count["n"] += 1
        # Fail on the second call only.
        if call_count["n"] == 2:
            raise RuntimeError("HTTP 500: transient server error")
        return {"number": 900 + call_count["n"]}

    with patch("post_merge.orchestrator.app") as mock_app:
        mock_app.get_installation_token.return_value = "tok"
        mock_app.ensure_repo_synced.return_value = str(tmp_path / "clone")
        (tmp_path / "clone").mkdir(exist_ok=True)
        mock_app.create_issue = MagicMock(side_effect=_flaky_create_issue)

        result = handle_pr_merged(
            owner="o",
            repo="r",
            pr_number=50,
            installation_id=1,
            pr_meta=_pr_meta(50, "feat: x"),
            config=cfg,
        )

    # Three attempts total — one failed, two succeeded.
    assert mock_app.create_issue.call_count == 3, (
        f"W6 regression: expected 3 create_issue attempts, got "
        f"{mock_app.create_issue.call_count}. Loop broke on first failure?"
    )
    # Two issue numbers persisted (the successful ones).
    assert len(result["followups_filed"]) == 2
    # And review store carries the two successful titles so a retry
    # dedupes correctly + only re-tries the failed one.
    rec = review_store.get_review("o/r", 50)
    assert rec is not None
    assert len(rec.followups_filed_titles) == 2
    # The failed title ("second thing") must NOT be in the persisted
    # titles — that would block the retry from re-filing it.
    titles_lower = [t.casefold() for t in rec.followups_filed_titles]
    assert not any("second" in t for t in titles_lower), (
        f"W6 regression: failed followup was persisted as succeeded: "
        f"{rec.followups_filed_titles}"
    )


def test_safe_open_in_repo_refuses_intermediate_symlink(tmp_path, monkeypatch):
    """W5: `_safe_open_in_repo` must refuse when any INTERMEDIATE
    directory in the path is a symlink. The old code only used
    O_NOFOLLOW on the final component, so `docs/` → `/etc` combined
    with reading `docs/CHANGELOG.md` would resolve through the
    intermediate symlink and our commonpath check could be bypassed
    on adversarial symlink targets.

    Fix walks each intermediate component with os.lstat and refuses
    if any is a symlink.
    """
    from post_merge.orchestrator import _safe_open_in_repo

    # Set up a fake repo with a symlinked intermediate dir.
    repo = tmp_path / "repo"
    repo.mkdir()
    # Real docs dir with a real changelog — this is what `docs/CHANGELOG.md`
    # SHOULD point at.
    real_docs = tmp_path / "elsewhere"
    real_docs.mkdir()
    (real_docs / "CHANGELOG.md").write_text("secret content from outside")
    # Replace `docs` with a symlink to `elsewhere`.
    os.symlink(str(real_docs), str(repo / "docs"))

    # Attempt to read `docs/CHANGELOG.md` — even though the realpath
    # resolves inside `tmp_path`, the intermediate `docs/` is a
    # symlink and must be rejected.
    result = _safe_open_in_repo(str(repo), "docs/CHANGELOG.md")
    assert result is None, (
        "W5 regression: intermediate-symlink was allowed through. "
        "Returned content: %r" % (result,)
    )


def test_safe_open_in_repo_allows_nested_real_dirs(tmp_path):
    """Sanity: the intermediate-symlink check must NOT false-positive on
    ordinary nested directories. `docs/notes/CHANGELOG.md` where every
    component is a real dir must still read successfully."""
    from post_merge.orchestrator import _safe_open_in_repo

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "docs").mkdir()
    (repo / "docs" / "notes").mkdir()
    (repo / "docs" / "notes" / "CHANGELOG.md").write_text("legit content")

    result = _safe_open_in_repo(str(repo), "docs/notes/CHANGELOG.md")
    assert result == "legit content"


def test_is_already_exists_error_via_response_attribute():
    """Mirror the text path through `err.response.status_code` + .text."""
    from post_merge.orchestrator import _is_already_exists_error

    class _Resp:
        def __init__(self, code, text):
            self.status_code = code
            self.text = text

    class _Err(Exception):
        def __init__(self, resp):
            super().__init__("some http error")
            self.response = resp

    assert _is_already_exists_error(
        _Err(_Resp(422, "A pull request already exists for branch X"))
    ) is True
    assert _is_already_exists_error(
        _Err(_Resp(422, "some other 422 error without the magic phrase"))
    ) is False
    assert _is_already_exists_error(
        _Err(_Resp(500, "already exists but wrong status"))
    ) is False
