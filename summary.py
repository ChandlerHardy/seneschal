"""PR diff summary generator.

Produces a short "what this PR changes" summary from file metadata alone,
no Claude call needed. Helps reviewers (human or AI) orient at a glance.
"""

from __future__ import annotations

from collections import Counter
from typing import List, Sequence

from risk import PRFile


def _category(filename: str) -> str:
    lower = filename.lower()
    # Infra checks run first because they use file extensions that would
    # otherwise match "config" (e.g. .github/workflows/ci.yml).
    if "dockerfile" in lower or "docker-compose" in lower or ".github/workflows/" in lower:
        return "infra"
    if "test" in lower or "_spec" in lower or ".spec." in lower:
        return "tests"
    if lower.endswith((".md", ".mdx", ".rst", ".txt")):
        return "docs"
    if any(lower.endswith(ext) for ext in (".yml", ".yaml", ".toml", ".json", ".ini")):
        return "config"
    if any(lower.endswith(ext) for ext in (".py", ".go", ".ts", ".tsx", ".js", ".jsx", ".swift", ".php", ".vue", ".rs")):
        return "code"
    return "other"


def summarize_diff(files: Sequence[PRFile]) -> str:
    """Return a 1-2 sentence summary of what kinds of files changed."""
    if not files:
        return "_(empty PR)_"

    total_files = len(files)
    total_adds = sum(f.additions for f in files)
    total_dels = sum(f.deletions for f in files)

    added = [f for f in files if f.status == "added"]
    removed = [f for f in files if f.status == "removed"]
    renamed = [f for f in files if f.status == "renamed"]

    categories = Counter(_category(f.filename) for f in files)

    def cat_phrase():
        parts: List[str] = []
        for cat in ("code", "tests", "docs", "config", "infra", "other"):
            n = categories.get(cat, 0)
            if n:
                parts.append(f"{n} {cat}")
        return ", ".join(parts)

    shape_parts: List[str] = []
    if added:
        shape_parts.append(f"{len(added)} new")
    if removed:
        shape_parts.append(f"{len(removed)} deleted")
    if renamed:
        shape_parts.append(f"{len(renamed)} renamed")
    shape = f" ({', '.join(shape_parts)})" if shape_parts else ""

    return (
        f"Touches {total_files} files{shape} across {cat_phrase()}; "
        f"+{total_adds}/-{total_dels} lines."
    )
