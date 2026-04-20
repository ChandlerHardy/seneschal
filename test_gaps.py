"""Detect new functions/classes added in a PR that lack test coverage.

Works from a raw unified diff. Parses added lines, extracts symbol
declarations from source files, and checks whether any test file in the
same PR mentions those symbols. Supports Python, Go, and JS/TS.

Gated on the target repo having a detectable test framework — "add a test
for X" is not actionable advice in a repo with zero test infrastructure,
and a diff against one shouldn't drown the reviewer in noise suggestions.
See `has_test_framework()` for the detection heuristic.
"""

from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set

from diff_parser import parse_unified_diff, parse_unified_diff_with_lines

# Re-exported for backward compatibility with the existing test_gaps test
# suite and any in-flight callers. New code should import directly from
# `diff_parser`.
__all__ = [
    "parse_unified_diff",
    "parse_unified_diff_with_lines",
    "has_test_framework",
    "is_test_file",
    "extract_added_symbols",
    "collect_referenced_identifiers",
    "find_test_gaps",
    "summarize_gaps",
    "TestGap",
]


def has_test_framework(repo_dir: Optional[str]) -> bool:
    """Return True if `repo_dir` has any sign of a test framework.

    Returns True on `None` or a missing directory so callers that haven't
    yet plumbed repo_dir through keep the previous behavior. Detection is
    deliberately broad: we want a "definitely no tests anywhere" signal,
    not a precise framework identification.

    Heuristics (any ONE is enough):
      - JS/TS: package.json has a `test` script, OR any jest/vitest config
               file, OR a __tests__ dir, OR any *.test.ts/*.spec.ts file
      - Python: pytest.ini / pyproject.toml with [tool.pytest], OR a
               tests/ dir with at least one test_*.py or *_test.py file
      - Go: any *_test.go anywhere in the tree (excluding vendor/ and
            node_modules/)
      - Swift: a Tests/ dir anywhere in the tree
      - Generic: a test/ or tests/ directory in the repo root
    """
    if not repo_dir or not os.path.isdir(repo_dir):
        # Conservative: if we don't know, don't suppress findings.
        return True

    # --- JS/TS ---
    pkg_path = os.path.join(repo_dir, "package.json")
    if os.path.isfile(pkg_path):
        try:
            with open(pkg_path) as fh:
                pkg = json.load(fh)
        except (json.JSONDecodeError, OSError):
            pkg = {}
        scripts = pkg.get("scripts", {}) if isinstance(pkg.get("scripts"), dict) else {}
        if isinstance(scripts, dict) and any(
            k == "test" or k.startswith("test:") for k in scripts
        ):
            return True
        deps = {}
        for key in ("dependencies", "devDependencies"):
            if isinstance(pkg.get(key), dict):
                deps.update(pkg[key])
        if any(name in deps for name in ("jest", "vitest", "mocha", "ava", "playwright", "@testing-library/react")):
            return True

    for pattern in ("jest.config.*", "vitest.config.*", "playwright.config.*"):
        if glob.glob(os.path.join(repo_dir, pattern)):
            return True
    for subdir in ("__tests__", "test", "tests"):
        candidate = os.path.join(repo_dir, subdir)
        if os.path.isdir(candidate):
            return True

    # --- Python ---
    if os.path.isfile(os.path.join(repo_dir, "pytest.ini")):
        return True
    pyproject = os.path.join(repo_dir, "pyproject.toml")
    if os.path.isfile(pyproject):
        try:
            with open(pyproject) as fh:
                if "[tool.pytest" in fh.read():
                    return True
        except OSError:
            pass

    # --- Go ---
    try:
        for root, dirs, filenames in os.walk(repo_dir):
            # Skip vendored and dependency trees to keep this fast on large repos.
            dirs[:] = [
                d for d in dirs
                if d not in {"node_modules", "vendor", ".git", "venv", ".venv"}
            ]
            for name in filenames:
                if name.endswith("_test.go"):
                    return True
    except OSError:
        pass

    # --- Swift / generic ---
    # Tests/ anywhere is a strong signal (SPM / XCTest convention).
    for root, dirs, _ in os.walk(repo_dir):
        dirs[:] = [
            d for d in dirs
            if d not in {"node_modules", "vendor", ".git", "venv", ".venv"}
        ]
        if "Tests" in dirs:
            return True
        # Bound the walk — don't crawl deep if nothing matched yet.
        if root.count(os.sep) - repo_dir.count(os.sep) > 4:
            dirs[:] = []

    return False


# ----- Symbol extraction from source -----

_PY_FUNC = re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z_0-9]*)\s*\(")
_PY_CLASS = re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z_0-9]*)\s*[\(:]")

_GO_FUNC = re.compile(
    r"^\s*func\s+(?:\([^\)]+\)\s+)?([A-Z][A-Za-z_0-9]*)\s*\("
)
_GO_TYPE = re.compile(r"^\s*type\s+([A-Z][A-Za-z_0-9]*)\s+(?:struct|interface|func)")

_JS_FUNC = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z_0-9$]*)\s*\("
)
_JS_ARROW = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z_0-9$]*)\s*="
    r"\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][A-Za-z_0-9$]*)\s*=>"
)
_JS_CLASS = re.compile(r"^\s*(?:export\s+)?class\s+([A-Z][A-Za-z_0-9$]*)")

# Swift: func / class / struct / protocol / enum. Ignores private/fileprivate.
_SWIFT_FUNC = re.compile(
    r"^\s*(?:public\s+|internal\s+|open\s+)?(?:static\s+|class\s+)?func\s+([A-Za-z_][A-Za-z_0-9]*)\s*[<(]"
)
_SWIFT_TYPE = re.compile(
    r"^\s*(?:public\s+|internal\s+|open\s+|final\s+)*(?:class|struct|protocol|enum|actor)\s+([A-Z][A-Za-z_0-9]*)"
)
_SWIFT_PRIVATE_HINT = re.compile(r"^\s*(private|fileprivate)\b")

# PHP: function / class (namespaces ignored — we just grab the names).
_PHP_FUNC = re.compile(
    r"^\s*(?:public\s+|protected\s+)?(?:static\s+)?function\s+([A-Za-z_][A-Za-z_0-9]*)\s*\("
)
_PHP_CLASS = re.compile(r"^\s*(?:abstract\s+|final\s+)?class\s+([A-Z][A-Za-z_0-9]*)")


def _extract_python(lines: Sequence[str]) -> List[str]:
    out: List[str] = []
    for line in lines:
        for rx in (_PY_FUNC, _PY_CLASS):
            m = rx.match(line)
            if m:
                name = m.group(1)
                if name.startswith("_"):
                    continue  # private
                out.append(name)
    return out


def _extract_go(lines: Sequence[str]) -> List[str]:
    out: List[str] = []
    for line in lines:
        for rx in (_GO_FUNC, _GO_TYPE):
            m = rx.match(line)
            if m:
                out.append(m.group(1))
    return out


def _extract_js(lines: Sequence[str]) -> List[str]:
    out: List[str] = []
    for line in lines:
        for rx in (_JS_FUNC, _JS_ARROW, _JS_CLASS):
            m = rx.match(line)
            if m:
                name = m.group(1)
                if name.startswith("_"):
                    continue
                out.append(name)
    return out


def _extract_swift(lines: Sequence[str]) -> List[str]:
    out: List[str] = []
    for line in lines:
        if _SWIFT_PRIVATE_HINT.match(line):
            continue
        for rx in (_SWIFT_FUNC, _SWIFT_TYPE):
            m = rx.match(line)
            if m:
                out.append(m.group(1))
    return out


def _extract_php(lines: Sequence[str]) -> List[str]:
    out: List[str] = []
    for line in lines:
        # Skip private-qualified functions.
        if re.match(r"^\s*private\s+", line):
            continue
        for rx in (_PHP_FUNC, _PHP_CLASS):
            m = rx.match(line)
            if m:
                out.append(m.group(1))
    return out


def extract_added_symbols(filename: str, added_lines: Sequence[str]) -> List[str]:
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext == "py":
        return _extract_python(added_lines)
    if ext == "go":
        return _extract_go(added_lines)
    if ext in ("js", "jsx", "ts", "tsx", "mjs", "cjs", "vue"):
        return _extract_js(added_lines)
    if ext == "swift":
        return _extract_swift(added_lines)
    if ext == "php":
        return _extract_php(added_lines)
    return []


# ----- Test-file detection and symbol references -----

def is_test_file(filename: str) -> bool:
    lower = filename.lower()
    if lower.endswith("_test.go"):
        return True
    if lower.endswith(".test.ts") or lower.endswith(".test.tsx"):
        return True
    if lower.endswith(".test.js") or lower.endswith(".test.jsx"):
        return True
    if lower.endswith(".spec.ts") or lower.endswith(".spec.tsx"):
        return True
    if lower.endswith(".spec.js") or lower.endswith(".spec.jsx"):
        return True
    base = lower.rsplit("/", 1)[-1]
    if base.startswith("test_") and base.endswith(".py"):
        return True
    if "/tests/" in lower or "/test/" in lower or "__tests__" in lower:
        return True
    return False


_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z_0-9$]*")


def collect_referenced_identifiers(lines: Sequence[str]) -> Set[str]:
    out: Set[str] = set()
    for line in lines:
        for ident in _IDENTIFIER.findall(line):
            out.add(ident)
    return out


# ----- Main gap-finding -----

@dataclass
class TestGap:
    symbol: str
    file: str
    line: int = 0

    def __str__(self) -> str:
        return f"`{self.symbol}` ({self.file}:{self.line})" if self.line else f"`{self.symbol}` ({self.file})"


def find_test_gaps(diff_text: str) -> List[TestGap]:
    """Return a list of new symbols added in source files that no test file
    in this PR references.

    Only considers symbols that a test *could* plausibly reference. Private
    helpers (underscore-prefixed in Python/JS, lowercase in Go) are ignored
    because they are not exported and typically tested via their callers.
    """
    files_with_lines = parse_unified_diff_with_lines(diff_text)
    source_symbols: Dict[str, List[tuple]] = {}  # filename -> [(symbol, line)]
    test_identifiers: Set[str] = set()

    for filename, line_pairs in files_with_lines.items():
        lines = [content for _, content in line_pairs]
        if is_test_file(filename):
            test_identifiers |= collect_referenced_identifiers(lines)
            continue
        # Extract symbols with their line numbers.
        symbols_with_lines: List[tuple] = []
        for line_num, content in line_pairs:
            syms = extract_added_symbols(filename, [content])
            for sym in syms:
                symbols_with_lines.append((sym, line_num))
        if symbols_with_lines:
            source_symbols[filename] = symbols_with_lines

    gaps: List[TestGap] = []
    for filename, sym_lines in source_symbols.items():
        for sym, line in sym_lines:
            if sym not in test_identifiers:
                gaps.append(TestGap(symbol=sym, file=filename, line=line))
    return gaps


def summarize_gaps(gaps: Sequence[TestGap]) -> str:
    if not gaps:
        return "**Test coverage: ok** — all new public symbols have test references."
    lines = [f"**Test coverage: {len(gaps)} gap(s)**", ""]
    by_file: Dict[str, List[str]] = {}
    for g in gaps:
        by_file.setdefault(g.file, []).append(g.symbol)
    for filename, syms in sorted(by_file.items()):
        lines.append(f"- `{filename}` — {', '.join(f'`{s}`' for s in syms)}")
    return "\n".join(lines)
