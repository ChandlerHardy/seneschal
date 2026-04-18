"""Tests for PR title quality checker."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from title_check import check_title  # noqa: E402


def test_empty_title_warns():
    assert check_title("").level == "warning"
    assert check_title(None).level == "warning"
    assert check_title("   ").level == "warning"


def test_very_short_title_warns():
    assert check_title("fix").level == "warning"
    assert check_title("wip").level == "warning"


def test_vague_single_word_warns():
    assert check_title("update").level == "warning"
    assert check_title("changes").level == "warning"
    assert check_title("cleanup").level == "warning"


def test_short_no_prefix_is_nit():
    report = check_title("foo bar")
    assert report.level in ("nit", "warning")


def test_good_conventional_title_ok():
    assert check_title("feat: add user authentication flow").level == "ok"
    assert check_title("fix(api): handle missing session token").level == "ok"
    assert check_title("refactor: extract storage layer").level == "ok"


def test_long_title_without_prefix_ok():
    assert check_title("Add support for exporting user data as CSV").level == "ok"


def test_wip_prefix_nit():
    report = check_title("wip added stuff")
    assert report.level in ("nit", "warning")
