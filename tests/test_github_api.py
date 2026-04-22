"""Tests for `github_api` module helpers that don't belong to the larger
orchestrator / concurrency suites.

All GitHub I/O is mocked via patching the module-local `_github_session`.
"""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import github_api  # noqa: E402


def _mock_response(status_code=200, json_body=None):
    """Build a minimal mock that quacks like `requests.Response`."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body if json_body is not None else []
    resp.raise_for_status = MagicMock()
    return resp


# --------------------------------------------------------------------------
# get_pr_commits pagination (round 5 W2)
#
# Previous behavior: one-shot `per_page=100` returned only the first page
# of a PR's commits. A PR with 101+ commits silently missed breaking-
# change signals past commit 100, so release-bump kind was computed
# against partial data.
# --------------------------------------------------------------------------


def test_get_pr_commits_concatenates_multiple_pages():
    """Three full-ish pages: two full (100 each) + one short (20). The
    function must return the concatenated 220 commits, not just the
    first 100."""
    page1 = [{"sha": f"p1-{i}", "commit": {"message": f"c{i}"}} for i in range(100)]
    page2 = [{"sha": f"p2-{i}", "commit": {"message": f"c{i}"}} for i in range(100)]
    page3 = [{"sha": f"p3-{i}", "commit": {"message": f"c{i}"}} for i in range(20)]

    session = MagicMock()
    session.get.side_effect = [
        _mock_response(json_body=page1),
        _mock_response(json_body=page2),
        _mock_response(json_body=page3),
    ]

    with patch("github_api._github_session", return_value=session):
        out = github_api.get_pr_commits("o", "r", 42, "tok")

    assert len(out) == 220, (
        f"expected all 3 pages concatenated (220 commits); got {len(out)}. "
        "Without pagination the release-bump scan misses BREAKING CHANGE "
        "signals past commit 100."
    )
    # Order preserved: page1 first, then page2, then page3.
    assert out[0]["sha"] == "p1-0"
    assert out[100]["sha"] == "p2-0"
    assert out[200]["sha"] == "p3-0"
    # Exactly 3 GET requests were made (page=1, page=2, page=3).
    assert session.get.call_count == 3, (
        f"expected 3 paginated GETs; got {session.get.call_count}"
    )


def test_get_pr_commits_stops_on_short_page():
    """A page smaller than per_page=100 means no more pages — don't keep
    requesting page N+1, that's wasted I/O on every call."""
    short_page = [{"sha": f"s-{i}", "commit": {"message": f"c{i}"}} for i in range(5)]

    session = MagicMock()
    session.get.side_effect = [_mock_response(json_body=short_page)]

    with patch("github_api._github_session", return_value=session):
        out = github_api.get_pr_commits("o", "r", 42, "tok")

    assert len(out) == 5
    # Single GET — loop must break on short page.
    assert session.get.call_count == 1, (
        "short-page response must short-circuit the pagination loop"
    )


def test_get_pr_commits_stops_on_empty_page():
    """Edge case: the FIRST response is empty. Don't loop forever."""
    session = MagicMock()
    session.get.side_effect = [_mock_response(json_body=[])]

    with patch("github_api._github_session", return_value=session):
        out = github_api.get_pr_commits("o", "r", 42, "tok")

    assert out == []
    assert session.get.call_count == 1


def test_get_pr_commits_caps_at_page_limit():
    """A PR with >1000 commits (a rare-but-possible monster merge)
    should hit the page cap at 10. The function must return the bounded
    scan rather than loop unbounded — runaway I/O on a pathological PR
    can block the post-merge queue.
    """
    # 11 full pages' worth of responses queued — if uncapped, the loop
    # would consume all 11. The cap should stop at 10.
    full_page = [{"sha": f"x-{i}", "commit": {"message": f"c{i}"}} for i in range(100)]
    responses = [_mock_response(json_body=full_page) for _ in range(11)]

    session = MagicMock()
    session.get.side_effect = responses

    # Patch log to capture the truncation warning.
    captured_logs = []

    def _capture_log(msg):
        captured_logs.append(msg)

    with patch("github_api._github_session", return_value=session), \
            patch("app.log", _capture_log):
        out = github_api.get_pr_commits("o", "r", 42, "tok")

    # Cap is 10 pages × 100 = 1000 commits.
    assert len(out) == 1000, (
        f"expected cap at 10 pages (1000 commits); got {len(out)}. "
        "Unbounded pagination can stall the post-merge worker."
    )
    assert session.get.call_count == 10, (
        f"expected exactly 10 GETs at cap; got {session.get.call_count}"
    )
    # The truncation warning must be logged so operators see the cap hit.
    assert any("truncated" in msg.lower() for msg in captured_logs), (
        f"expected a truncation warning in logs; got {captured_logs!r}"
    )
