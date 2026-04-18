"""PR title / commit message quality check.

Catches low-effort titles like "wip", "fix", "update", and gently nudges
toward conventional commits without being strict about it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


CONVENTIONAL_TYPES = (
    "feat",
    "fix",
    "docs",
    "style",
    "refactor",
    "perf",
    "test",
    "build",
    "ci",
    "chore",
    "revert",
)

VAGUE_TITLES = {
    "wip",
    "fix",
    "update",
    "updates",
    "changes",
    "fixes",
    "stuff",
    "tweaks",
    "misc",
    "cleanup",
    "work",
    "patch",
}


@dataclass
class TitleReport:
    level: str  # "ok", "nit", "warning"
    reason: str = ""

    @property
    def is_ok(self) -> bool:
        return self.level == "ok"


def _bare(title: str) -> str:
    return title.strip().lower().rstrip(".!?")


def check_title(title: Optional[str]) -> TitleReport:
    """Evaluate PR title quality. Returns level + reason."""
    if not title or not title.strip():
        return TitleReport(level="warning", reason="Empty PR title")

    raw = title.strip()
    bare = _bare(raw)

    # Very short.
    if len(bare) < 5:
        return TitleReport(level="warning", reason=f"PR title is too short ({len(bare)} chars)")

    # Vague single-word / single-phrase titles.
    if bare in VAGUE_TITLES:
        return TitleReport(level="warning", reason=f"PR title is too vague: '{raw}'")

    # Conventional commit prefix check (non-strict — just a nit).
    has_conventional = any(
        bare.startswith(f"{t}:") or bare.startswith(f"{t}(")
        for t in CONVENTIONAL_TYPES
    )

    # Titles that start with vague words even if slightly longer
    vague_starts = ("wip ", "fix.", "update.", "fixes ", "fix: stuff", "fix: things")
    if any(bare.startswith(v) for v in vague_starts):
        return TitleReport(level="nit", reason=f"PR title starts vaguely: '{raw}'")

    if not has_conventional and len(bare) < 15:
        return TitleReport(
            level="nit",
            reason=f"Short title without conventional prefix: '{raw}'",
        )

    return TitleReport(level="ok")
