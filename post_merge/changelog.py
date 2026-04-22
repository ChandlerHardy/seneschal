"""Changelog curation — pure formatting helpers.

This module is intentionally I/O-free. It produces strings; the orchestrator
does the disk + GitHub Contents-API plumbing.

Convention: Keep-a-Changelog (https://keepachangelog.com/en/1.1.0/).
The `## [Unreleased]` section accumulates entries between releases. Each
merged PR contributes one bullet under one of:

  ### Added       (feat)
  ### Fixed       (fix)
  ### Changed     (refactor, perf, style)
  ### Removed     (BREAKING — explicit removals)

Other conventional types (chore, docs, test, build, ci) currently roll into
`### Changed` so the changelog still acknowledges the PR rather than
silently dropping it. Repos that want a stricter filter can layer that on
top in P3 (standards enforcement).
"""

from __future__ import annotations

import re
import sys
from typing import Optional

# Single source of truth for valid prefixes. Imported (not duplicated) per the
# campaign plan's "reused helpers" block.
_THIS_DIR = "/".join(__file__.split("/")[:-2])
sys.path.insert(0, _THIS_DIR)
from title_check import CONVENTIONAL_TYPES  # noqa: E402


_KEEP_A_CHANGELOG_HEADER = (
    "# Changelog\n\n"
    "All notable changes to this project will be documented in this file.\n\n"
    "The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),\n"
    "and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).\n"
)

# Map a conventional-type prefix to a Keep-a-Changelog subsection.
# `chore`/`docs`/`test`/`build`/`ci` fall back to Changed so they still
# show up in the changelog rather than being silently dropped.
_PREFIX_TO_SUBSECTION = {
    "feat": "Added",
    "fix": "Fixed",
    "refactor": "Changed",
    "perf": "Changed",
    "style": "Changed",
    "chore": "Changed",
    "docs": "Changed",
    "test": "Changed",
    "build": "Changed",
    "ci": "Changed",
    "revert": "Changed",
    # Synthetic kinds the orchestrator may pass:
    "BREAKING": "Removed",
}

# Subsection ordering inside an Unreleased block — Keep-a-Changelog convention.
_SUBSECTION_ORDER = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")

# Regex to match a conventional-commit prefix at the start of a title:
#   feat: ...
#   feat(scope): ...
#   feat!: ...
#   feat(scope)!: ...
_PREFIX_RE = re.compile(
    r"^(?P<type>[a-zA-Z]+)(?:\([^)]*\))?(?P<bang>!?):\s*(?P<rest>.*)$",
)


def classify_prefix(title: str) -> Optional[str]:
    """Classify the conventional-commit prefix of a PR title.

    Returns one of the values in CONVENTIONAL_TYPES (lowercased) or None
    if the title has no recognized prefix.
    """
    if not title or not title.strip():
        return None
    m = _PREFIX_RE.match(title.strip())
    if not m:
        return None
    type_ = m.group("type").lower()
    if type_ not in CONVENTIONAL_TYPES:
        return None
    return type_


def _strip_prefix(title: str) -> str:
    """Drop the `feat: ` / `fix(scope)!: ` style prefix from a title."""
    if not title:
        return ""
    m = _PREFIX_RE.match(title.strip())
    if not m:
        return title.strip()
    type_ = m.group("type").lower()
    if type_ not in CONVENTIONAL_TYPES:
        return title.strip()
    return m.group("rest").strip() or title.strip()


def format_unreleased_entry(pr_number: int, title: str, url: str) -> str:
    """Render one markdown bullet for a merged PR.

    The conventional-commit prefix is stripped so the bullet reads as a
    human-friendly description rather than echoing the type taxonomy.
    """
    description = _strip_prefix(title)
    return f"- {description} ([#{int(pr_number)}]({url}))"


def _ensure_header(existing: str) -> str:
    """If `existing` is empty or has no `# Changelog` header, prepend one."""
    if not existing.strip():
        return _KEEP_A_CHANGELOG_HEADER
    if "# Changelog" not in existing.split("\n", 1)[0] and not existing.lstrip().startswith("# Changelog"):
        # Be defensive: if the file exists but isn't a Keep-a-Changelog
        # file, prepend the header rather than mangling whatever is there.
        return _KEEP_A_CHANGELOG_HEADER + "\n" + existing
    return existing


def _sub_for_kind(kind: str) -> str:
    """Map a kind hint to a subsection name, defaulting to Changed."""
    if not kind:
        return "Changed"
    if kind.upper() == "BREAKING":
        return "Removed"
    return _PREFIX_TO_SUBSECTION.get(kind.lower(), "Changed")


def insert_unreleased_entry(existing_changelog: str, entry: str, kind: str) -> str:
    """Insert `entry` under the right `### <Subsection>` of `## [Unreleased]`.

    - Creates the Keep-a-Changelog header if missing.
    - Creates the `## [Unreleased]` section if missing (positioned above the
      first existing release block, or at the top below the header).
    - Creates the appropriate `### <Subsection>` if missing inside Unreleased.
    - Appends to the bottom of the subsection (preserves chronological order:
      oldest entries appear first within a subsection).
    """
    text = _ensure_header(existing_changelog)
    subsection = _sub_for_kind(kind)

    # Locate the Unreleased section.
    unreleased_match = re.search(
        r"^## \[Unreleased\][^\n]*\n", text, re.MULTILINE
    )
    if unreleased_match is None:
        # Insert a fresh Unreleased block before the first ## [version] header.
        first_release = re.search(r"^## \[\d", text, re.MULTILINE)
        unreleased_block = f"## [Unreleased]\n\n### {subsection}\n{entry}\n\n"
        if first_release:
            insertion_point = first_release.start()
            return text[:insertion_point] + unreleased_block + text[insertion_point:]
        # No releases yet — append after the header.
        return text.rstrip() + "\n\n" + unreleased_block.rstrip() + "\n"

    # Unreleased exists — find its bounds (up to next ## section or EOF).
    unreleased_start = unreleased_match.end()
    next_section = re.search(r"^## ", text[unreleased_start:], re.MULTILINE)
    if next_section:
        unreleased_end = unreleased_start + next_section.start()
    else:
        unreleased_end = len(text)
    body = text[unreleased_start:unreleased_end]

    # Find or create the target subsection inside Unreleased.
    sub_pattern = re.compile(rf"^### {re.escape(subsection)}\s*\n", re.MULTILINE)
    sub_match = sub_pattern.search(body)
    if sub_match:
        # Append entry at the end of this subsection (before the next ###
        # subsection or end of unreleased body).
        sub_body_start = sub_match.end()
        next_sub = re.search(r"^### ", body[sub_body_start:], re.MULTILINE)
        if next_sub:
            sub_body_end = sub_body_start + next_sub.start()
        else:
            sub_body_end = len(body)
        sub_body = body[sub_body_start:sub_body_end].rstrip()
        new_sub_body = (sub_body + "\n" + entry + "\n\n") if sub_body else (entry + "\n\n")
        new_body = body[:sub_body_start] + new_sub_body + body[sub_body_end:]
    else:
        # Subsection missing — create it. Insert in canonical order.
        new_subsection = f"### {subsection}\n{entry}\n\n"
        new_body = _insert_subsection_in_order(body, subsection, new_subsection)

    return text[:unreleased_start] + new_body + text[unreleased_end:]


def _insert_subsection_in_order(unreleased_body: str, new_sub: str, block: str) -> str:
    """Insert `block` (a full `### Sub\\n...\\n\\n` chunk) into the Unreleased
    body, positioned according to `_SUBSECTION_ORDER`.
    """
    # Find existing subsections in order of appearance.
    new_sub_idx = (
        _SUBSECTION_ORDER.index(new_sub) if new_sub in _SUBSECTION_ORDER else len(_SUBSECTION_ORDER)
    )
    insertion_point = None
    for match in re.finditer(r"^### (\w+)", unreleased_body, re.MULTILINE):
        existing_sub = match.group(1)
        existing_idx = (
            _SUBSECTION_ORDER.index(existing_sub) if existing_sub in _SUBSECTION_ORDER else len(_SUBSECTION_ORDER)
        )
        if existing_idx > new_sub_idx:
            insertion_point = match.start()
            break
    if insertion_point is None:
        # Append at the end of the Unreleased body.
        body = unreleased_body.rstrip()
        return (body + "\n\n" + block) if body else block
    return unreleased_body[:insertion_point] + block + unreleased_body[insertion_point:]
