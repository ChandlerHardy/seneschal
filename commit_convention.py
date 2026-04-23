"""Strict conventional-commit PR-title check.

Distinct from `title_check.py`, which does a *soft* nudge toward
conventional-commit style. This module is the opt-in *strict* variant:
if a repo configures `commit_convention_strict: true` in `.seneschal.yml`,
any non-conforming PR title surfaces as a WARNING finding.

Imports `CONVENTIONAL_TYPES` from `title_check` to keep the canonical
type list in exactly one place.

Accepted shapes (per Conventional Commits spec):
    type: message
    type(scope): message          # scope must be non-empty
    type!: message                # `!` marks a breaking change
    type(scope)!: message
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from title_check import CONVENTIONAL_TYPES


@dataclass
class ConventionViolation:
    """Violation context.

    `title` is the offending PR title as submitted (post-strip). `reason`
    is a human-readable explanation. Callers (e.g. `_convention_to_finding`
    in analyzer.py) can choose to render one or both — having both on the
    dataclass keeps the renderer side plain and symmetric with sibling
    violation shapes (LicenseViolation, BranchNameViolation).
    """

    title: str
    reason: str

    @property
    def subject(self) -> str:
        """Uniform accessor across P3 violation dataclasses (see also
        LicenseViolation.subject, BranchNameViolation.subject). Lets
        analyzer's finding renderers stay table-driven instead of
        copy-pasting per-field extraction."""
        return self.title


def check_pr_title_strict(
    title: Optional[str],
    strict: bool = False,
) -> Optional[ConventionViolation]:
    """Check whether a PR title conforms to conventional-commit style.

    Args:
        title: The PR title to check.
        strict: Only fire in strict mode. Defaults to False to match the
            `StandardsConfig.commit_convention_strict` default — callers
            that don't pass it opt OUT of the check entirely. In non-
            strict mode the caller should rely on `title_check.check_title`
            for the soft nudge instead — this module returns None so it
            can be wired in unconditionally without double-reporting.

    Returns:
        `None` if the title is acceptable (or `strict=False`),
        `ConventionViolation` with the offending title and a reason
        otherwise.
    """
    if not strict:
        return None

    if title is None or not title.strip():
        return ConventionViolation(title=title or "", reason="PR title is empty")

    original = title.strip()
    bare = original.lower()

    # Accept:
    #  - `type:`
    #  - `type!:`  (breaking)
    #  - `type(scope):`
    #  - `type(scope)!:`
    # Scope when parens are present must be non-empty.
    for t in CONVENTIONAL_TYPES:
        # Unsscoped forms first.
        if bare.startswith(f"{t}:") or bare.startswith(f"{t}!:"):
            return None
        # Scoped forms: `type(scope):` or `type(scope)!:`
        if bare.startswith(f"{t}("):
            # Must close the paren and be followed by `:` or `!:`.
            closing = bare.find(")")
            if closing == -1:
                continue
            # Reject empty scope: `type():` or `type()!:` are not valid —
            # the spec says scope is a non-empty noun describing a section.
            scope_slice = bare[len(t) + 1 : closing]
            if not scope_slice.strip():
                continue
            tail = bare[closing + 1 :]
            if tail.startswith(":") or tail.startswith("!:"):
                return None
            continue

    accepted = ", ".join(CONVENTIONAL_TYPES)
    return ConventionViolation(
        title=original,
        reason=(
            f"PR title does not match conventional-commit style. "
            f"Expected one of: {accepted} (e.g. `feat: ...`, `feat!: ...`, "
            f"or `fix(scope): ...`)."
        ),
    )
