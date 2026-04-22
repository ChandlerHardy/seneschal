"""Persistence for posted reviews, so the MCP server can surface them
to local Claude Code sessions later.

Each successful review writes:
    ~/.seneschal/reviews/<owner>/<repo>/<pr>.md

with YAML frontmatter carrying the PR number, verdict, timestamp, and
GitHub review URL. The body is the same markdown that was posted as
the review on GitHub.

This module is intentionally dependency-free (stdlib only) so it can
be imported both by the webhook handler (app.py) and the MCP server
without dragging Flask/requests/etc. into the MCP process.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

# Root of the per-review markdown files. Override via env for tests.
STORE_ROOT = os.environ.get(
    "SENESCHAL_REVIEW_STORE",
    os.path.expanduser("~/.seneschal/reviews"),
)

_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n(.*)$",
    re.DOTALL,
)

_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")


@dataclass(frozen=True)
class ReviewRecord:
    """One persisted review.

    Frontmatter v2 (P1) added optional fields at the end of the dataclass:
    `head_sha`, `merged_at`, `followups_filed`. They have safe defaults so
    v1 records (no new keys) round-trip with the same code path.
    """

    repo: str           # "owner/name"
    pr_number: int
    verdict: str        # "APPROVE" | "REQUEST_CHANGES" | "COMMENT" | "UNKNOWN"
    timestamp: str      # ISO-8601 UTC, e.g. "2026-04-18T15:42:00Z"
    url: str            # https://github.com/<owner>/<repo>/pull/<N>#pullrequestreview-123
    body: str           # the posted review markdown
    head_sha: str = ""  # PR head SHA at review time (v2)
    merged_at: Optional[str] = None  # ISO-8601 UTC, set by mark_merged (v2)
    followups_filed: List[int] = field(default_factory=list)  # issue numbers (v2)

    def summary(self) -> dict:
        """A compact representation suitable for MCP tool responses."""
        return {
            "repo": self.repo,
            "pr_number": self.pr_number,
            "verdict": self.verdict,
            "timestamp": self.timestamp,
            "url": self.url,
        }


def _validate_repo_slug(repo_slug: str) -> None:
    """Raise ValueError if repo_slug isn't a simple owner/repo form.

    Guards against path traversal via the repo_slug parameter (MCP
    clients are local but we still defend).
    """
    if not _REPO_SLUG_RE.match(repo_slug):
        raise ValueError(f"invalid repo slug: {repo_slug!r}")


def _repo_dir(repo_slug: str) -> Path:
    _validate_repo_slug(repo_slug)
    return Path(STORE_ROOT) / repo_slug


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def save_review(
    repo_slug: str,
    pr_number: int,
    verdict: str,
    url: str,
    body: str,
    *,
    timestamp: Optional[str] = None,
    head_sha: Optional[str] = None,
    merged_at: Optional[str] = None,
    followups_filed: Optional[List[int]] = None,
) -> Path:
    """Write a review to disk. Creates parent dirs.

    Returns the absolute path written. Idempotent per (repo, pr_number):
    re-saving the same PR overwrites the previous file.

    v2 fields (head_sha/merged_at/followups_filed) are written into the
    frontmatter only when non-empty so v1 callers produce v1-shaped files.
    """
    _validate_repo_slug(repo_slug)
    if not isinstance(pr_number, int) or pr_number <= 0:
        raise ValueError(f"invalid pr_number: {pr_number!r}")
    out_dir = _repo_dir(repo_slug)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{pr_number}.md"

    ts = timestamp or _now_iso()
    meta = {
        "pr_number": int(pr_number),
        "verdict": str(verdict or "UNKNOWN"),
        "timestamp": str(ts),
        "url": str(url or ""),
    }
    if head_sha:
        meta["head_sha"] = str(head_sha)
    if merged_at:
        meta["merged_at"] = str(merged_at)
    if followups_filed:
        meta["followups_filed"] = sorted({int(n) for n in followups_filed})

    frontmatter = json.dumps(meta, indent=2)
    content = f"---\n{frontmatter}\n---\n{body or ''}"
    out_path.write_text(content)
    return out_path


def _parse_review_file(path: Path, repo_slug: str) -> Optional[ReviewRecord]:
    try:
        raw = path.read_text()
    except OSError:
        return None
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        return None
    try:
        meta = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(meta, dict):
        return None
    try:
        pr_number = int(meta.get("pr_number", 0))
    except (TypeError, ValueError):
        return None

    # v2 fields are optional. Defaults preserve v1-record semantics.
    head_sha_raw = meta.get("head_sha", "")
    head_sha = str(head_sha_raw) if head_sha_raw is not None else ""
    merged_at_raw = meta.get("merged_at")
    merged_at = str(merged_at_raw) if merged_at_raw else None
    followups_raw = meta.get("followups_filed") or []
    followups_filed: List[int] = []
    if isinstance(followups_raw, list):
        for n in followups_raw:
            try:
                followups_filed.append(int(n))
            except (TypeError, ValueError):
                continue

    return ReviewRecord(
        repo=repo_slug,
        pr_number=pr_number,
        verdict=str(meta.get("verdict", "UNKNOWN")),
        timestamp=str(meta.get("timestamp", "")),
        url=str(meta.get("url", "")),
        body=m.group(2),
        head_sha=head_sha,
        merged_at=merged_at,
        followups_filed=followups_filed,
    )


def list_reviews(repo_slug: str, limit: int = 10) -> List[ReviewRecord]:
    """Return up to `limit` most-recent reviews for `repo_slug`, newest first.

    Recency is inferred from the PR number (higher = newer), which isn't
    strictly monotonic with time but is good enough for "show me recent
    reviews". Empty list if the repo has no reviews on disk.
    """
    _validate_repo_slug(repo_slug)
    out_dir = _repo_dir(repo_slug)
    if not out_dir.is_dir():
        return []
    files: List[Path] = []
    for p in out_dir.iterdir():
        if p.suffix == ".md" and p.stem.isdigit():
            files.append(p)
    files.sort(key=lambda p: int(p.stem), reverse=True)
    out: List[ReviewRecord] = []
    for p in files[: max(0, int(limit))]:
        rec = _parse_review_file(p, repo_slug)
        if rec is not None:
            out.append(rec)
    return out


def get_review(repo_slug: str, pr_number: int) -> Optional[ReviewRecord]:
    """Return the review for (repo_slug, pr_number), or None if missing."""
    _validate_repo_slug(repo_slug)
    path = _repo_dir(repo_slug) / f"{int(pr_number)}.md"
    if not path.is_file():
        return None
    return _parse_review_file(path, repo_slug)


def last_review(repo_slug: str) -> Optional[ReviewRecord]:
    """Return the most recent review for `repo_slug`, or None."""
    reviews = list_reviews(repo_slug, limit=1)
    return reviews[0] if reviews else None


def mark_merged(
    repo_slug: str,
    pr_number: int,
    merged_at: str,
    followup_issue_numbers: List[int],
) -> Optional[Path]:
    """Update an existing review record to reflect a merge.

    - Reads the existing review (returns None if missing — caller should
      treat that as "no stored review to annotate" and move on).
    - Writes a new file with the same body but updated frontmatter, adding
      `merged_at` and merging `followup_issue_numbers` into any existing
      `followups_filed` list (deduplicated, sorted).
    - Atomic: writes to a sibling tempfile, then `os.replace`. Borrows the
      pattern from `review_memory.save` so a crash mid-write can't leave a
      half-written frontmatter the next read would barf on.
    """
    _validate_repo_slug(repo_slug)
    if not isinstance(pr_number, int) or pr_number <= 0:
        return None
    target = _repo_dir(repo_slug) / f"{int(pr_number)}.md"
    if not target.is_file():
        return None
    existing = _parse_review_file(target, repo_slug)
    if existing is None:
        return None

    merged_followups = sorted(
        {int(n) for n in (existing.followups_filed or [])}
        | {int(n) for n in (followup_issue_numbers or [])}
    )

    meta = {
        "pr_number": existing.pr_number,
        "verdict": existing.verdict,
        "timestamp": existing.timestamp,
        "url": existing.url,
    }
    if existing.head_sha:
        meta["head_sha"] = existing.head_sha
    if merged_at:
        meta["merged_at"] = str(merged_at)
    elif existing.merged_at:
        meta["merged_at"] = existing.merged_at
    if merged_followups:
        meta["followups_filed"] = merged_followups

    frontmatter = json.dumps(meta, indent=2)
    new_content = f"---\n{frontmatter}\n---\n{existing.body}"

    # Atomic write via mkstemp sibling — same pattern as review_memory.save.
    target_dir = str(target.parent)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target_dir,
    )
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(new_content)
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return target


def get_repo_memory(repo_slug: str, repo_root: str) -> str:
    """Read the repo's own review-memory markdown (curated rules history).

    Returns the file contents, or empty string if the file doesn't exist.
    Checks the two canonical filenames: `.seneschal-memory.md` and the
    legacy `.ch-code-reviewer-memory.md`.
    """
    _validate_repo_slug(repo_slug)
    for name in (".seneschal-memory.md", ".ch-code-reviewer-memory.md"):
        p = os.path.join(repo_root, name)
        if os.path.isfile(p):
            try:
                with open(p, "r") as fh:
                    return fh.read()
            except OSError:
                return ""
    return ""
