"""Strict conventional-commit PR-title check.

Distinct from `title_check.py`, which does a *soft* nudge toward
conventional-commit style. This module is the opt-in *strict* variant:
if a repo configures `commit_convention_strict: true` in `.seneschal.yml`,
any non-conforming PR title surfaces as a WARNING finding.

Imports `CONVENTIONAL_TYPES` from `title_check` to keep the canonical
type list in exactly one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from title_check import CONVENTIONAL_TYPES


@dataclass
class ConventionViolation:
    reason: str


def check_pr_title(title: Optional[str], strict: bool = True) -> Optional[ConventionViolation]:
    """Check whether a PR title conforms to conventional-commit style.

    Args:
        title: The PR title to check.
        strict: Only fire in strict mode. In non-strict mode the caller
            should rely on `title_check.check_title` for the soft nudge
            instead — this module returns None so it can be wired in
            unconditionally without double-reporting.

    Returns:
        `None` if the title is acceptable (or `strict=False`),
        `ConventionViolation` with a human-readable reason otherwise.
    """
    if not strict:
        return None

    if not title or not title.strip():
        return ConventionViolation(reason="PR title is empty")

    bare = title.strip().lower()

    # Accept `type: ...` and `type(scope): ...`
    for t in CONVENTIONAL_TYPES:
        if bare.startswith(f"{t}:") or bare.startswith(f"{t}("):
            # For `type(`, require a matching `):` so we don't accept
            # half-formed scopes like `feat(widget add thing`.
            if bare.startswith(f"{t}("):
                # Must close the paren AND be followed by `:`
                closing = bare.find(")")
                if closing == -1 or closing + 1 >= len(bare) or bare[closing + 1] != ":":
                    continue
            return None

    accepted = ", ".join(CONVENTIONAL_TYPES)
    return ConventionViolation(
        reason=(
            f"PR title does not match conventional-commit style. "
            f"Expected one of: {accepted} (e.g. `feat: ...` or `fix(scope): ...`)."
        )
    )
