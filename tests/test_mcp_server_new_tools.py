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
         "title": "T", "status": "accepted", "excerpt": "body"},
    ]
    with patch.object(server, "_get_index", return_value=mock_idx):
        out = server.seneschal_search_adrs("postgres")
    assert isinstance(out, list)
    assert out[0]["id"] == "0001"


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
# Index lifecycle — lazy open + sync-once flag.
# --------------------------------------------------------------------------


def test_get_index_syncs_once_per_process(monkeypatch):
    """The first tool call opens the index + syncs; subsequent calls
    reuse the same Index and skip re-sync."""
    # Reset sentinel.
    server._INDEX = None
    server._INDEX_SYNCED = False

    mock_idx = MagicMock()
    with patch("mcp_server.server.review_index.open_index", return_value=mock_idx):
        ix1 = server._get_index()
        ix2 = server._get_index()
    assert ix1 is ix2
    # sync_from_markdown was called exactly once.
    assert mock_idx.sync_from_markdown.call_count == 1
    # Cleanup.
    server._INDEX = None
    server._INDEX_SYNCED = False
