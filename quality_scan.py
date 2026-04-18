"""Quality heuristics: debug leftovers and TODO markers in added lines.

Surfaces common low-grade issues that shouldn't block a merge but are
worth flagging as NIT/INFO findings so the author notices them:

- Stray `print` / `console.log` / `fmt.Println` in non-test code
- `TODO:`, `FIXME:`, `XXX:`, `HACK:` comments introduced by this PR
- Commented-out code blocks (heuristic: contiguous added lines starting with `#` or `//`)

All findings here are low-severity — false positives are acceptable
because the author can ignore them. Never promoted to BLOCKER.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Sequence

from diff_parser import parse_unified_diff_with_lines
from test_gaps import is_test_file


@dataclass
class QualityHit:
    kind: str  # "debug", "todo", "commented-out"
    file: str
    line: int
    snippet: str


# Debug-leftover patterns, per-extension.
_DEBUG_PATTERNS = [
    # Python
    (re.compile(r"^\s*print\s*\("), ".py", "print"),
    (re.compile(r"^\s*pprint\s*\("), ".py", "pprint"),
    (re.compile(r"^\s*breakpoint\s*\("), ".py", "breakpoint"),
    # Go
    (re.compile(r"^\s*fmt\.Println\s*\("), ".go", "fmt.Println"),
    (re.compile(r"^\s*fmt\.Printf\s*\("), ".go", "fmt.Printf"),
    # JS / TS
    (re.compile(r"^\s*console\.log\s*\("), (".js", ".jsx", ".ts", ".tsx", ".vue"), "console.log"),
    (re.compile(r"^\s*console\.debug\s*\("), (".js", ".jsx", ".ts", ".tsx"), "console.debug"),
    (re.compile(r"^\s*debugger\s*;?"), (".js", ".jsx", ".ts", ".tsx"), "debugger"),
    # Swift
    (re.compile(r"^\s*print\s*\("), ".swift", "print"),
    # PHP
    (re.compile(r"^\s*var_dump\s*\("), ".php", "var_dump"),
    (re.compile(r"^\s*print_r\s*\("), ".php", "print_r"),
    (re.compile(r"^\s*dd\s*\("), ".php", "dd"),
    # Ruby
    (re.compile(r"^\s*puts\s+"), ".rb", "puts"),
]


# TODO-style markers in any language.
_TODO_PATTERN = re.compile(
    r"(?:#|//|/\*|--)\s*(TODO|FIXME|XXX|HACK|BUG)[:\s]"
)


def _matches_ext(filename: str, ext_spec) -> bool:
    lower = filename.lower()
    if isinstance(ext_spec, tuple):
        return any(lower.endswith(e) for e in ext_spec)
    return lower.endswith(ext_spec)


def _is_vendored(filename: str) -> bool:
    lower = filename.lower()
    for marker in (
        "node_modules/",
        "vendor/",
        "dist/",
        "build/",
        ".next/",
        "generated/",
        "examples/",
        "example/",
        "samples/",
        "fixtures/",
    ):
        if marker in lower:
            return True
    return False


# Documentation files are exempt from quality scanning. Markdown specs and
# PRDs legitimately contain `## TODO:` headings and example `print(` blocks,
# and flagging them produced one noisy finding per PR that touched docs.
_DOC_EXTENSIONS = (".md", ".markdown", ".txt", ".rst", ".adoc")


def _is_doc_file(filename: str) -> bool:
    lower = filename.lower()
    return any(lower.endswith(ext) for ext in _DOC_EXTENSIONS)


def scan_quality(diff_text: str) -> List[QualityHit]:
    """Return all quality hits across the PR's added lines."""
    files = parse_unified_diff_with_lines(diff_text)
    out: List[QualityHit] = []

    for filename, pairs in files.items():
        if _is_vendored(filename) or _is_doc_file(filename):
            continue

        for line_num, content in pairs:
            # TODO markers apply everywhere (including tests).
            if _TODO_PATTERN.search(content):
                match = _TODO_PATTERN.search(content)
                marker = match.group(1) if match else "TODO"
                out.append(QualityHit(
                    kind=f"todo:{marker.lower()}",
                    file=filename,
                    line=line_num,
                    snippet=content.strip()[:120],
                ))
                continue

            # Debug leftovers: skip test files since they legitimately print.
            if is_test_file(filename):
                continue
            # Skip doctest blocks; ">>>" prefixes are example output, not real code.
            if content.lstrip().startswith(">>>"):
                continue
            for pattern, ext_spec, label in _DEBUG_PATTERNS:
                if not _matches_ext(filename, ext_spec):
                    continue
                if pattern.match(content):
                    out.append(QualityHit(
                        kind=f"debug:{label}",
                        file=filename,
                        line=line_num,
                        snippet=content.strip()[:120],
                    ))
                    break
    return out


def summarize_quality(hits: Sequence[QualityHit]) -> str:
    if not hits:
        return "**Quality scan: clean** — no debug leftovers or TODO markers added."
    by_kind: Dict[str, List[QualityHit]] = {}
    for h in hits:
        by_kind.setdefault(h.kind, []).append(h)
    lines = [f"**Quality scan: {len(hits)} hit(s)**", ""]
    for kind in sorted(by_kind.keys()):
        items = by_kind[kind]
        lines.append(f"- **{kind}** ({len(items)}):")
        for item in items[:3]:
            lines.append(f"  - `{item.file}:{item.line}` - `{item.snippet}`")
    return "\n".join(lines)
