"""Branch-name regex convention check.

Given a PR's head-ref (e.g. `feat/add-widget`) and a list of regex
patterns from `.seneschal.yml`, returns a `BranchNameViolation` when
ZERO patterns match. Empty pattern list = feature disabled.

Matching uses `re.fullmatch` — patterns must describe the ENTIRE head
ref, not just a prefix. `^feat/add` would otherwise match
`feat/addendum-sneaky-branch`, which is almost never what operators
want. If a prefix-only match is intended the pattern can be explicit
(e.g. `^feat/.*`).

ReDoS mitigation: `re.match` / `re.fullmatch` in stdlib do not accept
a timeout parameter, so we rely on defense-in-depth:
 - Patterns are truncated to ~200 chars by `repo_config._sanitize`
   during config parse.
 - `.seneschal.yml` is operator-controlled (push access required).
 - Invalid regex compile errors are caught and logged; the offending
   pattern is skipped rather than crashing the check.
"""

from __future__ import annotations

import re
import sys as _sys
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class BranchNameViolation:
    """Branch-name mismatch context.

    `head_ref` is the offending ref. `reason` explains the mismatch
    (what patterns were configured); it does NOT embed `head_ref` —
    the renderer in analyzer.py composes the final message so reviewers
    see the ref once, not twice.
    """

    head_ref: str
    reason: str

    @property
    def subject(self) -> str:
        """Uniform accessor across P3 violation dataclasses (see also
        LicenseViolation.subject, ConventionViolation.subject). Lets
        analyzer's finding renderers stay table-driven instead of
        copy-pasting per-field extraction."""
        return self.head_ref


def check_branch_name(
    head_ref: Optional[str],
    patterns: List[str],
) -> Optional[BranchNameViolation]:
    """Return a violation if `head_ref` matches none of the patterns.

    Behavior:
     - Empty `patterns` list = feature OFF → return None silently.
     - Patterns configured but `head_ref` is None/empty → return None
       and emit a stderr warning so the silent no-op is operator-visible.
     - Invalid regex patterns are logged to stderr and skipped.
     - All patterns invalid → return None (no wolf-crying on every PR).
    """
    if not patterns:
        return None
    # Patterns configured but head_ref is missing — surface the no-op
    # instead of silently accepting. Webhook payloads SHOULD carry the
    # ref; a missing ref means the caller upstream is broken.
    if not head_ref:
        print(
            "[seneschal] branch_name_patterns configured but head_ref is "
            "missing; skipping branch-name check",
            file=_sys.stderr,
        )
        return None

    compiled: List[re.Pattern] = []
    for pat in patterns:
        try:
            compiled.append(re.compile(pat))
        except re.error as exc:
            # Mirror repo_config.py's pattern — print to stderr because
            # this module may be imported from contexts (MCP server) that
            # don't wire `app.log`.
            print(
                f"[seneschal] invalid branch_name_pattern {pat!r}: {exc}; skipping",
                file=_sys.stderr,
            )

    # If every configured pattern was invalid, there's effectively nothing
    # to match against. Return None rather than crying wolf on every PR.
    if not compiled:
        return None

    for rx in compiled:
        # fullmatch: pattern must describe the ENTIRE ref, not just a
        # prefix. `^feat/add` rightly refuses to match
        # `feat/addendum-sneaky-branch`.
        if rx.fullmatch(head_ref):
            return None

    accepted = ", ".join(patterns)
    return BranchNameViolation(
        head_ref=head_ref,
        reason=(
            f"Branch name does not match any configured "
            f"branch_name_patterns: {accepted}"
        ),
    )
