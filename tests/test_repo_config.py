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


def test_load_from_path_pins_utf8_encoding(tmp_path, monkeypatch):
    """Round-3 Warning companion to fs_safety: `open(path, "r")` without
    `encoding=` decodes via `locale.getpreferredencoding()`. On `LANG=C`
    a `.seneschal.yml` carrying a Unicode rule string would raise
    UnicodeDecodeError → bare `except Exception` swallows it → config
    silently falls back to all-defaults (no rules applied).

    Verify the open() call pins `encoding="utf-8"`."""
    from unittest.mock import patch

    cfg_path = tmp_path / ".seneschal.yml"
    cfg_path.write_text(
        'rules:\n  - "Prefer résumé over CV, café ☕ tokens"\n',
        encoding="utf-8",
    )

    captured = {}
    real_open = open

    def _spy_open(path, *args, **kwargs):
        if str(path).endswith(".seneschal.yml"):
            captured["encoding"] = kwargs.get("encoding")
        return real_open(path, *args, **kwargs)

    with patch("builtins.open", side_effect=_spy_open):
        config = load_from_path(str(cfg_path))

    assert captured.get("encoding") == "utf-8", (
        "repo_config.load_from_path opened the config without "
        "encoding='utf-8' — under LANG=C non-ASCII rule strings would "
        "crash the parse and silently drop all config."
    )
    # And the rule loaded correctly.
    assert len(config.rules) == 1
    assert "résumé" in config.rules[0]


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


# --------------------------------------------------------------------------
# PostMergeConfig (P1)
# --------------------------------------------------------------------------


def test_default_post_merge_off():
    from repo_config import PostMergeConfig
    cfg = parse_config("")
    assert isinstance(cfg.post_merge, PostMergeConfig)
    assert cfg.post_merge.changelog is False
    assert cfg.post_merge.followups is False
    assert cfg.post_merge.release_threshold == ""
    assert cfg.post_merge.changelog_path == "CHANGELOG.md"
    assert cfg.post_merge.release_base_branch == "main"
    assert cfg.post_merge.release_pr_draft is True
    assert cfg.post_merge.followup_label == "seneschal-followup"


def test_post_merge_changelog_on():
    raw = """
post_merge:
  changelog: true
"""
    cfg = parse_config(raw)
    assert cfg.post_merge.changelog is True
    assert cfg.post_merge.followups is False


def test_post_merge_followups_on_with_label():
    raw = """
post_merge:
  followups: true
  followup_label: needs-investigation
"""
    cfg = parse_config(raw)
    assert cfg.post_merge.followups is True
    assert cfg.post_merge.followup_label == "needs-investigation"


def test_post_merge_release_threshold_valid():
    for val in ("patch", "minor", "major"):
        raw = f"post_merge:\n  release_threshold: {val}\n"
        cfg = parse_config(raw)
        assert cfg.post_merge.release_threshold == val


def test_post_merge_release_threshold_invalid_falls_back():
    raw = "post_merge:\n  release_threshold: bogus\n"
    cfg = parse_config(raw)
    assert cfg.post_merge.release_threshold == ""


def test_post_merge_unknown_keys_ignored():
    raw = """
post_merge:
  changelog: true
  bogus_key: whatever
"""
    cfg = parse_config(raw)
    assert cfg.post_merge.changelog is True


def test_post_merge_release_base_branch_override():
    raw = "post_merge:\n  release_base_branch: develop\n"
    cfg = parse_config(raw)
    assert cfg.post_merge.release_base_branch == "develop"


def test_post_merge_changelog_path_override():
    raw = "post_merge:\n  changelog_path: docs/CHANGELOG.md\n"
    cfg = parse_config(raw)
    assert cfg.post_merge.changelog_path == "docs/CHANGELOG.md"


def test_post_merge_release_pr_draft_off():
    raw = "post_merge:\n  release_pr_draft: false\n"
    cfg = parse_config(raw)
    assert cfg.post_merge.release_pr_draft is False


def test_post_merge_block_invalid_type_ignored():
    raw = "post_merge: not-a-dict\n"
    cfg = parse_config(raw)
    # Falls back to defaults silently.
    assert cfg.post_merge.changelog is False


# --------------------------------------------------------------------------
# Path-traversal + branch-name rejection (security)
# --------------------------------------------------------------------------


def test_changelog_path_rejects_parent_traversal():
    raw = "post_merge:\n  changelog_path: ../.github/workflows/attack.yml\n"
    cfg = parse_config(raw)
    # Falls back to the default rather than honoring the attacker's value.
    assert cfg.post_merge.changelog_path == "CHANGELOG.md"


def test_changelog_path_rejects_absolute_path():
    raw = "post_merge:\n  changelog_path: /etc/passwd\n"
    cfg = parse_config(raw)
    assert cfg.post_merge.changelog_path == "CHANGELOG.md"


def test_changelog_path_rejects_backslashes():
    raw = "post_merge:\n  changelog_path: 'docs\\\\..\\\\CHANGELOG.md'\n"
    cfg = parse_config(raw)
    assert cfg.post_merge.changelog_path == "CHANGELOG.md"


def test_changelog_path_accepts_nested_subdir():
    raw = "post_merge:\n  changelog_path: docs/notes/CHANGELOG.md\n"
    cfg = parse_config(raw)
    assert cfg.post_merge.changelog_path == "docs/notes/CHANGELOG.md"


def test_changelog_path_rejects_mid_path_traversal():
    raw = "post_merge:\n  changelog_path: docs/../../../etc/passwd\n"
    cfg = parse_config(raw)
    assert cfg.post_merge.changelog_path == "CHANGELOG.md"


def test_release_base_branch_rejects_slash_injection():
    raw = "post_merge:\n  release_base_branch: main?admin=1\n"
    cfg = parse_config(raw)
    assert cfg.post_merge.release_base_branch == "main"


def test_release_base_branch_rejects_spaces():
    raw = "post_merge:\n  release_base_branch: 'main release'\n"
    cfg = parse_config(raw)
    assert cfg.post_merge.release_base_branch == "main"


def test_release_base_branch_rejects_dotdot():
    raw = "post_merge:\n  release_base_branch: 'foo..bar'\n"
    cfg = parse_config(raw)
    assert cfg.post_merge.release_base_branch == "main"


def test_release_base_branch_accepts_valid_name():
    raw = "post_merge:\n  release_base_branch: release/v2.x\n"
    cfg = parse_config(raw)
    assert cfg.post_merge.release_base_branch == "release/v2.x"


# --------------------------------------------------------------------------
# Blocker 1: deny-list for sensitive changelog_path values
#
# Even after traversal rejection, a `changelog_path: CODEOWNERS` (or
# `.github/workflows/ci.yml`) is a valid repo-relative path that would
# let a PR author redirect Seneschal's auto-commit at a file protected
# by branch rules. The fix is a case-insensitive basename + top-segment
# deny-list in `safe_changelog_path`.
# --------------------------------------------------------------------------


def test_changelog_path_rejects_github_dir():
    raw = "post_merge:\n  changelog_path: .github/workflows/ci.yml\n"
    cfg = parse_config(raw)
    assert cfg.post_merge.changelog_path == "CHANGELOG.md"


def test_changelog_path_rejects_github_root_codeowners():
    # `.github/CODEOWNERS` is the standard location for reviewers.
    raw = "post_merge:\n  changelog_path: .github/CODEOWNERS\n"
    cfg = parse_config(raw)
    assert cfg.post_merge.changelog_path == "CHANGELOG.md"


def test_changelog_path_rejects_root_codeowners_case_insensitive():
    raw = "post_merge:\n  changelog_path: CODEOWNERS\n"
    cfg = parse_config(raw)
    assert cfg.post_merge.changelog_path == "CHANGELOG.md"
    raw = "post_merge:\n  changelog_path: Codeowners\n"
    cfg = parse_config(raw)
    assert cfg.post_merge.changelog_path == "CHANGELOG.md"
    raw = "post_merge:\n  changelog_path: codeowners\n"
    cfg = parse_config(raw)
    assert cfg.post_merge.changelog_path == "CHANGELOG.md"


def test_changelog_path_rejects_security_md():
    raw = "post_merge:\n  changelog_path: SECURITY.md\n"
    cfg = parse_config(raw)
    assert cfg.post_merge.changelog_path == "CHANGELOG.md"


def test_changelog_path_rejects_gitattributes():
    raw = "post_merge:\n  changelog_path: .gitattributes\n"
    cfg = parse_config(raw)
    assert cfg.post_merge.changelog_path == "CHANGELOG.md"


def test_changelog_path_rejects_gitignore():
    raw = "post_merge:\n  changelog_path: .gitignore\n"
    cfg = parse_config(raw)
    assert cfg.post_merge.changelog_path == "CHANGELOG.md"


def test_changelog_path_rejects_license_variants():
    for name in ("LICENSE", "LICENSE.md", "LICENSE.txt", "license", "License.MD"):
        raw = f"post_merge:\n  changelog_path: {name}\n"
        cfg = parse_config(raw)
        assert cfg.post_merge.changelog_path == "CHANGELOG.md", f"should reject {name!r}"


def test_changelog_path_rejects_env_file():
    raw = "post_merge:\n  changelog_path: .env\n"
    cfg = parse_config(raw)
    assert cfg.post_merge.changelog_path == "CHANGELOG.md"


def test_changelog_path_rejects_dockerfile():
    raw = "post_merge:\n  changelog_path: Dockerfile\n"
    cfg = parse_config(raw)
    assert cfg.post_merge.changelog_path == "CHANGELOG.md"
    raw = "post_merge:\n  changelog_path: docker-compose.yml\n"
    cfg = parse_config(raw)
    assert cfg.post_merge.changelog_path == "CHANGELOG.md"
    raw = "post_merge:\n  changelog_path: docker-compose.yaml\n"
    cfg = parse_config(raw)
    assert cfg.post_merge.changelog_path == "CHANGELOG.md"


def test_changelog_path_rejects_git_dir_segment():
    # Nested `.git/HEAD` — crafted to avoid the `.github/` startswith check.
    raw = "post_merge:\n  changelog_path: .git/HEAD\n"
    cfg = parse_config(raw)
    assert cfg.post_merge.changelog_path == "CHANGELOG.md"


def test_changelog_path_rejects_nested_git_segment():
    # Any segment equal to `.git` anywhere in the path is suspicious.
    raw = "post_merge:\n  changelog_path: docs/.git/HEAD\n"
    cfg = parse_config(raw)
    assert cfg.post_merge.changelog_path == "CHANGELOG.md"


def test_changelog_path_accepts_docs_subdir():
    # Make sure the deny-list doesn't bleed over into ordinary paths.
    raw = "post_merge:\n  changelog_path: docs/changes/HISTORY.md\n"
    cfg = parse_config(raw)
    assert cfg.post_merge.changelog_path == "docs/changes/HISTORY.md"


def test_sensitive_path_sets_are_inspectable():
    from repo_config import _SENSITIVE_FILENAMES, _SENSITIVE_PATH_SEGMENTS

    # Sets must be frozensets so operators can inspect without mutation risk.
    assert isinstance(_SENSITIVE_FILENAMES, frozenset)
    assert isinstance(_SENSITIVE_PATH_SEGMENTS, frozenset)
    assert "codeowners" in _SENSITIVE_FILENAMES
    assert ".github" in _SENSITIVE_PATH_SEGMENTS
