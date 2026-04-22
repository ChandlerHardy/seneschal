"""License-header scan for newly-added source files.

Reads added-file line data via `diff_parser.parse_unified_diff` and
checks that the first N lines of each new file satisfy the operator's
configured license-header text.

Behavior:
 - Fires ONLY on newly-added files. Modified / renamed / deleted files
   are ignored. New-file detection prefers `pr_files` status (from
   GitHub's PR files API) but falls back to a "no deletions + entirely
   new content" heuristic.
 - Respects `license_applies_to` globs: if non-empty, only files matching
   at least one glob are checked. Empty = check every added file.
 - Respects `license_exemptions` globs: matching files are skipped even
   if they match `license_applies_to`.
 - Skips binary files (NUL byte detected in added content).
 - Supports `{YEAR}` placeholder in the header text: translates to the
   regex `\\d{4}` (any 4-digit year) so the configured header doesn't
   need to be edited every January.
 - Multi-line headers: the configured header and the file's leading
   content are compared line-by-line (up to len(header_lines) lines).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence

from diff_parser import parse_unified_diff
from repo_config import StandardsConfig, glob_match
from risk import PRFile


@dataclass
class LicenseViolation:
    file: str
    reason: str


# How many leading lines of the added content we'll consider as "header
# candidate" region. Anything past this is ignored — headers don't live
# 500 lines into a file.
_HEADER_WINDOW = 40


def _looks_binary(lines: Sequence[str]) -> bool:
    """Return True if any of the sampled added lines contains a NUL byte."""
    for ln in lines[:_HEADER_WINDOW]:
        if "\x00" in ln:
            return True
    return False


def _build_header_regex(header_text: str) -> re.Pattern:
    """Translate header text (with optional `{YEAR}` placeholder) to regex.

    `{YEAR}` is the only placeholder we honor. Everything else is treated
    as literal and escaped. Matching is line-oriented: `header_text` is
    split on newlines and each line becomes a regex; together they must
    appear as a contiguous prefix in the file's leading content.
    """
    placeholder = "\x00YEAR\x00"
    work = header_text.replace("{YEAR}", placeholder)
    escaped = re.escape(work)
    # After re.escape, our sentinel is still intact (NUL bytes aren't
    # escaped), so substitute the regex fragment for it.
    regex = escaped.replace(re.escape(placeholder), r"\d{4}")
    return re.compile(regex)


def _header_matches(added_lines: Sequence[str], header_text: str) -> bool:
    """Does the leading added content contain the configured header?

    The scan compares the first `len(header_lines)` lines of added content
    against a per-line regex derived from `header_text`. Lines are
    compared as full-string regex matches to keep the contract strict —
    operators want a sharp signal, not fuzzy contains-matching.
    """
    if not header_text:
        return True  # feature off — treat as satisfied

    header_lines = header_text.split("\n")
    # If the added content is shorter than the header, there's no way to
    # satisfy it.
    if len(added_lines) < len(header_lines):
        return False

    for expected, actual in zip(header_lines, added_lines[: len(header_lines)]):
        pattern = _build_header_regex(expected)
        if not pattern.fullmatch(actual):
            return False
    return True


def _file_is_new(
    filename: str,
    diff_text: str,
    pr_files: Optional[Sequence[PRFile]],
) -> bool:
    """Determine whether this file is a new addition in the PR.

    Prefers `pr_files` metadata (typed `PRFile` — `.status == "added"`
    is the authoritative signal). When `pr_files is None`, falls back
    to the diff-text heuristic: `new file mode` marker. The fallback
    is explicit (None is the sentinel) so a caller that passes an
    empty sequence opts OUT of scanning — matching "nothing in this PR
    is new" semantics rather than silently dropping to heuristic.

    Renamed files are NOT treated as new: the file existed pre-PR,
    only its path changed, so the license-scan contract (new-file-only)
    means no violation.
    """
    if pr_files is not None:
        for entry in pr_files:
            if entry.filename != filename:
                continue
            if entry.status == "renamed":
                # Rename, not add — content was already licensed pre-PR.
                return False
            return entry.status == "added"
        # pr_files supplied but this filename isn't in it — treat as
        # not-new (the caller intentionally scoped it out).
        return False

    # Fallback: look for `new file mode` marker in the raw diff. This is
    # cheap and reliable for standard `git diff --no-color` output.
    marker = f"diff --git a/{filename} b/{filename}"
    idx = diff_text.find(marker)
    if idx == -1:
        return False
    # Check a small window after the marker for `new file mode`.
    window = diff_text[idx : idx + 200]
    return "new file mode" in window


def scan_license_headers(
    diff_text: str,
    pr_files: Optional[Sequence[PRFile]],
    config: StandardsConfig,
) -> List[LicenseViolation]:
    """Scan a PR's diff for files missing the configured license header.

    Returns an empty list if:
     - `config.license_header` is empty (feature off).
     - The diff has no newly-added files.
     - Every added file either matches the header or is exempt.
    """
    if not config.license_header:
        return []

    added_by_file = parse_unified_diff(diff_text)
    violations: List[LicenseViolation] = []

    for filename, added_lines in added_by_file.items():
        # Skip non-new files (modified, renamed, etc.)
        if not _file_is_new(filename, diff_text, pr_files):
            continue

        # applies_to filter. Empty = check every added file.
        if config.license_applies_to:
            if not any(glob_match(p, filename) for p in config.license_applies_to):
                continue

        # exemptions filter (takes priority over applies_to)
        if any(glob_match(p, filename) for p in config.license_exemptions):
            continue

        # Binary file guard
        if _looks_binary(added_lines):
            continue

        if not _header_matches(added_lines, config.license_header):
            violations.append(
                LicenseViolation(
                    file=filename,
                    reason="Missing or mismatched license header",
                )
            )

    return violations
