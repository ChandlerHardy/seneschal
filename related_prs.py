"""Detect other open PRs that overlap with the current PR's file set.

Pure computation separated from GitHub I/O so it can be unit-tested without
hitting the network. Callers assemble `OtherPR` objects from the API and
hand them to `find_related_prs`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Sequence, Set


@dataclass(frozen=True)
class OtherPR:
    number: int
    title: str
    files: frozenset  # frozenset[str]


@dataclass
class RelatedPR:
    number: int
    title: str
    overlapping_files: List[str] = field(default_factory=list)

    @property
    def overlap_count(self) -> int:
        return len(self.overlapping_files)


def find_related_prs(
    current_files: Iterable[str],
    others: Sequence[OtherPR],
    max_results: int = 5,
) -> List[RelatedPR]:
    """Return open PRs that share at least one file with the current PR.

    Results are sorted by overlap count (desc), then PR number (asc).
    Capped at `max_results` to keep review comments readable.
    """
    current_set: Set[str] = set(current_files)
    if not current_set:
        return []
    related: List[RelatedPR] = []
    for other in others:
        overlap = sorted(current_set & other.files)
        if overlap:
            related.append(
                RelatedPR(
                    number=other.number,
                    title=other.title,
                    overlapping_files=overlap,
                )
            )
    related.sort(key=lambda r: (-r.overlap_count, r.number))
    return related[:max_results]


def summarize_related(related: Sequence[RelatedPR]) -> str:
    if not related:
        return "**Related PRs: none** — no other open PRs touch these files."
    lines = [f"**Related PRs: {len(related)}** — potential merge conflicts:", ""]
    for r in related:
        preview = ", ".join(f"`{f}`" for f in r.overlapping_files[:3])
        if r.overlap_count > 3:
            preview += f", +{r.overlap_count - 3} more"
        lines.append(f"- #{r.number} ({r.title}) — overlap: {preview}")
    return "\n".join(lines)
