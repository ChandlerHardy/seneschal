"""Tests for branch-name regex match."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from branch_naming import check_branch_name, BranchNameViolation  # noqa: E402


def test_matching_pattern_passes():
    assert check_branch_name("feat/add-widget", [r"^feat/", r"^fix/"]) is None


def test_matching_fix_prefix_passes():
    assert check_branch_name("fix/bug-123", [r"^feat/", r"^fix/"]) is None


def test_non_matching_branch_flagged():
    result = check_branch_name("random-branch", [r"^feat/", r"^fix/"])
    assert isinstance(result, BranchNameViolation)
    assert result.head_ref == "random-branch"
    assert result.reason


def test_empty_patterns_returns_none_feature_disabled():
    # Empty pattern list = feature OFF; don't flag anything.
    assert check_branch_name("any-branch-name", []) is None
    assert check_branch_name("", []) is None


def test_invalid_regex_pattern_skipped_gracefully():
    # Bad regex should not crash the whole check. Good pattern still fires.
    result = check_branch_name("random", [r"[invalid(regex", r"^feat/"])
    # Still matches the good pattern? No — "random" doesn't start with feat/.
    # But the bad regex must not crash.
    assert isinstance(result, BranchNameViolation)


def test_any_pattern_match_valid_branch():
    # Multiple patterns — ANY match counts as valid.
    assert check_branch_name("chore/cleanup", [r"^feat/", r"^fix/", r"^chore/"]) is None


def test_empty_head_ref_with_patterns():
    # If operator set patterns, an empty head_ref should fail.
    result = check_branch_name("", [r"^feat/"])
    assert isinstance(result, BranchNameViolation)


def test_none_head_ref_returns_none_when_patterns_present():
    # Defensive: if head_ref comes through as None (shouldn't happen
    # but we are defensive), we skip rather than crash.
    assert check_branch_name(None, [r"^feat/"]) is None  # type: ignore[arg-type]


def test_all_bad_patterns_no_crash():
    # If EVERY pattern is invalid, the check should fall through without
    # crashing — nothing to match against.
    result = check_branch_name("random", [r"[bad", r"(no-close"])
    # All patterns invalid → effectively no patterns → None
    assert result is None
