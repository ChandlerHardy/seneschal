"""Pre-computed context loader for PR reviews.

Given a parsed diff, find callers of newly-added or modified public
symbols by shelling out to ripgrep against the cloned repo. Output is a
structured report that gets inlined into the Claude review prompt so the
reviewer doesn't have to remember to search for context itself.

Ripgrep is assumed available on the system. On systems without rg, the
loader falls back to `grep -rn`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from diff_parser import parse_unified_diff
from test_gaps import extract_added_symbols, is_test_file


@dataclass
class CallSite:
    file: str
    line: int
    preview: str


@dataclass
class SymbolContext:
    symbol: str
    defined_in: str
    callers: List[CallSite] = field(default_factory=list)

    @property
    def caller_count(self) -> int:
        return len(self.callers)


@dataclass
class BlastRadius:
    symbols: List[SymbolContext] = field(default_factory=list)

    @property
    def total_callers(self) -> int:
        return sum(s.caller_count for s in self.symbols)

    def summary(self) -> str:
        if not self.symbols:
            return "_(no touched symbols with callers)_"
        lines: List[str] = []
        for sc in self.symbols:
            if sc.caller_count == 0:
                lines.append(f"- `{sc.symbol}` ({sc.defined_in}) — no callers found")
                continue
            head = f"- `{sc.symbol}` ({sc.defined_in}) — {sc.caller_count} caller(s):"
            lines.append(head)
            for site in sc.callers[:5]:
                snippet = site.preview.strip().replace("`", "'")
                lines.append(f"  - {site.file}:{site.line} — `{snippet[:80]}`")
            if sc.caller_count > 5:
                lines.append(f"  - ...and {sc.caller_count - 5} more")
        return "\n".join(lines)

    def as_prompt_section(self) -> str:
        if not self.symbols:
            return ""
        return (
            "## Blast Radius (pre-computed callers of touched symbols)\n\n"
            + self.summary()
            + "\n\nUse this to reason about whether the change breaks any call site. "
            "If a caller's expectations no longer hold, flag it in the review.\n"
        )


# ----- Ripgrep / grep invocation -----

def _rg_binary() -> Optional[str]:
    return shutil.which("rg")


def _grep_binary() -> Optional[str]:
    return shutil.which("grep")


def _run_rg(pattern: str, repo_dir: str, max_lines: int = 30) -> List[str]:
    """Run ripgrep. Returns list of 'path:line:content' strings."""
    rg = _rg_binary()
    if rg:
        try:
            result = subprocess.run(
                [rg, "--fixed-strings", "-n", "--no-heading", "--max-count", str(max_lines), pattern, repo_dir],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode in (0, 1):
                return result.stdout.strip().splitlines()
        except subprocess.TimeoutExpired:
            return []
        except OSError:
            return []

    grep = _grep_binary()
    if grep:
        try:
            result = subprocess.run(
                [grep, "-rn", "--", pattern, repo_dir],
                capture_output=True,
                text=True,
                timeout=10,
            )
            lines = result.stdout.strip().splitlines()
            return lines[:max_lines]
        except (subprocess.TimeoutExpired, OSError):
            return []
    return []


def _parse_rg_line(line: str, repo_dir: str) -> Optional[CallSite]:
    # "path:lineno:content"
    parts = line.split(":", 2)
    if len(parts) != 3:
        return None
    path, lineno_str, preview = parts
    try:
        lineno = int(lineno_str)
    except ValueError:
        return None
    if path.startswith(repo_dir + "/"):
        path = path[len(repo_dir) + 1:]
    return CallSite(file=path, line=lineno, preview=preview)


# ----- Caller search with filtering -----

_IGNORE_DIR_PREFIXES = (
    "node_modules/",
    "vendor/",
    "dist/",
    "build/",
    ".next/",
    "target/",
    ".git/",
    "__pycache__/",
)


def _is_ignored_path(relative: str) -> bool:
    for prefix in _IGNORE_DIR_PREFIXES:
        if prefix in relative:
            return True
    return False


def find_callers(symbol: str, defined_in: str, repo_dir: str, max_results: int = 20) -> List[CallSite]:
    """Find call sites for `symbol(` in the repo, excluding the definition file."""
    if not repo_dir or not os.path.isdir(repo_dir):
        return []
    raw = _run_rg(f"{symbol}(", repo_dir, max_lines=max_results * 3)
    sites: List[CallSite] = []
    seen: set = set()
    for line in raw:
        site = _parse_rg_line(line, repo_dir)
        if site is None:
            continue
        if site.file == defined_in:
            continue
        if _is_ignored_path(site.file):
            continue
        key = (site.file, site.line)
        if key in seen:
            continue
        seen.add(key)
        sites.append(site)
        if len(sites) >= max_results:
            break
    return sites


def compute_blast_radius(
    diff_text: str,
    repo_dir: str,
    max_symbols: int = 10,
    max_callers_per_symbol: int = 20,
) -> BlastRadius:
    """Compute caller context for newly-added public symbols in a PR diff."""
    files = parse_unified_diff(diff_text)
    contexts: List[SymbolContext] = []

    for filename, added in files.items():
        if is_test_file(filename):
            continue
        symbols = extract_added_symbols(filename, added)
        for sym in symbols:
            callers = find_callers(sym, filename, repo_dir, max_results=max_callers_per_symbol)
            contexts.append(SymbolContext(symbol=sym, defined_in=filename, callers=callers))
            if len(contexts) >= max_symbols:
                return BlastRadius(symbols=contexts)
    return BlastRadius(symbols=contexts)
