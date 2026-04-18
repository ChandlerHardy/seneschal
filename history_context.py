"""Load decision history (ADRs) from the target repo.

Architecture Decision Records (ADRs) and decision logs are the
team-knowledge layer that generic AI reviewers miss. A diff might
look fine in isolation but violate a deliberate decision recorded
months ago. We scan the repo for common ADR conventions and feed
the ones most relevant to the current diff into the reviewer's
system prompt.

Discovery locations (first existing wins per category):
    docs/adr/            — adr-tools / Nygard style
    docs/decisions/      — Log4brains / MADR style
    adr/                 — top-level variant
    ADR.md               — single-file team history
    DECISIONS.md         — single-file team history
    docs/decisions.md    — docs-subfolder variant

Filename patterns recognized in the directories:
    adr-*.md, ADR-*.md
    NNNN-*.md (e.g. 0001-use-postgres.md, 042-drop-mongo.md)
    *.adr.md

Safety: ADR contents are appended to the Claude system prompt, so we
cap total bytes and per-ADR length, strip control chars, and ignore
files outside the repo root (path traversal protection).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import List, Sequence

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# ADR directories to probe, in priority order (first-match wins within
# a repo, but files across multiple conventions are aggregated).
ADR_DIRS = (
    "docs/adr",
    "docs/decisions",
    "adr",
)

# Single-file conventions
ADR_FILES = (
    "ADR.md",
    "DECISIONS.md",
    "docs/decisions.md",
)

# Filename patterns — case-insensitive
_ADR_FILENAME_RE = re.compile(
    r"""(?xi)
    ^(
        adr-[\w\-]+\.md           # adr-something.md
      | \d{3,4}-[\w\-]+\.md       # 0001-use-postgres.md
      | [\w\-]+\.adr\.md          # feature.adr.md
    )$
    """
)

# Size caps
MAX_ADR_BODY_LEN = 800       # per-ADR excerpt length fed to reviewer
MAX_ADRS_IN_PROMPT = 5       # never send more than this
MAX_ADRS_DISCOVERED = 200    # hard cap during discovery to bound I/O

# Heuristic relevance scoring: a token is "meaningful" if it's >= 4 chars
# and not in a small stop-word set. We extract tokens from the diff +
# touched filenames, intersect with tokens from the ADR, and rank.
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
_STOPWORDS = frozenset(
    {
        "this",
        "that",
        "with",
        "from",
        "what",
        "when",
        "then",
        "have",
        "been",
        "will",
        "should",
        "would",
        "could",
        "into",
        "also",
        "must",
        "your",
        "their",
        "which",
    }
)


@dataclass(frozen=True)
class ADR:
    id: str           # "0001", "drop-mongo", etc. — best effort from filename
    title: str        # first H1 or filename fallback
    status: str       # "accepted", "proposed", "superseded", "" if unknown
    body: str         # post-title content, trimmed
    path: str         # relative to repo_root


def _sanitize_text(text: str) -> str:
    return _CONTROL_CHARS.sub("", text)


def _parse_adr_file(abs_path: str, rel_path: str) -> ADR:
    """Best-effort parse of an ADR markdown file."""
    try:
        with open(abs_path, "r") as fh:
            content = fh.read()
    except OSError:
        return ADR(id="", title=os.path.basename(rel_path), status="", body="", path=rel_path)

    content = _sanitize_text(content)

    # ID from filename (strip extension, leading digits preserved)
    filename = os.path.basename(rel_path)
    adr_id = os.path.splitext(filename)[0]
    # Strip "adr-" prefix if present
    if adr_id.lower().startswith("adr-"):
        adr_id = adr_id[4:]

    # Title: first H1, fallback to filename
    title = adr_id.replace("-", " ").replace("_", " ").title()
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            break

    # Status: look for "## Status" section OR YAML-ish `status:` field
    status = ""
    lower = content.lower()
    status_match = re.search(r"^\s*status\s*:\s*(\w+)", content, re.MULTILINE | re.IGNORECASE)
    if status_match:
        status = status_match.group(1).lower()
    else:
        heading_match = re.search(r"^##\s*Status\s*\n+\s*(\w+)", content, re.MULTILINE | re.IGNORECASE)
        if heading_match:
            status = heading_match.group(1).lower()

    # Body: everything after the title (trim to 4KB upstream of scoring)
    body = content
    if body.startswith("#"):
        # drop the H1 line for a cleaner body
        nl = body.find("\n")
        if nl >= 0:
            body = body[nl + 1 :].strip()
    body = body[:4000]

    return ADR(id=adr_id, title=title, status=status, body=body, path=rel_path)


def _discover_dir(repo_root: str, dir_rel: str, out: List[str]) -> None:
    """Append rel paths of matching .md files inside dir_rel to out."""
    abs_dir = os.path.join(repo_root, dir_rel)
    if not os.path.isdir(abs_dir):
        return
    try:
        entries = sorted(os.listdir(abs_dir))
    except OSError:
        return
    for name in entries:
        if not name.endswith(".md"):
            continue
        if not _ADR_FILENAME_RE.match(name):
            continue
        rel = os.path.join(dir_rel, name)
        out.append(rel)
        if len(out) >= MAX_ADRS_DISCOVERED:
            return


def find_adrs(repo_root: str) -> List[ADR]:
    """Discover ADRs in the target repo. Returns at most MAX_ADRS_DISCOVERED."""
    if not os.path.isdir(repo_root):
        return []
    rel_paths: List[str] = []

    # Probe directories first (most teams use these)
    for dir_rel in ADR_DIRS:
        _discover_dir(repo_root, dir_rel, rel_paths)
        if len(rel_paths) >= MAX_ADRS_DISCOVERED:
            break

    # Then single-file conventions
    if len(rel_paths) < MAX_ADRS_DISCOVERED:
        for file_rel in ADR_FILES:
            abs_path = os.path.join(repo_root, file_rel)
            if os.path.isfile(abs_path):
                rel_paths.append(file_rel)

    adrs: List[ADR] = []
    abs_root = os.path.realpath(repo_root)
    for rel in rel_paths:
        abs_path = os.path.realpath(os.path.join(repo_root, rel))
        # Path-traversal guard (unlikely via listdir, but defend anyway)
        if not (abs_path == abs_root or abs_path.startswith(abs_root + os.sep)):
            continue
        adrs.append(_parse_adr_file(abs_path, rel))
    return adrs


def _extract_tokens(text: str) -> set:
    tokens = {
        m.group(0).lower()
        for m in _TOKEN_RE.finditer(text)
    }
    return tokens - _STOPWORDS


def _score_adr(adr: ADR, diff_tokens: set, touched_filenames: Sequence[str]) -> int:
    """Higher score = more relevant. Zero means irrelevant."""
    # Token overlap in body + title
    adr_text = f"{adr.title}\n{adr.body}"
    adr_tokens = _extract_tokens(adr_text)
    overlap = len(adr_tokens & diff_tokens)

    # Filename-keyword bonus: if any touched filename shares a substring
    # with the ADR's filename stem, bump the score (directory-level hints).
    stem = os.path.splitext(os.path.basename(adr.path))[0].lower()
    stem_tokens = {t for t in re.split(r"[-_/]", stem) if len(t) >= 4}
    fname_bonus = 0
    for f in touched_filenames:
        f_tokens = {t for t in re.split(r"[-_/.]", f.lower()) if len(t) >= 4}
        if stem_tokens & f_tokens:
            fname_bonus += 2

    # If there's no real relevance signal, score zero — status alone
    # doesn't make an ADR relevant to a specific diff.
    if overlap == 0 and fname_bonus == 0:
        return 0

    # Prefer accepted/approved ADRs slightly over proposed/superseded
    # (only kicks in when the ADR is already relevant).
    status_bonus = 1 if adr.status in {"accepted", "approved"} else 0

    return overlap + fname_bonus + status_bonus


def relevant_adrs(
    adrs: Sequence[ADR],
    touched_files: Sequence[str],
    diff_text: str,
    limit: int = MAX_ADRS_IN_PROMPT,
) -> List[ADR]:
    """Rank ADRs by relevance to the current diff; return top `limit`.

    Zero-score ADRs are dropped (it's better to feed no context than
    noise). If all score zero, returns empty list.
    """
    if not adrs:
        return []
    diff_tokens = _extract_tokens(diff_text)
    scored = []
    for adr in adrs:
        s = _score_adr(adr, diff_tokens, touched_files)
        if s > 0:
            scored.append((s, adr))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [adr for _s, adr in scored[:limit]]


def render_adrs_addendum(adrs: Sequence[ADR]) -> str:
    """Format selected ADRs for inclusion in the reviewer's system prompt."""
    if not adrs:
        return ""
    parts = ["## Relevant team decisions / ADRs\n"]
    parts.append(
        "The following Architecture Decision Records were flagged as potentially "
        "relevant to this diff. If any proposed change in the PR conflicts with "
        "a decision below, call that out explicitly in your review.\n"
    )
    for adr in adrs:
        excerpt = adr.body[:MAX_ADR_BODY_LEN].rstrip()
        header = f"### {adr.title}"
        if adr.status:
            header += f" _(status: {adr.status})_"
        header += f"\n_Source: {adr.path}_"
        parts.append(header)
        parts.append("")
        parts.append(excerpt)
        parts.append("")
    return "\n".join(parts)
