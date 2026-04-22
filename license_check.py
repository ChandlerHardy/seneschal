"""License-header scan for newly-added source files.

Reads added-file line data via `diff_parser.parse_unified_diff` and
checks that the first N lines of each new file satisfy the operator's
configured license-header text.

Behavior:
 - Fires ONLY on newly-added files. Modified / renamed / deleted files
   are ignored. New-file detection prefers `pr_files` status (from
   GitHub's PR files API, typed as `Sequence[PRFile]`) but falls back
   to the `new file mode` diff marker when `pr_files is None`.
 - Respects `license_applies_to` globs: if non-empty, only files matching
   at least one glob are checked. Empty = check every added file.
 - Respects `license_exemptions` globs: matching files are skipped even
   if they match `license_applies_to`.
 - Skips binary files (NUL byte detected anywhere in added content —
   full-line scan, not just the header window).
 - Supports `{YEAR}` placeholder in the header text: translates to the
   regex `\\d{4}` (any 4-digit year) so the configured header doesn't
   need to be edited every January.
 - Multi-line headers: the configured header and the file's leading
   content are compared line-by-line (up to len(header_lines) lines).
 - Normalizes added lines before matching: strips UTF-8 BOM from the
   first line, strips trailing `\r` from every line. Operators' header
   text is pre-normalized during `_sanitize_header_text` (CRLF→LF,
   trailing newline stripped), so a YAML block-scalar header behaves
   the same as an inline one.
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
# 500 lines into a file. Distinct from the binary-file NUL scan (which
# covers ALL added lines, not just this window).
_HEADER_WINDOW = 40

# UTF-8 BOM sequence. Windows editors sometimes prepend this to the first
# line of source files; `git diff` passes it through verbatim.
_UTF8_BOM = "﻿"


def _looks_binary(lines: Sequence[str]) -> bool:
    """Return True if ANY added line contains a NUL byte.

    Scans the full sequence (not just the header window) because a binary
    file's NUL bytes can appear anywhere — capping at 40 would misclassify
    a 50-line text preamble + binary tail as text and falsely flag it as
    "missing header". Lines are already in memory, so O(n) is free.
    """
    for ln in lines:
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


def _normalize_added_line(line: str, is_first: bool) -> str:
    """Strip UTF-8 BOM (first line only) and trailing CR from a diff line.

    Fix F: normalize before regex match so BOM-prefixed or CRLF-preserved
    diff lines still satisfy a plain configured header.
    """
    if is_first and line.startswith(_UTF8_BOM):
        line = line[len(_UTF8_BOM):]
    if line.endswith("\r"):
        line = line[:-1]
    return line


def _header_matches(
    added_lines: Sequence[str],
    compiled_patterns: Sequence[re.Pattern],
) -> bool:
    """Does the leading added content contain the configured header?

    Takes a pre-compiled per-line pattern list (built once per scan call
    by `scan_license_headers`) to avoid N*M re.compile() calls on large
    diffs. Lines are full-string matched — operators want a sharp signal,
    not fuzzy contains-matching.

    Normalization (fix F) is applied to each added line before match:
    first-line BOM strip + trailing-CR strip.
    """
    if not compiled_patterns:
        return True  # feature off — treat as satisfied

    if len(added_lines) < len(compiled_patterns):
        return False

    for i, (pattern, actual) in enumerate(
        zip(compiled_patterns, added_lines[: len(compiled_patterns)])
    ):
        normalized = _normalize_added_line(actual, is_first=(i == 0))
        if not pattern.fullmatch(normalized):
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

    # Fix G: compile the per-line header pattern list ONCE per scan call.
    # Previously _build_header_regex was called once per header line per
    # file (N files × M header lines = N*M compiles on every PR).
    # Fix E (belt + suspenders): strip trailing empty lines from the
    # split. `_sanitize_header_text` already rstrips a single trailing
    # newline from YAML-loaded configs, but constructors that bypass
    # the sanitizer (direct StandardsConfig()) still benefit.
    header_lines = config.license_header.split("\n")
    while header_lines and header_lines[-1] == "":
        header_lines.pop()
    compiled_patterns = [_build_header_regex(ln) for ln in header_lines]

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

        # Binary file guard (full-line scan).
        if _looks_binary(added_lines):
            continue

        if not _header_matches(added_lines, compiled_patterns):
            violations.append(
                LicenseViolation(
                    file=filename,
                    reason="Missing or mismatched license header",
                )
            )

    return violations
