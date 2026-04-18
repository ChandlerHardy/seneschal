"""Tests for per-repo config loader."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repo_config import RepoConfig, load_from_path, load_from_repo, parse_config  # noqa: E402


def test_empty_config_is_default():
    config = parse_config("")
    assert config.rules == []
    assert config.ignore_paths == []
    assert config.max_risk_for_auto_fix == "high"
    assert config.review_style == "concise"


def test_parse_rules():
    raw = """
rules:
  - "Use Realm for storage"
  - "Prefer cobra over flag"
"""
    config = parse_config(raw)
    assert len(config.rules) == 2
    assert "Realm" in config.rules[0]


def test_parse_ignore_paths():
    raw = """
ignore_paths:
  - docs/
  - examples/
"""
    config = parse_config(raw)
    assert "docs/" in config.ignore_paths
    assert "examples/" in config.ignore_paths


def test_parse_max_risk():
    raw = "max_risk_for_auto_fix: medium\n"
    config = parse_config(raw)
    assert config.max_risk_for_auto_fix == "medium"


def test_invalid_max_risk_ignored():
    raw = "max_risk_for_auto_fix: whatever\n"
    config = parse_config(raw)
    assert config.max_risk_for_auto_fix == "high"


def test_review_style():
    raw = "review_style: blunt\n"
    config = parse_config(raw)
    assert config.review_style == "blunt"


def test_system_prompt_addendum_with_rules():
    config = RepoConfig(rules=["Rule A", "Rule B"])
    addendum = config.system_prompt_addendum()
    assert "Rule A" in addendum
    assert "Rule B" in addendum


def test_system_prompt_addendum_empty_when_default():
    config = RepoConfig()
    assert config.system_prompt_addendum() == ""


def test_blunt_style_includes_instruction():
    config = RepoConfig(review_style="blunt")
    addendum = config.system_prompt_addendum()
    assert "blunt" in addendum.lower()


def test_should_skip_file():
    config = RepoConfig(ignore_paths=["docs/", "examples/"])
    assert config.should_skip_file("docs/readme.md") is True
    assert config.should_skip_file("docs") is True
    assert config.should_skip_file("examples/foo.py") is True
    assert config.should_skip_file("src/foo.py") is False


def test_load_from_path_missing_file():
    config = load_from_path("/tmp/nonexistent-config-12345.yml")
    assert config.rules == []


def test_load_from_repo_finds_yml():
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, ".ch-code-reviewer.yml"), "w") as fh:
            fh.write("rules:\n  - \"Use Realm for storage\"\n")
        config = load_from_repo(d)
        assert len(config.rules) == 1
        assert "Realm" in config.rules[0]


def test_load_from_repo_no_config():
    with tempfile.TemporaryDirectory() as d:
        config = load_from_repo(d)
        assert config.rules == []


def test_load_from_repo_yaml_fallback():
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, ".ch-code-reviewer.yaml"), "w") as fh:
            fh.write("review_style: thorough\n")
        config = load_from_repo(d)
        assert config.review_style == "thorough"
