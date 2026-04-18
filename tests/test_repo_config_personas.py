"""Tests for personas: field in RepoConfig parsing."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repo_config import RepoConfig, parse_config  # noqa: E402


def test_personas_empty_by_default():
    cfg = RepoConfig()
    assert cfg.personas == []


def test_parse_personas_with_builtins():
    raw = """
personas:
  - builtin: architect
  - builtin: security
"""
    cfg = parse_config(raw)
    assert cfg.personas == [
        {"builtin": "architect"},
        {"builtin": "security"},
    ]


def test_parse_personas_mixed_builtin_and_file():
    raw = """
personas:
  - builtin: security
  - file: .seneschal/personas/hipaa.md
"""
    cfg = parse_config(raw)
    assert len(cfg.personas) == 2
    assert cfg.personas[0] == {"builtin": "security"}
    assert cfg.personas[1] == {"file": ".seneschal/personas/hipaa.md"}


def test_parse_personas_drops_malformed_entries():
    raw = """
personas:
  - builtin: architect
  - "just a string"
  - weird: thing
  - file: .seneschal/personas/perf.md
"""
    cfg = parse_config(raw)
    # Only "builtin" or "file" keys survive the filter
    assert len(cfg.personas) == 2
    assert cfg.personas[0] == {"builtin": "architect"}
    assert cfg.personas[1] == {"file": ".seneschal/personas/perf.md"}


def test_parse_personas_caps_list_at_10():
    entries = "\n".join(f"  - builtin: p{i}" for i in range(15))
    raw = f"personas:\n{entries}\n"
    cfg = parse_config(raw)
    assert len(cfg.personas) == 10


def test_parse_personas_non_list_ignored():
    raw = "personas: not-a-list\n"
    cfg = parse_config(raw)
    assert cfg.personas == []


def test_parse_personas_missing_field_ignored():
    # Confirm other fields still work when personas is absent.
    raw = """
rules:
  - Use Go modules
review_style: blunt
"""
    cfg = parse_config(raw)
    assert cfg.personas == []
    assert cfg.rules == ["Use Go modules"]
    assert cfg.review_style == "blunt"
