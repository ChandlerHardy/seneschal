"""Tests for Finding, FindingSet, and severity sorting."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from findings import Finding, FindingSet, Severity  # noqa: E402


def test_severity_ordering():
    assert Severity.BLOCKER < Severity.WARNING < Severity.NIT < Severity.INFO


def test_severity_labels():
    assert Severity.BLOCKER.label == "BLOCKER"
    assert Severity.WARNING.label == "WARNING"


def test_finding_render_includes_category_and_title():
    f = Finding(severity=Severity.WARNING, category="risk", title="Large diff")
    text = f.render()
    assert "WARNING" in text
    assert "[risk]" in text
    assert "Large diff" in text


def test_finding_set_sorted_blockers_first():
    fs = FindingSet()
    fs.add(Finding(severity=Severity.NIT, category="a", title="third"))
    fs.add(Finding(severity=Severity.BLOCKER, category="b", title="first"))
    fs.add(Finding(severity=Severity.WARNING, category="c", title="second"))
    ordered = fs.sorted()
    assert ordered[0].title == "first"
    assert ordered[1].title == "second"
    assert ordered[2].title == "third"


def test_headline_clean():
    assert FindingSet().headline() == "clean"


def test_headline_with_blockers():
    fs = FindingSet()
    fs.add(Finding(severity=Severity.BLOCKER, category="a", title="x"))
    fs.add(Finding(severity=Severity.WARNING, category="b", title="y"))
    fs.add(Finding(severity=Severity.NIT, category="c", title="z"))
    assert "1 blocker" in fs.headline()


def test_headline_warnings_only():
    fs = FindingSet()
    fs.add(Finding(severity=Severity.WARNING, category="a", title="x"))
    assert "warning" in fs.headline()
    assert "blocker" not in fs.headline()


def test_has_blockers():
    fs = FindingSet()
    assert fs.has_blockers() is False
    fs.add(Finding(severity=Severity.BLOCKER, category="secret", title="x"))
    assert fs.has_blockers() is True


def test_render_grouped_shows_sections():
    fs = FindingSet()
    fs.add(Finding(severity=Severity.BLOCKER, category="secret", title="leaked .env"))
    fs.add(Finding(severity=Severity.WARNING, category="risk", title="large"))
    text = fs.render_grouped()
    assert "BLOCKER" in text
    assert "WARNING" in text
    assert "leaked .env" in text
    assert "large" in text


def test_render_grouped_empty():
    assert "no automated" in FindingSet().render_grouped().lower()
