"""Branch-name regex convention check.

Given a PR's head-ref (e.g. `feat/add-widget`) and a list of regex
patterns from `.seneschal.yml`, returns a `BranchNameViolation` when
ZERO patterns match. Empty pattern list = feature disabled.

ReDoS mitigation: `re.match` in stdlib does not accept a timeout
parameter, so we rely on defense-in-depth:
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
    head_ref: str
    reason: str


def check_branch_name(
    head_ref: Optional[str],
    patterns: List[str],
) -> Optional[BranchNameViolation]:
    """Return a violation if `head_ref` matches none of the patterns.

    Empty `patterns` list means the feature is OFF — return None.
    Invalid regex patterns are logged to stderr and skipped.
    """
    if not patterns:
        return None
    # Defensive: webhook payloads could conceivably be missing the ref.
    # Rather than raising, just skip the check.
    if head_ref is None:
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
        if rx.match(head_ref):
            return None

    accepted = ", ".join(patterns)
    return BranchNameViolation(
        head_ref=head_ref,
        reason=(
            f"Branch name `{head_ref}` does not match any configured "
            f"branch_name_patterns: {accepted}"
        ),
    )
