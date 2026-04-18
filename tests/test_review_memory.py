"""Tests for review memory."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from review_memory import MEMORY_FILENAME, ReviewMemory, load, parse  # noqa: E402


def test_parse_rules_from_markdown():
    raw = """# header
- rule one
- rule two
  - nested (ignored)
- rule three
"""
    rules = parse(raw)
    assert "rule one" in rules
    assert "rule two" in rules
    assert "rule three" in rules


def test_parse_skips_comments_and_blanks():
    raw = """# title
# comment

- keep me
"""
    rules = parse(raw)
    assert rules == ["keep me"]


def test_empty_memory_prompt_block_is_empty():
    assert ReviewMemory().prompt_block() == ""


def test_non_empty_memory_prompt_block():
    m = ReviewMemory(rules=["Always use Realm for storage", "Prefer cobra for CLI"])
    block = m.prompt_block()
    assert "Realm" in block
    assert "cobra" in block
    assert "Repo-specific" in block


def test_add_rule_deduplicates():
    m = ReviewMemory()
    assert m.add("Use Realm") is True
    assert m.add("Use Realm") is False  # dup
    assert m.add("use realm") is False  # case-insensitive dup
    assert len(m.rules) == 1


def test_add_empty_rule_ignored():
    m = ReviewMemory()
    assert m.add("") is False
    assert m.add("   ") is False
    assert m.rules == []


def test_load_missing_file_returns_empty():
    with tempfile.TemporaryDirectory() as d:
        m = load(d)
        assert m.is_empty
        assert m.path == os.path.join(d, MEMORY_FILENAME)


def test_load_existing_file():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, MEMORY_FILENAME)
        with open(path, "w") as fh:
            fh.write("- rule one\n- rule two\n")
        m = load(d)
        assert "rule one" in m.rules
        assert "rule two" in m.rules


def test_save_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        m = load(d)
        m.add("Always prefer explicit error types")
        m.add("Use structured logging")
        m.save()
        m2 = load(d)
        assert "Always prefer explicit error types" in m2.rules
        assert "Use structured logging" in m2.rules


def test_max_rules_capped():
    from review_memory import MAX_RULES
    m = ReviewMemory()
    for i in range(MAX_RULES + 10):
        m.add(f"rule {i}")
    assert len(m.rules) == MAX_RULES
