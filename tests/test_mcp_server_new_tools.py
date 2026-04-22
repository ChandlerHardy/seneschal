"""Tests for the 5 new MCP tools registered in mcp_server/server.py.

The MCP tools are thin wrappers around the `review_index`, `cross_repo`,
`dependency_grep`, and `seneschal_token` modules — so we mock those
dependencies and focus on:
  - Error-payload shape (dict with "error" key or list with one-element
    error dict) is consistent with the existing 4 tools.
  - validate_repo_slug rejection flows back as an error, not a crash.
  - `seneschal_followups` correctly skips repos where the App isn't
    installed.
  - `seneschal_merged_prs` plumbs since/limit.
  - `seneschal_dependency_usage` returns hits as dicts.

We don't test the underlying modules here (those have their own suites).
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import seneschal_token  # noqa: E402
from mcp_server import server  # noqa: E402


# --------------------------------------------------------------------------
# seneschal_search_reviews / seneschal_search_adrs.
# --------------------------------------------------------------------------


def test_search_reviews_returns_list_of_dicts():
    mock_idx = MagicMock()
    mock_idx.search_reviews.return_value = [
        {"repo": "a/b", "pr_number": 1, "verdict": "APPROVE",
         "timestamp": "2026-04-21T00:00:00Z", "merged_at": None,
         "head_sha": "", "url": "", "snippet": "hit"},
    ]
    with patch.object(server, "_get_index", return_value=mock_idx):
        out = server.seneschal_search_reviews("query")
    assert isinstance(out, list)
    assert out[0]["repo"] == "a/b"


def test_search_reviews_error_payload_on_invalid_repo():
    with patch.object(server, "_get_index") as get_index:
        # validate_repo_slug is applied inside search_reviews; mock it to raise.
        mock_idx = MagicMock()
        mock_idx.search_reviews.side_effect = ValueError("invalid repo slug: 'bad'")
        get_index.return_value = mock_idx
        out = server.seneschal_search_reviews("query", repo="../etc/passwd")
    assert isinstance(out, list)
    assert len(out) == 1
    assert "error" in out[0]


def test_search_adrs_returns_list_of_dicts():
    mock_idx = MagicMock()
    mock_idx.search_adrs.return_value = [
        {"repo": "a/b", "path": "docs/adr/0001.md", "id": "0001",
         "title": "T", "status": "accepted", "snippet": "body"},
    ]
    with patch.object(server, "_get_index", return_value=mock_idx):
        out = server.seneschal_search_adrs("postgres")
    assert isinstance(out, list)
    assert out[0]["id"] == "0001"
    # `snippet` is the unified key across search_reviews and search_adrs;
    # the old `excerpt` key has been renamed.
    assert "snippet" in out[0]
    assert "excerpt" not in out[0]


def test_search_adrs_error_payload_on_invalid_repo():
    with patch.object(server, "_get_index") as get_index:
        mock_idx = MagicMock()
        mock_idx.search_adrs.side_effect = ValueError("invalid repo slug: 'bad'")
        get_index.return_value = mock_idx
        out = server.seneschal_search_adrs("q", repo="../etc")
    assert len(out) == 1 and "error" in out[0]


# --------------------------------------------------------------------------
# seneschal_merged_prs.
# --------------------------------------------------------------------------


def test_merged_prs_returns_list_of_dicts():
    mock_idx = MagicMock()
    mock_idx.list_merged_prs.return_value = [
        {"repo": "a/b", "pr_number": 5, "verdict": "APPROVE",
         "timestamp": "2026-04-21T00:00:00Z", "merged_at": "2026-04-22T00:00:00Z",
         "head_sha": "abc", "url": ""},
    ]
    with patch.object(server, "_get_index", return_value=mock_idx):
        out = server.seneschal_merged_prs(since="2026-04-01T00:00:00Z")
    assert len(out) == 1
    assert out[0]["merged_at"] == "2026-04-22T00:00:00Z"
    # `since` arg threaded through.
    call_kwargs = mock_idx.list_merged_prs.call_args.kwargs
    assert call_kwargs.get("since") == "2026-04-01T00:00:00Z"


def test_merged_prs_error_on_bad_slug():
    mock_idx = MagicMock()
    mock_idx.list_merged_prs.side_effect = ValueError("invalid repo slug: 'bad'")
    with patch.object(server, "_get_index", return_value=mock_idx):
        out = server.seneschal_merged_prs(repo="..")
    assert len(out) == 1 and "error" in out[0]


# --------------------------------------------------------------------------
# seneschal_followups — mocks mint_installation_token + requests.get.
# --------------------------------------------------------------------------


def _issue_response(items):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = items
    resp.raise_for_status = MagicMock()
    resp.headers = {}
    return resp


def test_followups_skips_repos_where_app_not_installed():
    import cross_repo

    fake_repos = [
        cross_repo.KnownRepo(slug="a/b", path="/tmp/a/b"),
        cross_repo.KnownRepo(slug="c/d", path="/tmp/c/d"),
    ]

    def _mint(slug):
        if slug == "a/b":
            raise seneschal_token.AppNotInstalledError(slug)
        return "fake-tok"

    with patch.object(cross_repo, "known_repos", return_value=fake_repos), patch.object(
        seneschal_token, "mint_installation_token", side_effect=_mint
    ), patch("mcp_server.server.requests.get") as rget:
        rget.return_value = _issue_response([
            {"number": 101, "title": "fix retry",
             "state": "open", "html_url": "https://x/101"},
        ])
        out = server.seneschal_followups()
    # Only one repo yielded followups (the App-installed one).
    assert len(out) == 1
    assert out[0]["repo"] == "c/d"
    assert out[0]["number"] == 101


def test_followups_scoped_to_single_repo():
    with patch.object(seneschal_token, "mint_installation_token", return_value="tok"), \
         patch("mcp_server.server.requests.get") as rget:
        rget.return_value = _issue_response([
            {"number": 5, "title": "do thing",
             "state": "open", "html_url": "https://x/5"},
        ])
        out = server.seneschal_followups(repo="owner/only")
    assert all(item["repo"] == "owner/only" for item in out)
    # First positional arg (URL) includes the explicit slug.
    first_call = rget.call_args_list[0]
    url = first_call.args[0] if first_call.args else first_call.kwargs.get("url", "")
    assert "owner/only" in url


def test_followups_respects_limit():
    """Large pages returned by GitHub must be truncated to `limit`."""
    with patch.object(seneschal_token, "mint_installation_token", return_value="tok"), \
         patch("mcp_server.server.requests.get") as rget:
        page = [
            {"number": n, "title": f"t{n}", "state": "open",
             "html_url": f"https://x/{n}"}
            for n in range(1, 51)
        ]
        rget.return_value = _issue_response(page)
        out = server.seneschal_followups(repo="owner/r", limit=5)
    assert len(out) == 5


def test_dependency_usage_error_does_not_leak_exception_detail():
    """Catch-all error paths must not echo raw exception strings back to
    the MCP client. Those strings can embed tokens (GitHub API errors
    include URLs with tokens in the query string), local paths, and
    other host-sensitive detail. The response should be a fixed-shape
    'internal error; see server logs' message instead."""
    import dependency_grep

    sensitive_msg = "boom ghs_SENSITIVE_TOKEN_XYZ /home/user/.config/secret"
    with patch.object(dependency_grep, "scan_all", side_effect=RuntimeError(sensitive_msg)):
        out = server.seneschal_dependency_usage("foo")
    assert len(out) == 1
    err = out[0].get("error", "")
    assert "SENSITIVE" not in err
    assert "secret" not in err.lower()
    # The error prefix carries the tool name for grep-ability.
    assert "seneschal_dependency_usage" in err


def test_search_reviews_error_does_not_leak_exception_detail():
    """Same contract for search_reviews. The ValueError path is allowed
    to echo (caller-facing validation messages); the catch-all path is
    not."""
    mock_idx = MagicMock()
    mock_idx.search_reviews.side_effect = RuntimeError("boom ghs_LEAK_TOKEN /etc/secret")
    with patch.object(server, "_get_index", return_value=mock_idx):
        out = server.seneschal_search_reviews("q")
    assert len(out) == 1
    err = out[0].get("error", "")
    assert "LEAK" not in err
    assert "/etc/secret" not in err


def test_followups_breaks_loop_on_rate_limit():
    """When GitHub returns 403/429 for one repo, every subsequent repo
    in the sweep will hit the same rate limit — the loop must break
    rather than burning through tokens + cluttering the response."""
    import cross_repo

    fake_repos = [
        cross_repo.KnownRepo(slug="a/one", path="/tmp/a/one"),
        cross_repo.KnownRepo(slug="a/two", path="/tmp/a/two"),
        cross_repo.KnownRepo(slug="a/three", path="/tmp/a/three"),
    ]

    rate_limited = MagicMock()
    rate_limited.status_code = 429
    rate_limited.raise_for_status = MagicMock()
    rate_limited.json.return_value = []
    rate_limited.headers = {}

    get_call_count = {"n": 0}

    def _fake_get(*args, **kwargs):
        get_call_count["n"] += 1
        return rate_limited

    with patch.object(cross_repo, "known_repos", return_value=fake_repos), patch.object(
        seneschal_token, "mint_installation_token", return_value="tok"
    ), patch("mcp_server.server.requests.get", side_effect=_fake_get):
        out = server.seneschal_followups()

    # The loop must STOP on the first rate-limit response — not call
    # requests.get for every subsequent repo.
    assert get_call_count["n"] == 1
    # Round-3: a rate-limited sweep returns a sentinel as the final
    # element so the caller can detect the truncation. The sentinel
    # must be the only result (no real followups landed before the
    # break since the very first repo rate-limited us).
    assert len(out) == 1
    sentinel = out[-1]
    assert sentinel.get("_truncated") is True
    assert sentinel.get("_reason") == "rate_limit"
    assert sentinel.get("_processed_slugs") == 1


def test_followups_rate_limit_sentinel_includes_partial_results():
    """When some repos return real followups before the rate-limit hits,
    the real results come first and the sentinel is appended last."""
    import cross_repo

    fake_repos = [
        cross_repo.KnownRepo(slug="a/one", path="/tmp/a/one"),
        cross_repo.KnownRepo(slug="a/two", path="/tmp/a/two"),
        cross_repo.KnownRepo(slug="a/three", path="/tmp/a/three"),
    ]

    responses = iter(
        [
            _issue_response(
                [
                    {
                        "number": 10,
                        "title": "fix retry",
                        "state": "open",
                        "html_url": "https://x/10",
                    },
                ]
            ),
            _issue_response(
                [
                    {
                        "number": 20,
                        "title": "wire up cache",
                        "state": "open",
                        "html_url": "https://x/20",
                    },
                ]
            ),
            # Third call rate-limits.
            (lambda: (lambda m: (setattr(m, "status_code", 429), m)[1])(MagicMock()))(),
        ]
    )

    def _fake_get(*args, **kwargs):
        resp = next(responses)
        # The lambda-built rate-limit response doesn't carry json/raise_for_status;
        # real code only inspects status_code for 403/429 before raising.
        return resp

    with patch.object(cross_repo, "known_repos", return_value=fake_repos), patch.object(
        seneschal_token, "mint_installation_token", return_value="tok"
    ), patch("mcp_server.server.requests.get", side_effect=_fake_get):
        out = server.seneschal_followups()

    # First 2 real results come through, then the sentinel.
    assert len(out) == 3
    assert out[0]["number"] == 10
    assert out[1]["number"] == 20
    sentinel = out[-1]
    assert sentinel.get("_truncated") is True
    assert sentinel.get("_reason") == "rate_limit"
    # 3 slugs were touched before the break (two succeeded, third triggered).
    assert sentinel.get("_processed_slugs") == 3


def test_followups_rejects_invalid_repo_slug():
    out = server.seneschal_followups(repo="../etc/passwd")
    assert len(out) == 1 and "error" in out[0]


def test_followups_returns_error_on_token_mint_failure():
    with patch.object(seneschal_token, "mint_installation_token") as mint:
        mint.side_effect = seneschal_token.TokenMintError("network down")
        out = server.seneschal_followups(repo="owner/x")
    assert len(out) == 1 and "error" in out[0]


# --------------------------------------------------------------------------
# seneschal_dependency_usage.
# --------------------------------------------------------------------------


def test_dependency_usage_returns_list_of_dicts():
    import dependency_grep

    fake_hits = [
        dependency_grep.Hit(
            repo="a/b", path="package.json", line=3, matched='"axios": "1.0"'
        ),
    ]
    with patch.object(dependency_grep, "scan_all", return_value=fake_hits):
        out = server.seneschal_dependency_usage("axios")
    assert out[0]["repo"] == "a/b"
    assert out[0]["path"] == "package.json"
    assert out[0]["line"] == 3


def test_dependency_usage_empty_for_missing_name():
    # Empty query -> empty list, no error payload.
    with patch("dependency_grep.scan_all", return_value=[]):
        out = server.seneschal_dependency_usage("")
    assert out == []


def test_dependency_usage_respects_limit():
    import dependency_grep

    with patch.object(dependency_grep, "scan_all") as scan:
        scan.return_value = []
        server.seneschal_dependency_usage("foo", limit=7)
    assert scan.call_args.kwargs.get("limit") == 7


# --------------------------------------------------------------------------
# Round-3: unified error shapes for P0 tools.
# --------------------------------------------------------------------------


def test_p0_tools_return_unified_error_shape_on_bad_slug():
    """Round-3 warnings #10-14: P0 tools used to return either
    `{"error": str(e)}` (no prefix) or `f"(error) {e}"` (string, not
    dict). The unified contract is now `{"error": "<tool> failed: ..."}`
    for single-dict tools and `[{"error": "<tool> failed: ..."}]` for
    list-returning tools — matching the P2 tool shape so callers can
    detect errors with one check across all 9 tools.
    """
    # seneschal_last_review — dict-returning.
    out = server.seneschal_last_review("not-a-slug")
    assert isinstance(out, dict)
    assert "error" in out
    assert out["error"].startswith("seneschal_last_review failed: ")

    # seneschal_review_history — list-returning.
    out = server.seneschal_review_history("not-a-slug")
    assert isinstance(out, list) and len(out) == 1
    assert out[0]["error"].startswith("seneschal_review_history failed: ")

    # seneschal_review_text — dict-returning.
    out = server.seneschal_review_text("not-a-slug", 1)
    assert isinstance(out, dict)
    assert out["error"].startswith("seneschal_review_text failed: ")

    # seneschal_repo_memory — dict-returning (was string-returning pre-round3).
    out = server.seneschal_repo_memory("not-a-slug", "/tmp")
    assert isinstance(out, dict)
    assert out["error"].startswith("seneschal_repo_memory failed: ")


def test_p0_tools_scrub_internal_exceptions():
    """Every P0 tool catches non-ValueError exceptions and routes them
    through `_error_dict`/`_error_list` with a fixed-shape "internal
    error; see server logs" message — so a stray OSError from
    review_store that embeds a token or local path can't leak through
    the MCP response channel."""
    sensitive_msg = "boom ghs_SENSITIVE_TOKEN_XYZ /home/user/.secret"

    # last_review
    with patch("mcp_server.server.last_review", side_effect=RuntimeError(sensitive_msg)):
        out = server.seneschal_last_review("owner/repo")
    assert "SENSITIVE" not in out.get("error", "")
    assert "seneschal_last_review" in out.get("error", "")

    # review_history
    with patch("mcp_server.server.list_reviews", side_effect=RuntimeError(sensitive_msg)):
        out = server.seneschal_review_history("owner/repo")
    assert len(out) == 1
    assert "SENSITIVE" not in out[0].get("error", "")
    assert "seneschal_review_history" in out[0].get("error", "")

    # review_text
    with patch("mcp_server.server.get_review", side_effect=RuntimeError(sensitive_msg)):
        out = server.seneschal_review_text("owner/repo", 1)
    assert "SENSITIVE" not in out.get("error", "")
    assert "seneschal_review_text" in out.get("error", "")

    # repo_memory
    with patch("mcp_server.server.get_repo_memory", side_effect=RuntimeError(sensitive_msg)):
        out = server.seneschal_repo_memory("owner/repo", "/tmp")
    assert "SENSITIVE" not in out.get("error", "")
    assert "seneschal_repo_memory" in out.get("error", "")


def test_seneschal_repo_memory_returns_content_dict_on_success(tmp_path):
    """The shape flip from `str` → `{"content": str}` is the one
    user-visible change; pin it explicitly so a future regression that
    unwraps the dict gets caught."""
    (tmp_path / ".seneschal-memory.md").write_text("# Rules\nUse tabs")
    out = server.seneschal_repo_memory("owner/repo", str(tmp_path))
    assert isinstance(out, dict)
    assert out.get("content") == "# Rules\nUse tabs"
    assert "error" not in out


def test_seneschal_review_history_default_limit_matches_constant():
    """Round-3 warning #12: the MCP server's `seneschal_review_history`
    used to default to 10; P2 tools defaulted to DEFAULT_SEARCH_LIMIT
    or DEFAULT_LIST_LIMIT. The P0 list tool now uses DEFAULT_LIST_LIMIT
    so defaults line up across the MCP surface."""
    import inspect

    sig = inspect.signature(server.seneschal_review_history)
    default = sig.parameters["limit"].default
    assert default == server.DEFAULT_LIST_LIMIT


# --------------------------------------------------------------------------
# Index lifecycle — lazy open + sync-once flag.
# --------------------------------------------------------------------------


def test_get_index_syncs_once_per_process(monkeypatch):
    """The first tool call opens the index + syncs; subsequent calls
    reuse the same Index and skip re-sync. The sync-once flag now
    lives on `Index.ensure_synced`, so this test just verifies the
    composition in `server._get_index`."""
    # Reset module-level cached Index.
    server._INDEX = None

    mock_idx = MagicMock()
    with patch("mcp_server.server.review_index.open_index", return_value=mock_idx):
        ix1 = server._get_index()
        ix2 = server._get_index()
    assert ix1 is ix2
    # ensure_synced was called for every get_index (cheap no-op after
    # the first call; the Index is responsible for not re-syncing).
    assert mock_idx.ensure_synced.call_count == 2
    # Cleanup.
    server._INDEX = None


def test_ensure_synced_runs_exactly_once_across_threads(tmp_path):
    """Blocker #7: two concurrent threads calling `ensure_synced`
    must only trigger `sync_from_markdown` once. The old module-level
    `_INDEX_SYNCED` sentinel was read-then-set without a lock, so two
    threads both saw False and both ran sync, colliding on a second
    BEGIN IMMEDIATE inside the first's transaction."""
    import threading
    import review_index

    db_path = tmp_path / "idx.db"
    ix = review_index.open_index(str(db_path))
    try:
        call_count = {"n": 0}
        ready = threading.Event()
        start = threading.Event()

        orig = ix.sync_from_markdown

        def _counting_sync(*a, **kw):
            call_count["n"] += 1
            # Hold inside the sync briefly so the second thread has
            # a chance to race on the flag.
            ready.set()
            start.wait(timeout=1.0)
            return orig(*a, **kw)

        ix.sync_from_markdown = _counting_sync

        results = []

        def _runner():
            ix.ensure_synced()
            results.append(True)

        t1 = threading.Thread(target=_runner)
        t2 = threading.Thread(target=_runner)
        t1.start()
        ready.wait(timeout=1.0)
        t2.start()
        start.set()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

        assert len(results) == 2
        assert call_count["n"] == 1, (
            "sync_from_markdown ran more than once across concurrent "
            "ensure_synced calls — sentinel race"
        )
    finally:
        ix.close()


def test_index_search_is_thread_safe(tmp_path):
    """Blocker #6: FastMCP can dispatch sync tools on a thread pool
    executor (asyncio.to_thread). The default sqlite3 connection has
    check_same_thread=True, which raises ProgrammingError when used
    from a second thread. The Index now opens with
    check_same_thread=False and serializes access via an internal
    RLock — concurrent searches from multiple threads must not raise."""
    import threading
    import review_index
    import review_store

    db_path = tmp_path / "idx.db"
    store_root = tmp_path / "reviews"
    store_root.mkdir()
    # Seed one review so search has content to scan.
    owner_dir = store_root / "a"
    owner_dir.mkdir()
    repo_dir = owner_dir / "b"
    repo_dir.mkdir()
    import json
    meta = {"pr_number": 1, "verdict": "APPROVE",
            "timestamp": "2026-04-21T00:00:00Z", "url": ""}
    (repo_dir / "1.md").write_text(
        f"---\n{json.dumps(meta)}\n---\nbody about migration"
    )

    ix = review_index.open_index(str(db_path))
    try:
        ix.sync_from_markdown(str(store_root))

        errors = []

        def _runner():
            try:
                for _ in range(10):
                    ix.search_reviews("migration")
                    ix.list_merged_prs()
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=_runner) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert errors == [], (
            f"concurrent Index access raised: {errors!r} "
            "(check_same_thread or lock misconfiguration)"
        )
    finally:
        ix.close()
