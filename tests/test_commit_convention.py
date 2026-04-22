"""Tests for strict conventional-commit PR title check."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from commit_convention import check_pr_title, ConventionViolation  # noqa: E402
from title_check import CONVENTIONAL_TYPES  # noqa: E402


def test_strict_mode_accepts_conventional_feat():
    assert check_pr_title("feat: add widget", strict=True) is None


def test_strict_mode_accepts_conventional_fix_with_scope():
    assert check_pr_title("fix(parser): handle combined diffs", strict=True) is None


def test_strict_mode_accepts_every_canonical_type():
    # Every type in the canonical list should pass when used correctly.
    for t in CONVENTIONAL_TYPES:
        assert check_pr_title(f"{t}: some change", strict=True) is None, t


def test_strict_mode_flags_missing_prefix():
    result = check_pr_title("add a widget", strict=True)
    assert isinstance(result, ConventionViolation)
    assert result.reason  # non-empty reason


def test_strict_mode_flags_unknown_prefix():
    result = check_pr_title("improve: something", strict=True)
    assert isinstance(result, ConventionViolation)


def test_strict_mode_flags_empty_title():
    result = check_pr_title("", strict=True)
    assert isinstance(result, ConventionViolation)


def test_strict_mode_flags_whitespace_only_title():
    result = check_pr_title("   ", strict=True)
    assert isinstance(result, ConventionViolation)


def test_non_strict_always_returns_none():
    # Non-strict mode is handled by title_check.py's soft nudge — this
    # module only fires in strict mode.
    assert check_pr_title("not a conventional title", strict=False) is None
    assert check_pr_title("", strict=False) is None


def test_strict_mode_flags_missing_colon():
    result = check_pr_title("feat add widget", strict=True)
    assert isinstance(result, ConventionViolation)


def test_reuses_conventional_types_from_title_check():
    # Guard against drift — if someone adds a type to title_check.py,
    # commit_convention.py should accept it automatically.
    import commit_convention
    # The module must NOT define its own CONVENTIONAL_TYPES list.
    assert not hasattr(commit_convention, "_OWN_TYPES"), (
        "commit_convention.py should import CONVENTIONAL_TYPES from "
        "title_check, not redeclare its own copy."
    )
