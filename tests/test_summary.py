"""Tests for diff summary generator."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from risk import PRFile  # noqa: E402
from summary import summarize_diff  # noqa: E402


def f(name, adds=10, dels=5, status="modified"):
    return PRFile(filename=name, additions=adds, deletions=dels, status=status)


def test_empty_pr():
    assert "empty" in summarize_diff([]).lower()


def test_mentions_code_and_tests():
    files = [
        f("src/foo.py", 40, 10),
        f("tests/test_foo.py", 30, 0, status="added"),
    ]
    out = summarize_diff(files)
    assert "code" in out
    assert "tests" in out


def test_reports_new_and_deleted_counts():
    files = [
        f("src/a.py", 10, 0, status="added"),
        f("src/b.py", 0, 20, status="removed"),
        f("src/c.py", 5, 5),
    ]
    out = summarize_diff(files)
    assert "1 new" in out
    assert "1 deleted" in out


def test_docs_categorized():
    files = [f("README.md", 5, 2)]
    out = summarize_diff(files)
    assert "docs" in out


def test_infra_categorized():
    files = [f(".github/workflows/ci.yml", 5, 2)]
    out = summarize_diff(files)
    assert "infra" in out


def test_totals_reported():
    files = [f("a.py", 100, 50)]
    out = summarize_diff(files)
    assert "+100" in out
    assert "50" in out
