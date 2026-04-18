"""Scope-drift detection for pull requests.

Flags PRs that touch multiple unrelated areas of the codebase without
declaring themselves as broad refactors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Sequence, Set

from risk import PRFile


# Directories to ignore when counting scope — generated, vendored, or meta.
IGNORED_TOP_LEVEL_DIRS: Set[str] = {
    "node_modules",
    "vendor",
    "dist",
    "build",
    ".next",
    "target",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".turbo",
    "coverage",
}

# Title fragments that signal "broad by design" — don't flag these as drift.
BROAD_TITLE_FRAGMENTS: Sequence[str] = (
    "refactor",
    "chore",
    "wip",
    "bump",
    "deps:",
    "deps(",
    "cleanup",
    "rename",
    "move",
    "reorganize",
    "reorg",
    "restructure",
    "lint",
    "format",
)

# How many distinct top-level areas triggers "drift".
DRIFT_THRESHOLD = 3


@dataclass
class ScopeReport:
    drifted: bool
    top_level_dirs: List[str] = field(default_factory=list)
    reason: str = ""

    def summary(self) -> str:
        if not self.drifted:
            if self.top_level_dirs:
                return f"**Scope: focused** ({len(self.top_level_dirs)} area(s))"
            return "**Scope: focused**"
        return f"**Scope: DRIFTED** — {self.reason}"


def _is_broad_title(title: str) -> bool:
    if not title:
        return False
    lower = title.lower()
    return any(frag in lower for frag in BROAD_TITLE_FRAGMENTS)


def _top_level(filename: str) -> str:
    if "/" not in filename:
        return ""
    return filename.split("/", 1)[0]


def collect_top_level_dirs(files: Sequence[PRFile]) -> List[str]:
    """Return sorted unique top-level directories after filtering generated paths."""
    seen: Set[str] = set()
    for f in files:
        top = _top_level(f.filename)
        if not top:
            continue
        if top in IGNORED_TOP_LEVEL_DIRS:
            continue
        seen.add(top)
    return sorted(seen)


def detect_scope_drift(title: str, files: Sequence[PRFile]) -> ScopeReport:
    """Decide whether a PR has drifted scope.

    A PR is drifted if:
      - It touches >= DRIFT_THRESHOLD distinct top-level directories
      - Its title does not signal a broad refactor/chore
    """
    dirs = collect_top_level_dirs(files)
    if _is_broad_title(title):
        return ScopeReport(drifted=False, top_level_dirs=dirs, reason="")
    if len(dirs) >= DRIFT_THRESHOLD:
        return ScopeReport(
            drifted=True,
            top_level_dirs=dirs,
            reason=f"Touches {len(dirs)} unrelated areas: {', '.join(dirs)}",
        )
    return ScopeReport(drifted=False, top_level_dirs=dirs, reason="")
