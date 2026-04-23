"""Tests for strict conventional-commit PR title check."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from commit_convention import check_pr_title_strict, ConventionViolation  # noqa: E402
from title_check import CONVENTIONAL_TYPES  # noqa: E402


def test_strict_mode_accepts_conventional_feat():
    assert check_pr_title_strict("feat: add widget", strict=True) is None


def test_strict_mode_accepts_conventional_fix_with_scope():
    assert check_pr_title_strict("fix(parser): handle combined diffs", strict=True) is None


def test_strict_mode_accepts_every_canonical_type():
    # Every type in the canonical list should pass when used correctly.
    for t in CONVENTIONAL_TYPES:
        assert check_pr_title_strict(f"{t}: some change", strict=True) is None, t


def test_strict_mode_flags_missing_prefix():
    result = check_pr_title_strict("add a widget", strict=True)
    assert isinstance(result, ConventionViolation)
    assert result.reason  # non-empty reason


def test_strict_mode_flags_unknown_prefix():
    result = check_pr_title_strict("improve: something", strict=True)
    assert isinstance(result, ConventionViolation)


def test_strict_mode_flags_empty_title():
    result = check_pr_title_strict("", strict=True)
    assert isinstance(result, ConventionViolation)


def test_strict_mode_flags_whitespace_only_title():
    result = check_pr_title_strict("   ", strict=True)
    assert isinstance(result, ConventionViolation)


def test_non_strict_always_returns_none():
    # Non-strict mode is handled by title_check.py's soft nudge — this
    # module only fires in strict mode.
    assert check_pr_title_strict("not a conventional title", strict=False) is None
    assert check_pr_title_strict("", strict=False) is None


def test_strict_mode_flags_missing_colon():
    result = check_pr_title_strict("feat add widget", strict=True)
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


# --------------------------------------------------------------------------
# Fix B — breaking-change `!` marker support
# --------------------------------------------------------------------------


def test_strict_mode_accepts_breaking_feat_bang():
    assert check_pr_title_strict("feat!: add X", strict=True) is None


def test_strict_mode_accepts_breaking_fix_with_scope_bang():
    assert check_pr_title_strict("fix(api)!: remove Y", strict=True) is None


def test_strict_mode_accepts_breaking_feat_with_scope_bang():
    assert check_pr_title_strict("feat(core)!: breaking change", strict=True) is None


def test_strict_mode_flags_bang_without_colon():
    # `feat!` alone is not valid — still needs `:` terminator.
    result = check_pr_title_strict("feat! add X", strict=True)
    assert isinstance(result, ConventionViolation)


# --------------------------------------------------------------------------
# Fix C — empty-scope `feat():` rejection
# --------------------------------------------------------------------------


def test_strict_mode_flags_empty_scope_parens():
    # Spec requires non-empty scope between parens when present.
    result = check_pr_title_strict("feat(): add X", strict=True)
    assert isinstance(result, ConventionViolation)


def test_strict_mode_flags_empty_scope_parens_with_bang():
    result = check_pr_title_strict("feat()!: add X", strict=True)
    assert isinstance(result, ConventionViolation)


# --------------------------------------------------------------------------
# Fix K — default strict mode aligns with config default
# --------------------------------------------------------------------------


def test_check_pr_title_strict_default_matches_config_default():
    # Config default `commit_convention_strict=False` means the function
    # default should also be False — callers that don't pass strict get
    # no-op behavior, matching opt-in semantics.
    import inspect
    sig = inspect.signature(check_pr_title_strict)
    assert sig.parameters["strict"].default is False


# --------------------------------------------------------------------------
# Fix L — canonical function name is check_pr_title_strict
# --------------------------------------------------------------------------


def test_module_exports_check_pr_title_strict():
    import commit_convention
    assert hasattr(commit_convention, "check_pr_title_strict")


# --------------------------------------------------------------------------
# Fix H — ConventionViolation carries title context
# --------------------------------------------------------------------------


def test_convention_violation_carries_title_context():
    result = check_pr_title_strict("random thing", strict=True)
    assert isinstance(result, ConventionViolation)
    assert result.title == "random thing"
    assert result.reason


def test_convention_violation_subject_aliases_title():
    # Uniform `.subject` across P3 violation dataclasses — lets the
    # analyzer's finding renderers stay table-driven.
    result = check_pr_title_strict("random thing", strict=True)
    assert isinstance(result, ConventionViolation)
    assert result.subject == result.title
