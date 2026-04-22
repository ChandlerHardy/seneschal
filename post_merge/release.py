"""Release-prep helpers — pure semver + notes rendering.

I/O-free. The orchestrator decides when to open / amend a release PR.
"""

from __future__ import annotations

import re
from typing import List

# Match `1.2.3` or `v1.2.3`; capture the optional `v` prefix so we can
# preserve it in the bumped string.
_SEMVER_RE = re.compile(r"^(?P<v>v?)(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$")

_BREAKING_LINE_RE = re.compile(r"BREAKING\s*CHANGE", re.IGNORECASE)
# Conventional-commit `!` marker before the colon: `feat!:` / `fix(scope)!:`.
_BANG_PREFIX_RE = re.compile(r"^\s*[-*]?\s*[a-zA-Z]+(?:\([^)]*\))?!:", re.MULTILINE)
_FEAT_PREFIX_RE = re.compile(r"^\s*[-*]?\s*feat(?:\([^)]*\))?:", re.IGNORECASE | re.MULTILINE)


def bump_kind(unreleased_lines: List[str]) -> str:
    """Decide whether the unreleased entries warrant a major / minor / patch.

    Rules:
      - Any `BREAKING CHANGE` marker -> major.
      - Any `<type>!:` prefix       -> major.
      - Any `feat:` / `feat(scope):` -> minor.
      - Otherwise (fix, chore, etc.) -> patch.
    """
    text = "\n".join(unreleased_lines)
    if _BREAKING_LINE_RE.search(text) or _BANG_PREFIX_RE.search(text):
        return "major"
    if _FEAT_PREFIX_RE.search(text):
        return "minor"
    return "patch"


def next_version(current: str, kind: str) -> str:
    """Bump a semver string. Preserves a leading `v` if present.

    Raises ValueError on input that doesn't match `[v]MAJOR.MINOR.PATCH`.
    """
    m = _SEMVER_RE.match((current or "").strip())
    if not m:
        raise ValueError(f"invalid semver: {current!r}")
    prefix = m.group("v")
    major = int(m.group("major"))
    minor = int(m.group("minor"))
    patch = int(m.group("patch"))
    if kind == "major":
        major += 1
        minor = 0
        patch = 0
    elif kind == "minor":
        minor += 1
        patch = 0
    elif kind == "patch":
        patch += 1
    else:
        raise ValueError(f"invalid bump kind: {kind!r}")
    return f"{prefix}{major}.{minor}.{patch}"


def render_release_notes(unreleased_section: str, new_version: str, today_iso: str) -> str:
    """Take a `## [Unreleased]\\n...` block and return a `## [<version>] - <date>\\n...` block.

    Does NOT modify the input. The Unreleased subsections (### Added etc.)
    are preserved verbatim.
    """
    if not unreleased_section:
        return f"## [{new_version}] - {today_iso}\n"
    out = re.sub(
        r"^## \[Unreleased\][^\n]*",
        f"## [{new_version}] - {today_iso}",
        unreleased_section,
        count=1,
        flags=re.MULTILINE,
    )
    return out
