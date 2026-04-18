"""Tests for scope-drift detector."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from risk import PRFile  # noqa: E402
from scope import detect_scope_drift, collect_top_level_dirs  # noqa: E402


def f(name):
    return PRFile(filename=name)


def test_single_dir_focused():
    files = [f("src/auth/login.py"), f("src/auth/session.py")]
    report = detect_scope_drift("fix: login bug", files)
    assert report.drifted is False
    assert report.top_level_dirs == ["src"]


def test_two_dirs_focused():
    files = [f("src/foo.py"), f("tests/test_foo.py")]
    report = detect_scope_drift("fix: foo", files)
    assert report.drifted is False


def test_three_dirs_with_narrow_title_drifts():
    files = [
        f("src/foo.py"),
        f("api/handler.go"),
        f("docs/howto.md"),
    ]
    report = detect_scope_drift("fix: login bug", files)
    assert report.drifted is True
    assert "3 unrelated areas" in report.reason
    assert "api" in report.top_level_dirs
    assert "docs" in report.top_level_dirs
    assert "src" in report.top_level_dirs


def test_refactor_title_prevents_drift():
    files = [f("a/x.py"), f("b/y.py"), f("c/z.py"), f("d/w.py")]
    report = detect_scope_drift("refactor: extract shared utils", files)
    assert report.drifted is False


def test_chore_title_prevents_drift():
    files = [f("a/x.py"), f("b/y.py"), f("c/z.py")]
    report = detect_scope_drift("chore: bump versions", files)
    assert report.drifted is False


def test_wip_title_prevents_drift():
    files = [f("a/x.py"), f("b/y.py"), f("c/z.py")]
    report = detect_scope_drift("WIP: in progress", files)
    assert report.drifted is False


def test_node_modules_ignored():
    files = [
        f("src/foo.py"),
        f("node_modules/pkg/index.js"),
        f("node_modules/pkg2/index.js"),
    ]
    report = detect_scope_drift("fix: bug", files)
    assert report.top_level_dirs == ["src"]
    assert report.drifted is False


def test_generated_dirs_ignored():
    files = [
        f("src/foo.py"),
        f("dist/foo.min.js"),
        f(".next/cache/a.dat"),
    ]
    report = detect_scope_drift("fix: bug", files)
    assert report.top_level_dirs == ["src"]


def test_root_files_do_not_count_as_dirs():
    files = [f("README.md"), f("package.json"), f("src/foo.py")]
    report = detect_scope_drift("fix: bug", files)
    assert report.top_level_dirs == ["src"]
    assert report.drifted is False


def test_collect_top_level_dirs_sorted_unique():
    files = [
        f("b/x.py"),
        f("a/y.py"),
        f("b/z.py"),
        f("c/w.py"),
    ]
    dirs = collect_top_level_dirs(files)
    assert dirs == ["a", "b", "c"]


def test_summary_focused():
    files = [f("src/foo.py")]
    report = detect_scope_drift("fix: foo", files)
    assert "focused" in report.summary().lower()


def test_summary_drifted():
    files = [f("a/x"), f("b/y"), f("c/z"), f("d/w")]
    report = detect_scope_drift("fix: bug", files)
    assert "drifted" in report.summary().lower()
