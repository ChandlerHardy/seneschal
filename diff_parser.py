"""Unified-diff parser.

Generic diff-text parsing shared by every analysis module
(test_gaps, breaking_changes, quality_scan, secrets_scan, context_loader).
Lives in its own module so sibling analyzers don't reach into
`test_gaps.py`'s leading-underscore internals to get at the regexes.

Handles combined diffs (`@@@`) by suspending capture when a hunk header
that isn't a plain two-file hunk arrives — the two-column `+/-` markers
of a combined diff would otherwise be captured as content.
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple


DIFF_FILE_HEADER = re.compile(r"^diff --git a/(?P<a>\S+) b/(?P<b>\S+)\s*$")
DIFF_NEWFILE_LINE = re.compile(r"^\+\+\+ b/(?P<path>\S+)\s*$")
DIFF_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,\d+)? @@")


def parse_unified_diff(diff_text: str) -> Dict[str, List[str]]:
    """Return {filename: [added_lines_without_+_prefix]}.

    Only captures lines that start with a single '+' within a hunk (not
    the '+++' file header). Ignores context and deletion lines.
    """
    with_lines = parse_unified_diff_with_lines(diff_text)
    return {
        filename: [content for _, content in pairs]
        for filename, pairs in with_lines.items()
    }


def parse_unified_diff_with_lines(diff_text: str) -> Dict[str, List[Tuple[int, str]]]:
    """Return {filename: [(line_number, added_line_content), ...]}.

    line_number is the 1-based line number in the NEW file. Deletions and
    context lines are skipped. Hunk headers reset the counter.
    """
    result: Dict[str, List[Tuple[int, str]]] = {}
    current: str | None = None
    in_hunk = False
    next_line = 0

    for raw in diff_text.splitlines():
        m = DIFF_FILE_HEADER.match(raw)
        if m:
            current = m.group("b")
            result.setdefault(current, [])
            in_hunk = False
            continue
        m = DIFF_NEWFILE_LINE.match(raw)
        if m:
            current = m.group("path")
            result.setdefault(current, [])
            in_hunk = False
            continue
        hunk_match = DIFF_HUNK_HEADER.match(raw)
        if hunk_match:
            in_hunk = True
            next_line = int(hunk_match.group("start"))
            continue
        if raw.startswith("@@"):
            # Combined diff (`@@@`) or any unrecognized @@-prefixed line.
            # We can't trust the previous next_line counter, so suspend
            # capture until the next proper hunk header arrives.
            in_hunk = False
            continue
        if not in_hunk or current is None:
            continue
        if raw.startswith("+++"):
            continue
        if raw.startswith("+"):
            result[current].append((next_line, raw[1:]))
            next_line += 1
        elif raw.startswith("-"):
            # deletion — does not advance next_line
            continue
        else:
            # context line — advances next_line
            next_line += 1
    return result


def parse_diff_both_sides(diff_text: str) -> Dict[str, Dict[str, List[str]]]:
    """Return {filename: {'added': [lines], 'removed': [lines]}}.

    Simpler than the with-lines parser because we don't need line numbers.
    Used by the breaking-change detector. Handles combined diffs (`@@@`)
    by suspending capture — the two-column markers of a combined diff
    would otherwise be mis-captured as `++func Foo()` style lines.
    """
    result: Dict[str, Dict[str, List[str]]] = {}
    current: str | None = None
    in_hunk = False

    for raw in diff_text.splitlines():
        m = DIFF_FILE_HEADER.match(raw)
        if m:
            current = m.group("b")
            result.setdefault(current, {"added": [], "removed": []})
            in_hunk = False
            continue
        m = DIFF_NEWFILE_LINE.match(raw)
        if m:
            current = m.group("path")
            result.setdefault(current, {"added": [], "removed": []})
            in_hunk = False
            continue
        if DIFF_HUNK_HEADER.match(raw):
            in_hunk = True
            continue
        if raw.startswith("@@"):
            # Combined diff or otherwise unparseable hunk header — abandon
            # capture rather than mis-attributing +/- lines whose first
            # two chars carry the combined-diff status columns.
            in_hunk = False
            continue
        if not in_hunk or current is None:
            continue
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+"):
            result[current]["added"].append(raw[1:])
        elif raw.startswith("-"):
            result[current]["removed"].append(raw[1:])
    return result
