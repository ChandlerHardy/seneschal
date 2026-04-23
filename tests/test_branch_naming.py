"""Tests for branch-name regex match."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from branch_naming import check_branch_name, BranchNameViolation  # noqa: E402


def test_fullmatch_rejects_partial_prefix_overlap():
    # Fix D: `^feat/add` should NOT match `feat/addendum-sneaky-branch`.
    # The pattern is intended as an exact-shape match. `re.match` would
    # pass; `re.fullmatch` correctly rejects.
    result = check_branch_name("feat/addendum-sneaky-branch", [r"^feat/add"])
    assert isinstance(result, BranchNameViolation)


def test_fullmatch_accepts_exact_match():
    # Sanity check that fullmatch still accepts the intended-shape branch.
    assert check_branch_name("feat/add", [r"^feat/add$"]) is None


def test_violation_reason_omits_bare_head_ref_duplication(capsys):
    # Fix H: violation.reason should not embed head_ref; head_ref is its
    # own field. The renderer in analyzer.py composes the message.
    result = check_branch_name("weird-branch", [r"^feat/"])
    assert isinstance(result, BranchNameViolation)
    assert result.head_ref == "weird-branch"
    assert result.reason
    # reason should talk about expected patterns, not repeat the head_ref.
    assert "weird-branch" not in result.reason


def test_branch_violation_subject_aliases_head_ref():
    # Uniform `.subject` across P3 violation dataclasses — lets the
    # analyzer's finding renderers stay table-driven.
    result = check_branch_name("weird-branch", [r"^feat/"])
    assert isinstance(result, BranchNameViolation)
    assert result.subject == result.head_ref


def test_warns_when_patterns_present_but_head_ref_missing(capsys):
    # Fix N: `head_ref=None` with configured patterns should emit a stderr
    # warning so the silent no-op is visible. Still returns None (doesn't
    # break the check, just surfaces operator-visible signal).
    result = check_branch_name(None, [r"^feat/"])  # type: ignore[arg-type]
    assert result is None
    captured = capsys.readouterr()
    assert "[seneschal]" in captured.err
    assert "head_ref" in captured.err


def test_no_warning_when_no_patterns_and_no_head_ref(capsys):
    # Feature off (no patterns) + no head_ref = totally silent.
    result = check_branch_name(None, [])  # type: ignore[arg-type]
    assert result is None
    captured = capsys.readouterr()
    assert captured.err == ""


def test_no_warning_when_patterns_present_and_head_ref_present(capsys):
    result = check_branch_name("feat/thing", [r"^feat/.*"])
    assert result is None
    captured = capsys.readouterr()
    assert captured.err == ""


def test_max_branch_patterns_constant_exists():
    # Fix M: dedicated cap, not reused MAX_IGNORE_PATHS.
    import repo_config
    assert hasattr(repo_config, "MAX_BRANCH_PATTERNS")
    assert repo_config.MAX_BRANCH_PATTERNS == 20


def test_matching_pattern_passes():
    # Patterns must fullmatch the entire ref (fix D). Operators who want
    # a prefix match write `^feat/.*`.
    assert check_branch_name("feat/add-widget", [r"^feat/.*", r"^fix/.*"]) is None


def test_matching_fix_prefix_passes():
    assert check_branch_name("fix/bug-123", [r"^feat/.*", r"^fix/.*"]) is None


def test_non_matching_branch_flagged():
    result = check_branch_name("random-branch", [r"^feat/.*", r"^fix/.*"])
    assert isinstance(result, BranchNameViolation)
    assert result.head_ref == "random-branch"
    assert result.reason


def test_empty_patterns_returns_none_feature_disabled():
    # Empty pattern list = feature OFF; don't flag anything.
    assert check_branch_name("any-branch-name", []) is None
    assert check_branch_name("", []) is None


def test_invalid_regex_pattern_skipped_gracefully():
    # Bad regex should not crash the whole check. Good pattern still fires.
    result = check_branch_name("random", [r"[invalid(regex", r"^feat/.*"])
    # Still matches the good pattern? No — "random" doesn't start with feat/.
    # But the bad regex must not crash.
    assert isinstance(result, BranchNameViolation)


def test_any_pattern_match_valid_branch():
    # Multiple patterns — ANY match counts as valid.
    assert check_branch_name(
        "chore/cleanup",
        [r"^feat/.*", r"^fix/.*", r"^chore/.*"],
    ) is None


def test_empty_head_ref_with_patterns_returns_none_with_warning(capsys):
    # Fix N: empty head_ref is treated as "missing ref" — return None and
    # emit a stderr warning rather than silently dropping or crying wolf.
    result = check_branch_name("", [r"^feat/"])
    assert result is None
    captured = capsys.readouterr()
    assert "[seneschal]" in captured.err


def test_none_head_ref_returns_none_when_patterns_present():
    # Defensive: if head_ref comes through as None (shouldn't happen
    # but we are defensive), we skip rather than crash. Stderr warning
    # is validated in test_warns_when_patterns_present_but_head_ref_missing.
    assert check_branch_name(None, [r"^feat/"]) is None  # type: ignore[arg-type]


def test_all_bad_patterns_no_crash():
    # If EVERY pattern is invalid, the check should fall through without
    # crashing — nothing to match against.
    result = check_branch_name("random", [r"[bad", r"(no-close"])
    # All patterns invalid → effectively no patterns → None
    assert result is None
