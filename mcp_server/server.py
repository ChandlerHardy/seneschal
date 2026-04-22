"""Seneschal MCP server — read-only history + cross-repo knowledge tools.

Exposes nine tools to local Claude Code sessions via the Model Context
Protocol (stdio transport):

Single-repo review history (P0):
    seneschal_last_review(repo)             → summary of most recent review
    seneschal_review_history(repo, limit)   → list of recent review summaries
    seneschal_review_text(repo, pr_number)  → full body of a specific review
    seneschal_repo_memory(repo, repo_root)  → contents of the repo's
                                              .seneschal-memory.md file

Cross-repo knowledge custody (P2):
    seneschal_search_reviews(query, repo, limit)  → FTS across every indexed review
    seneschal_search_adrs(query, repo, limit)     → FTS across discovered ADRs
    seneschal_merged_prs(repo, since, limit)      → merged-PR timeline from the index
    seneschal_followups(repo, status, limit)      → open seneschal-followup issues via GitHub API
    seneschal_dependency_usage(package_name, limit) → grep manifest files for a package

Most tools are read-only against the on-disk review store. `seneschal_followups`
is the exception — it mints an installation token (or uses SENESCHAL_GITHUB_TOKEN
as a PAT fallback) and hits GitHub's issues endpoint. The 404-on-install-missing
case is handled gracefully: repos where the App isn't installed are silently
skipped rather than surfaced as errors.

Run with:
    python -m mcp_server.server
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Optional

import requests

# Ensure the parent dir is on sys.path so `import review_store` etc. work
# when this module is run as `python -m mcp_server.server`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cross_repo  # noqa: E402
import dependency_grep  # noqa: E402
import review_index  # noqa: E402
import seneschal_token  # noqa: E402
from fs_safety import validate_repo_slug  # noqa: E402
from review_store import (  # noqa: E402
    get_repo_memory,
    get_review,
    last_review,
    list_reviews,
)
from secrets_scan import redact as _redact  # noqa: E402

try:
    from fastmcp import FastMCP
except ImportError as e:  # pragma: no cover
    sys.stderr.write(
        f"seneschal-mcp-server: fastmcp is required. "
        f"Install with `pip install fastmcp`. ({e})\n"
    )
    sys.exit(1)


mcp = FastMCP(
    "seneschal",
    instructions=(
        "Seneschal review history, ADR knowledge, and cross-repo stewardship. "
        "Use these tools to look up what previous Seneschal reviews said about a PR, "
        "search reviews or ADRs across all your repos, track merged PRs over time, "
        "audit dependency usage, and list open seneschal-followup issues."
    ),
)


# --------------------------------------------------------------------------
# Shared helpers — index lifecycle, redaction.
# --------------------------------------------------------------------------

# Module-level Index reference. The MCP server is one long-running
# stdio process; we want the first tool call to open + sync, and every
# subsequent call in that session to reuse the same connection. The
# "sync-once" flag now lives on the Index itself (`_synced`) — moving
# it onto the object closes the check-then-set race where two
# concurrent first-calls both saw the module-level sentinel as False
# and both tried to BEGIN IMMEDIATE inside the other's transaction.
_INDEX: Optional[review_index.Index] = None
# Lock protects the open-once initialization. Once `_INDEX` is set,
# all further synchronization happens inside `Index._lock`.
_INDEX_OPEN_LOCK = threading.Lock()


from log import log as _neutral_log  # noqa: E402


def _log(msg: str) -> None:
    """Prefixed wrapper around the neutral stderr logger."""
    _neutral_log(f"[mcp_server] {msg}")


def _get_index() -> review_index.Index:
    """Lazy-open the SQLite index and sync once per process lifetime.

    Re-sync during a live MCP session would be wasteful (it walks the
    whole ~/.seneschal/reviews tree). Operators restart the server to
    re-enumerate after a bulk import — same convention as cross_repo's
    enumeration cache.

    Thread-safety: `_INDEX_OPEN_LOCK` serializes the first-time open.
    The sync-once guard lives on `Index.ensure_synced` where it shares
    the Index's internal RLock, preventing the double-BEGIN-IMMEDIATE
    race that the old module-level `_INDEX_SYNCED` sentinel had.
    """
    global _INDEX
    if _INDEX is None:
        with _INDEX_OPEN_LOCK:
            if _INDEX is None:
                _INDEX = review_index.open_index()
    try:
        _INDEX.ensure_synced()
    except Exception as e:  # noqa: BLE001 — defensive boundary
        _log(f"initial index sync failed: {e}; continuing with partial index")
        # `ensure_synced` leaves `_synced` False on failure so the
        # next call retries — same semantics as before, minus the race.
    return _INDEX


def _summary_or_none(rec):
    """Convert a ReviewRecord to a summary dict, or return None if missing."""
    return rec.summary() if rec is not None else None


# --------------------------------------------------------------------------
# P0 tools — single-repo review history (unchanged).
# --------------------------------------------------------------------------


@mcp.tool
def seneschal_last_review(repo: str) -> dict | None:
    """Return a summary of the most recent Seneschal review for `repo`.

    Args:
        repo: GitHub repo slug in "owner/name" form (e.g. "ChandlerHardy/seneschal").

    Returns:
        A dict with keys: repo, pr_number, verdict, timestamp, url. Or
        None if no reviews have been persisted for that repo.
    """
    try:
        rec = last_review(repo)
    except ValueError as e:
        return {"error": str(e)}
    return _summary_or_none(rec)


@mcp.tool
def seneschal_review_history(repo: str, limit: int = 10) -> list[dict]:
    """Return up to `limit` most-recent review summaries for `repo`, newest first.

    Args:
        repo: GitHub repo slug in "owner/name" form.
        limit: Max number of reviews to return. Default 10, max 100.

    Returns:
        List of summary dicts (same shape as seneschal_last_review), or
        an empty list if the repo has no persisted reviews. If `repo` is
        malformed, returns a one-element list with an error payload.
    """
    try:
        recs = list_reviews(repo, limit=min(max(1, int(limit)), 100))
    except ValueError as e:
        return [{"error": str(e)}]
    return [r.summary() for r in recs]


@mcp.tool
def seneschal_review_text(repo: str, pr_number: int) -> dict | None:
    """Return the full posted review body for (repo, pr_number).

    Args:
        repo: GitHub repo slug in "owner/name" form.
        pr_number: The PR number whose review body to return.

    Returns:
        A dict with keys: summary (metadata dict) and body (markdown).
        None if that PR has no persisted review.
    """
    try:
        rec = get_review(repo, int(pr_number))
    except (ValueError, TypeError) as e:
        return {"error": str(e)}
    if rec is None:
        return None
    # Redact the body before returning — a reviewer's code block could
    # embed a real token or the original PR diff could contain one in an
    # added line. The MCP tool is a new egress channel; snippets in
    # `search_reviews` are already scrubbed via `_redact_snippet`, and
    # we apply the same policy to the full body here for consistency.
    return {"summary": rec.summary(), "body": _redact(rec.body)}


@mcp.tool
def seneschal_repo_memory(repo: str, repo_root: str) -> str:
    """Return the curated review-memory markdown from a repo's working tree.

    Looks for `.seneschal-memory.md` first, then the legacy
    `.ch-code-reviewer-memory.md`. Returns the file contents as a string,
    or an empty string if neither file exists or the repo_root doesn't
    resolve to a directory.

    Args:
        repo: GitHub repo slug (used to validate the caller — not used to
            find the file; `repo_root` is the actual path).
        repo_root: Absolute path to the repo's working tree.
    """
    try:
        return get_repo_memory(repo, repo_root)
    except ValueError as e:
        return f"(error) {e}"


# --------------------------------------------------------------------------
# P2 tools — cross-repo knowledge custody.
# --------------------------------------------------------------------------


@mcp.tool
def seneschal_search_reviews(
    query: str, repo: Optional[str] = None, limit: int = 20
) -> list[dict]:
    """Full-text search across every indexed Seneschal review.

    Answers "which PRs across my repos flagged 'migration' or 'deprecation'?"
    without having to grep the filesystem. Snippets are redacted through
    secrets_scan patterns before being returned.

    Args:
        query: Free-text phrase to search for.
        repo: Optional owner/name filter. If malformed, returns an error payload.
        limit: Max hits to return (default 20, max 200).

    Returns:
        List of dicts with keys: repo, pr_number, verdict, timestamp,
        merged_at, head_sha, url, snippet. One-element error list on
        malformed `repo` slug.
    """
    try:
        idx = _get_index()
        return idx.search_reviews(query, repo=repo, limit=int(limit))
    except ValueError as e:
        return [{"error": str(e)}]
    except Exception as e:  # noqa: BLE001
        _log(f"search_reviews failed: {e}")
        return [{"error": f"search failed: {e}"}]


@mcp.tool
def seneschal_search_adrs(
    query: str, repo: Optional[str] = None, limit: int = 10
) -> list[dict]:
    """Full-text search across ADRs discovered in every known repo.

    Args:
        query: Free-text phrase to search for.
        repo: Optional owner/name filter.
        limit: Max ADRs to return (default 10, max 100).

    Returns:
        List of dicts with keys: repo, path, id, title, status, excerpt.
    """
    try:
        idx = _get_index()
        return idx.search_adrs(query, repo=repo, limit=int(limit))
    except ValueError as e:
        return [{"error": str(e)}]
    except Exception as e:  # noqa: BLE001
        _log(f"search_adrs failed: {e}")
        return [{"error": f"search failed: {e}"}]


@mcp.tool
def seneschal_merged_prs(
    repo: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    """Return merged PRs from the index, newest-first.

    Args:
        repo: Optional owner/name filter.
        since: ISO-8601 lower bound on `merged_at` (inclusive).
        limit: Max rows to return (default 20, max 200).

    Returns:
        List of dicts with keys: repo, pr_number, verdict, timestamp,
        merged_at, head_sha, url.
    """
    try:
        idx = _get_index()
        return idx.list_merged_prs(repo=repo, since=since, limit=int(limit))
    except ValueError as e:
        return [{"error": str(e)}]
    except Exception as e:  # noqa: BLE001
        _log(f"list_merged_prs failed: {e}")
        return [{"error": f"query failed: {e}"}]


@mcp.tool
def seneschal_followups(
    repo: Optional[str] = None,
    status: str = "open",
    limit: int = 50,
) -> list[dict]:
    """List open seneschal-followup issues across known repos.

    Iterates GitHub's issues endpoint (filtered by the `seneschal-followup`
    label) for each known repo (or just `repo` if specified). Repos where
    the GitHub App isn't installed are silently skipped — the MCP tool's
    job is to answer "what's on my plate?", not surface infra issues.

    Args:
        repo: Optional owner/name to scope to. If absent, iterates every
            `cross_repo.known_repos` entry.
        status: Issue state filter (`open`, `closed`, `all`). Default `open`.
        limit: Max total issues to return across all repos. Default 50.

    Returns:
        List of dicts with keys: repo, number, title, state, url.
    """
    # Validate inputs up-front.
    if repo is not None:
        try:
            validate_repo_slug(repo)
        except ValueError as e:
            return [{"error": str(e)}]
    if status not in ("open", "closed", "all"):
        return [{"error": f"invalid status: {status!r} (want open/closed/all)"}]
    limit = max(1, min(int(limit), 200))

    if repo is not None:
        slugs = [repo]
    else:
        try:
            slugs = [kr.slug for kr in cross_repo.known_repos()]
        except Exception as e:  # noqa: BLE001
            return [{"error": f"known_repos failed: {e}"}]

    out: list[dict] = []
    for slug in slugs:
        if len(out) >= limit:
            break
        # Mint a token per-slug. `AppNotInstalledError` → skip quietly.
        try:
            token = seneschal_token.mint_installation_token(slug)
        except seneschal_token.AppNotInstalledError:
            _log(f"App not installed on {slug}; skipping")
            continue
        except seneschal_token.TokenMintError as e:
            # A single-repo request that can't mint — surface as error.
            if repo is not None:
                return [{"error": f"token mint failed for {slug}: {e}"}]
            # Cross-repo sweep: skip and continue.
            _log(f"token mint failed for {slug}: {e}; skipping")
            continue
        except ValueError as e:
            return [{"error": str(e)}]

        url = f"https://api.github.com/repos/{slug}/issues"
        params = {"state": status, "labels": "seneschal-followup", "per_page": 100}
        try:
            resp = requests.get(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
                params=params,
                timeout=10,
            )
            resp.raise_for_status()
            issues = resp.json()
        except (requests.HTTPError, requests.RequestException) as e:
            _log(f"issues fetch failed for {slug}: {e}")
            if repo is not None:
                return [{"error": f"issues fetch failed for {slug}: {e}"}]
            continue
        if not isinstance(issues, list):
            continue
        for issue in issues:
            if len(out) >= limit:
                break
            # Filter out PRs (GitHub conflates issues + PRs on the same endpoint).
            if isinstance(issue, dict) and "pull_request" in issue:
                continue
            out.append(
                {
                    "repo": slug,
                    "number": int(issue.get("number", 0)) if isinstance(issue, dict) else 0,
                    "title": _redact(str(issue.get("title", ""))) if isinstance(issue, dict) else "",
                    "state": str(issue.get("state", "")) if isinstance(issue, dict) else "",
                    "url": str(issue.get("html_url", "")) if isinstance(issue, dict) else "",
                }
            )
    return out


@mcp.tool
def seneschal_dependency_usage(
    package_name: str, limit: int = 50
) -> list[dict]:
    """Grep every known repo's manifests for a package reference.

    Answers "if this package has a CVE, which repos do I need to patch?"
    No parsing — straight substring match across package.json,
    requirements.txt, pyproject.toml, go.mod, Package.swift, Cargo.toml.

    Args:
        package_name: Package name / substring to match.
        limit: Max total hits to return (default 50).

    Returns:
        List of dicts with keys: repo, path, line, matched.
    """
    try:
        hits = dependency_grep.scan_all(package_name, limit=int(limit))
    except Exception as e:  # noqa: BLE001
        _log(f"dependency scan failed: {e}")
        return [{"error": f"scan failed: {e}"}]
    return [
        {"repo": h.repo, "path": h.path, "line": h.line, "matched": h.matched}
        for h in hits
    ]


def main():  # pragma: no cover
    """Run the MCP server over stdio (standard MCP transport)."""
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
