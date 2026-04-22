"""Followup parsing — pure regex over a stored review body.

Reviewers can mark deferred work in their review with:

  - [FOLLOWUP] Refactor the X module to drop the global

The orchestrator extracts these and files GitHub issues. Cap at 10 per
review to keep noisy reviews from spamming the issue tracker; the 11th+
roll up into one synthetic issue listing the leftovers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

# Match a list bullet starting (with optional indent) with `- [FOLLOWUP] <title>`.
_MARKER_RE = re.compile(
    r"^(?P<indent>[ \t]*)-\s*\[FOLLOWUP\]\s+(?P<title>.+?)\s*$",
    re.MULTILINE | re.IGNORECASE,
)

_TITLE_CAP = 100
_EXCERPT_CAP = 500
_CONTEXT_LINES = 3
_FOLLOWUP_LIMIT = 10


@dataclass(frozen=True)
class Followup:
    title: str
    body_excerpt: str
    source_line: int  # 1-based


def _build_excerpt(lines: List[str], marker_idx: int) -> str:
    """Marker line + up to N non-empty context lines after it, capped at 500 chars."""
    pieces = [lines[marker_idx]]
    taken = 0
    for j in range(marker_idx + 1, len(lines)):
        if taken >= _CONTEXT_LINES:
            break
        line = lines[j]
        if not line.strip():
            # Blank line ends the context block.
            break
        pieces.append(line)
        taken += 1
    excerpt = "\n".join(pieces)
    return excerpt[:_EXCERPT_CAP]


def parse_followups(review_body: str) -> List[Followup]:
    """Extract `[FOLLOWUP]` markers from a review body.

    Returns at most _FOLLOWUP_LIMIT + 1 entries: the first 10 individual
    followups, plus an optional synthetic rollup if the review had more.
    """
    if not review_body:
        return []

    lines = review_body.split("\n")
    found: List[Followup] = []
    leftover_titles: List[str] = []

    # Re-scan with the regex but key on which line each match landed on so
    # source_line is accurate.
    for match in _MARKER_RE.finditer(review_body):
        # Compute 1-based line number from match start offset.
        line_idx = review_body.count("\n", 0, match.start())
        title = match.group("title").strip()[:_TITLE_CAP]
        if len(found) < _FOLLOWUP_LIMIT:
            excerpt = _build_excerpt(lines, line_idx)
            found.append(
                Followup(
                    title=title,
                    body_excerpt=excerpt,
                    source_line=line_idx + 1,
                )
            )
        else:
            leftover_titles.append(title)

    if leftover_titles:
        rollup_body = "Additional follow-ups not filed individually:\n\n" + "\n".join(
            f"- {t}" for t in leftover_titles
        )
        found.append(
            Followup(
                title="Additional follow-ups from review",
                body_excerpt=rollup_body[:_EXCERPT_CAP],
                source_line=0,
            )
        )

    return found
