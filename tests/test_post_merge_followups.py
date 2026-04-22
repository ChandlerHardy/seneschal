"""Tests for post_merge.followups: pure parsing of [FOLLOWUP] markers."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from post_merge.followups import Followup, parse_followups  # noqa: E402


def test_parse_followups_finds_marker():
    body = "Some text\n- [FOLLOWUP] Do the thing\nMore text"
    result = parse_followups(body)
    assert len(result) == 1
    assert result[0].title == "Do the thing"


def test_parse_followups_case_insensitive():
    body = "- [followup] lowercase marker\n- [Followup] mixed case\n"
    result = parse_followups(body)
    assert len(result) == 2


def test_parse_followups_no_markers_returns_empty():
    assert parse_followups("Just a regular review body") == []
    assert parse_followups("") == []


def test_parse_followups_truncates_long_title():
    long_text = "x" * 200
    body = f"- [FOLLOWUP] {long_text}"
    result = parse_followups(body)
    assert len(result[0].title) == 100


def test_parse_followups_includes_source_line():
    body = "line 1\nline 2\n- [FOLLOWUP] thing\nline 4\n"
    result = parse_followups(body)
    # 1-based line index
    assert result[0].source_line == 3


def test_parse_followups_excerpt_includes_context():
    body = (
        "intro\n"
        "- [FOLLOWUP] do thing\n"
        "  context line 1\n"
        "  context line 2\n"
        "  context line 3\n"
        "  context line 4 — should be cut\n"
    )
    result = parse_followups(body)
    excerpt = result[0].body_excerpt
    assert "do thing" in excerpt
    assert "context line 1" in excerpt
    assert "context line 2" in excerpt
    assert "context line 3" in excerpt
    # Capped at 3 context lines after the marker line
    assert "context line 4" not in excerpt


def test_parse_followups_excerpt_capped_at_500_chars():
    body_lines = ["- [FOLLOWUP] head", "x" * 300, "y" * 300, "z" * 300]
    body = "\n".join(body_lines)
    result = parse_followups(body)
    assert len(result[0].body_excerpt) <= 500


def test_parse_followups_caps_at_10_with_rollup():
    lines = [f"- [FOLLOWUP] item {i}" for i in range(15)]
    body = "\n".join(lines)
    result = parse_followups(body)
    assert len(result) == 11  # 10 individual + 1 rollup
    assert result[10].title == "Additional follow-ups from review"
    # Rollup should mention the leftover items
    assert "item 10" in result[10].body_excerpt or "item 14" in result[10].body_excerpt


def test_parse_followups_exactly_10_no_rollup():
    lines = [f"- [FOLLOWUP] item {i}" for i in range(10)]
    body = "\n".join(lines)
    result = parse_followups(body)
    assert len(result) == 10
    # No rollup synthetic entry
    assert all("Additional follow-ups" not in f.title for f in result)


def test_followup_is_frozen_dataclass():
    f = Followup(title="t", body_excerpt="b", source_line=1)
    import pytest
    with pytest.raises((AttributeError, Exception)):
        f.title = "new"  # type: ignore[misc]


def test_parse_followups_indented_marker():
    body = "  - [FOLLOWUP] indented\n    - [FOLLOWUP] more indented\n"
    result = parse_followups(body)
    assert len(result) == 2
