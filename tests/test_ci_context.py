"""Tests for ci_context: fetch + correlate CI check runs."""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ci_context import (  # noqa: E402
    CheckRun,
    CIResult,
    correlate_failing_checks,
    fetch_ci_results,
    render_ci_addendum,
)


# --------------------------------------------------------------------------
# CIResult behavior
# --------------------------------------------------------------------------


def test_ciresult_default_is_unfetched():
    r = CIResult()
    assert r.fetched is False
    assert r.total == 0
    assert r.has_failures is False


def test_ciresult_has_failures_flag():
    r = CIResult(fetched=True, total=3, passing=2, failing=1)
    assert r.has_failures is True


def test_ciresult_failing_checks_filters():
    checks = [
        CheckRun(name="tests", conclusion="success", status="completed", summary="", html_url=""),
        CheckRun(name="lint", conclusion="failure", status="completed", summary="x", html_url=""),
        CheckRun(name="build", conclusion="failure", status="completed", summary="y", html_url=""),
    ]
    r = CIResult(fetched=True, total=3, passing=1, failing=2, checks=checks)
    failures = r.failing_checks()
    assert len(failures) == 2
    assert {c.name for c in failures} == {"lint", "build"}


# --------------------------------------------------------------------------
# fetch_ci_results — with mocked requests
# --------------------------------------------------------------------------


def _mock_response(status_code, payload):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=payload)
    return resp


def test_fetch_returns_unfetched_on_non_200():
    mock_session = MagicMock()
    mock_session.get.return_value = _mock_response(403, {"message": "rate limited"})
    with patch("ci_context._github_session", return_value=mock_session):
        result = fetch_ci_results("tok", "owner", "repo", "abc123")
    assert result.fetched is False
    assert result.total == 0


def test_fetch_returns_unfetched_on_exception():
    mock_session = MagicMock()
    mock_session.get.side_effect = Exception("network kaboom")
    with patch("ci_context._github_session", return_value=mock_session):
        result = fetch_ci_results("tok", "owner", "repo", "abc")
    assert result.fetched is False


def test_fetch_counts_passing_failing_inprogress():
    payload = {
        "check_runs": [
            {"name": "tests", "status": "completed", "conclusion": "success",
             "output": {"summary": "All 50 tests passed"}, "html_url": "https://ex/1"},
            {"name": "lint", "status": "completed", "conclusion": "failure",
             "output": {"summary": "3 style errors"}, "html_url": "https://ex/2"},
            {"name": "build", "status": "in_progress", "conclusion": None,
             "output": {}, "html_url": "https://ex/3"},
            {"name": "flaky", "status": "completed", "conclusion": "neutral",
             "output": {}, "html_url": "https://ex/4"},  # counted only in total
        ]
    }
    mock_session = MagicMock()
    mock_session.get.return_value = _mock_response(200, payload)
    with patch("ci_context._github_session", return_value=mock_session):
        result = fetch_ci_results("tok", "owner", "repo", "abc")
    assert result.fetched is True
    assert result.total == 4
    assert result.passing == 1
    assert result.failing == 1
    assert result.in_progress == 1
    assert len(result.checks) == 4


def test_fetch_handles_missing_output_field():
    payload = {
        "check_runs": [
            {"name": "t", "status": "completed", "conclusion": "success"},
        ]
    }
    mock_session = MagicMock()
    mock_session.get.return_value = _mock_response(200, payload)
    with patch("ci_context._github_session", return_value=mock_session):
        result = fetch_ci_results("tok", "owner", "repo", "abc")
    assert result.fetched is True
    assert result.passing == 1
    assert result.checks[0].summary == ""


def test_fetch_strips_control_chars_from_summary():
    payload = {
        "check_runs": [
            {"name": "t", "status": "completed", "conclusion": "failure",
             "output": {"summary": "bad \x00\x01chars"}},
        ]
    }
    mock_session = MagicMock()
    mock_session.get.return_value = _mock_response(200, payload)
    with patch("ci_context._github_session", return_value=mock_session):
        result = fetch_ci_results("tok", "owner", "repo", "abc")
    assert "\x00" not in result.checks[0].summary
    assert "\x01" not in result.checks[0].summary
    assert "bad" in result.checks[0].summary


def test_fetch_malformed_json_returns_fetched_empty():
    # API says 200 but returns non-dict — we still mark fetched=True but
    # the checks list is empty (caller will see total=0 and skip rendering).
    mock_session = MagicMock()
    mock_session.get.return_value = _mock_response(200, [])
    with patch("ci_context._github_session", return_value=mock_session):
        result = fetch_ci_results("tok", "owner", "repo", "abc")
    assert result.fetched is True
    assert result.total == 0


# --------------------------------------------------------------------------
# correlate_failing_checks — heuristic
# --------------------------------------------------------------------------


def _failing_check(name, summary=""):
    return CheckRun(name=name, conclusion="failure", status="completed", summary=summary, html_url="")


def test_correlate_returns_empty_when_unfetched():
    ci = CIResult()  # fetched=False
    assert correlate_failing_checks(ci, ["src/foo.py"]) == []


def test_correlate_returns_empty_when_no_failures():
    ci = CIResult(fetched=True, total=1, passing=1, failing=0)
    assert correlate_failing_checks(ci, ["x.py"]) == []


def test_correlate_matches_on_path_token_overlap_in_name():
    failing = _failing_check(name="test_order_handler")
    ci = CIResult(fetched=True, total=1, failing=1, checks=[failing])
    matched = correlate_failing_checks(ci, ["src/orders/handler.py"])
    # "order" or "orders" should overlap
    assert len(matched) == 1


def test_correlate_matches_on_summary_text():
    failing = _failing_check(name="tests", summary="Failed: tests/test_payment_flow.py::test_refund")
    ci = CIResult(fetched=True, total=1, failing=1, checks=[failing])
    matched = correlate_failing_checks(ci, ["src/payment/refund.py"])
    assert len(matched) == 1


def test_correlate_skips_unrelated_failures():
    failing = _failing_check(name="lint_frontend", summary="eslint error in button.tsx")
    ci = CIResult(fetched=True, total=1, failing=1, checks=[failing])
    matched = correlate_failing_checks(ci, ["backend/database/migrations/001.sql"])
    assert matched == []


# --------------------------------------------------------------------------
# render_ci_addendum
# --------------------------------------------------------------------------


def test_render_empty_for_unfetched():
    assert render_ci_addendum(CIResult()) == ""


def test_render_empty_for_zero_checks():
    assert render_ci_addendum(CIResult(fetched=True, total=0)) == ""


def test_render_includes_counts_and_urls():
    checks = [
        _failing_check(name="tests", summary="3 tests failed"),
    ]
    checks[0] = CheckRun(name="tests", conclusion="failure", status="completed", summary="3 tests failed", html_url="https://ex/log")
    ci = CIResult(fetched=True, total=2, passing=1, failing=1, checks=checks)
    out = render_ci_addendum(ci)
    assert "2" in out
    assert "Passing: 1" in out
    assert "Failing: 1" in out
    assert "tests" in out
    assert "https://ex/log" in out


def test_render_includes_correlated_section_when_provided():
    checks = [
        CheckRun(name="test_orders", conclusion="failure", status="completed", summary="", html_url=""),
    ]
    ci = CIResult(fetched=True, total=1, failing=1, checks=checks)
    out = render_ci_addendum(ci, correlated_failures=checks)
    assert "likely related" in out.lower()


def test_render_shows_in_progress_line_when_nonzero():
    ci = CIResult(fetched=True, total=2, passing=1, failing=0, in_progress=1)
    out = render_ci_addendum(ci)
    assert "In progress: 1" in out
