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
from review_index import (  # noqa: E402
    DEFAULT_LIST_LIMIT,
    DEFAULT_SEARCH_LIMIT,
    MAX_LIMIT,
)
from review_store import (  # noqa: E402
    get_repo_memory,
    get_review,
    last_review,
    list_reviews,
)
from secrets_scan import redact as _redact  # noqa: E402


# --------------------------------------------------------------------------
# Error contract helpers.
# --------------------------------------------------------------------------

# Tools that return a list use `[{"error": msg}]`; tools that return a
# single dict use `{"error": msg}`. This pair of helpers is the one
# place that shape lives so the prefix format stays uniform.
#
# We deliberately do NOT echo raw exception strings into tool responses
# — those can carry secrets (GitHub API errors embed tokens in request
# URLs, stack traces leak local paths). `_log` gets the full detail,
# the tool consumer gets a scrubbed, fixed-shape payload.


def _error_list(tool_name: str, context: str) -> list[dict]:
    return [{"error": f"{tool_name} failed: {context}"}]


def _error_dict(tool_name: str, context: str) -> dict:
    return {"error": f"{tool_name} failed: {context}"}

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
# P0 tools — single-repo review history.
#
# Error contract (round 3): every P0 tool routes exceptions through
# `_error_dict`/`_error_list` so all 9 MCP tools share one payload shape.
# ValueError detail is safe to echo (our own validate_repo_slug output);
# any other exception is scrubbed to "internal error; see server logs"
# to avoid leaking tokens or local paths through the response channel.
# --------------------------------------------------------------------------


@mcp.tool
def seneschal_last_review(repo: str) -> dict | None:
    """Return a summary of the most recent Seneschal review for `repo`.

    Args:
        repo: GitHub repo slug in "owner/name" form (e.g. "ChandlerHardy/seneschal").

    Returns:
        A dict with keys: repo, pr_number, verdict, timestamp, url. Or
        None if no reviews have been persisted for that repo. On a
        malformed slug, returns `{"error": "seneschal_last_review failed: ..."}`
        with the validation detail — those strings are caller-facing.
        On any other exception, returns a scrubbed
        `{"error": "seneschal_last_review failed: internal error; see server logs"}`
        so tokens / local paths never leak through the MCP response
        channel.
    """
    try:
        rec = last_review(repo)
    except ValueError as e:
        # Caller-facing validation — echo the detail (safe: comes from our
        # own validate_repo_slug).
        return _error_dict("seneschal_last_review", str(e))
    except Exception as e:  # noqa: BLE001
        _log(f"seneschal_last_review failed for repo={repo!r}: {e}")
        return _error_dict("seneschal_last_review", "internal error; see server logs")
    return _summary_or_none(rec)


@mcp.tool
def seneschal_review_history(repo: str, limit: int = DEFAULT_LIST_LIMIT) -> list[dict]:
    """Return up to `limit` most-recent review summaries for `repo`, newest first.

    Args:
        repo: GitHub repo slug in "owner/name" form.
        limit: Max number of reviews to return. Default 50, clamped to
            `MAX_LIMIT=200`.

    Returns:
        List of summary dicts (same shape as seneschal_last_review), or
        an empty list if the repo has no persisted reviews. If `repo` is
        malformed, returns a one-element list with an error payload
        using the unified `"<tool> failed: ..."` shape.
    """
    try:
        recs = list_reviews(repo, limit=min(max(1, int(limit)), MAX_LIMIT))
    except ValueError as e:
        return _error_list("seneschal_review_history", str(e))
    except Exception as e:  # noqa: BLE001
        _log(f"seneschal_review_history failed for repo={repo!r}: {e}")
        return _error_list("seneschal_review_history", "internal error; see server logs")
    return [r.summary() for r in recs]


@mcp.tool
def seneschal_review_text(repo: str, pr_number: int) -> dict | None:
    """Return the full posted review body for (repo, pr_number).

    Args:
        repo: GitHub repo slug in "owner/name" form.
        pr_number: The PR number whose review body to return.

    Returns:
        A dict with keys: summary (metadata dict) and body (markdown).
        None if that PR has no persisted review. Error payloads use
        the unified `{"error": "<tool> failed: ..."}` shape — internal
        exceptions are scrubbed so secrets in error strings never leak.
    """
    try:
        rec = get_review(repo, int(pr_number))
    except (ValueError, TypeError) as e:
        return _error_dict("seneschal_review_text", str(e))
    except Exception as e:  # noqa: BLE001
        _log(f"seneschal_review_text failed for repo={repo!r} pr={pr_number!r}: {e}")
        return _error_dict("seneschal_review_text", "internal error; see server logs")
    if rec is None:
        return None
    # Redact the body before returning — a reviewer's code block could
    # embed a real token or the original PR diff could contain one in an
    # added line. The MCP tool is a new egress channel; snippets in
    # `search_reviews` are already scrubbed via `_redact_snippet`, and
    # we apply the same policy to the full body here for consistency.
    return {"summary": rec.summary(), "body": _redact(rec.body)}


@mcp.tool
def seneschal_repo_memory(repo: str, repo_root: str) -> dict:
    """Return the curated review-memory markdown from a repo's working tree.

    Looks for `.seneschal-memory.md` first, then the legacy
    `.ch-code-reviewer-memory.md`.

    Args:
        repo: GitHub repo slug (used to validate the caller — not used to
            find the file; `repo_root` is the actual path).
        repo_root: Absolute path to the repo's working tree.

    Returns:
        On success: `{"content": "<markdown>"}` — an empty string under
        `content` when neither memory file exists or `repo_root` doesn't
        resolve to a directory.
        On error: `{"error": "seneschal_repo_memory failed: ..."}` using
        the unified error-shape contract. Round-3 change from the
        previous `f"(error) {e}"` string return: every other tool
        returns a dict, and callers parsing across tools shouldn't have
        to special-case this one.
    """
    try:
        content = get_repo_memory(repo, repo_root)
    except ValueError as e:
        return _error_dict("seneschal_repo_memory", str(e))
    except Exception as e:  # noqa: BLE001
        _log(f"seneschal_repo_memory failed for repo={repo!r}: {e}")
        return _error_dict("seneschal_repo_memory", "internal error; see server logs")
    return {"content": content}


# --------------------------------------------------------------------------
# P2 tools — cross-repo knowledge custody.
# --------------------------------------------------------------------------


@mcp.tool
def seneschal_search_reviews(
    query: str, repo: Optional[str] = None, limit: int = DEFAULT_SEARCH_LIMIT
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
        # ValueError is caller-facing (malformed slug / limit) — pass
        # the raw message through so they can correct the input.
        return [{"error": str(e)}]
    except Exception as e:  # noqa: BLE001
        _log(f"seneschal_search_reviews failed for repo={repo!r}: {e}")
        return _error_list("seneschal_search_reviews", "internal error; see server logs")


@mcp.tool
def seneschal_search_adrs(
    query: str, repo: Optional[str] = None, limit: int = DEFAULT_SEARCH_LIMIT
) -> list[dict]:
    """Full-text search across ADRs discovered in every known repo.

    Args:
        query: Free-text phrase to search for.
        repo: Optional owner/name filter.
        limit: Max ADRs to return (default 20, max 200).

    Returns:
        List of dicts with keys: repo, path, id, title, status, snippet.
        The `snippet` key matches `search_reviews` — the previous
        `excerpt` key was renamed for consistency.
    """
    try:
        idx = _get_index()
        return idx.search_adrs(query, repo=repo, limit=int(limit))
    except ValueError as e:
        return [{"error": str(e)}]
    except Exception as e:  # noqa: BLE001
        _log(f"seneschal_search_adrs failed for repo={repo!r}: {e}")
        return _error_list("seneschal_search_adrs", "internal error; see server logs")


@mcp.tool
def seneschal_merged_prs(
    repo: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
) -> list[dict]:
    """Return merged PRs from the index, newest-first.

    Args:
        repo: Optional owner/name filter.
        since: ISO-8601 lower bound on `merged_at` (inclusive). Invalid
            strings (e.g. `"yesterday"`) return an error payload rather
            than silently producing an empty list — the index compares
            lexicographically against ISO-8601 stamps, so any other
            shape would silently skew results.
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
        _log(f"seneschal_merged_prs failed for repo={repo!r}: {e}")
        return _error_list("seneschal_merged_prs", "internal error; see server logs")


@mcp.tool
def seneschal_followups(
    repo: Optional[str] = None,
    status: str = "open",
    limit: int = DEFAULT_LIST_LIMIT,
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

        If the sweep is truncated by a GitHub rate-limit (403/429) mid-
        loop, the final element is a sentinel dict with keys
        `_truncated=True`, `_reason="rate_limit"`, and `_processed_slugs`
        (the count of slugs the sweep reached before breaking). Caller
        can inspect the last element to distinguish "only 3 repos had
        open followups" from "hit rate limit after repo 3".
    """
    # Validate inputs up-front.
    if repo is not None:
        try:
            validate_repo_slug(repo)
        except ValueError as e:
            return [{"error": str(e)}]
    if status not in ("open", "closed", "all"):
        return [{"error": f"invalid status: {status!r} (want open/closed/all)"}]
    limit = max(1, min(int(limit), MAX_LIMIT))

    if repo is not None:
        slugs = [repo]
    else:
        try:
            slugs = [kr.slug for kr in cross_repo.known_repos()]
        except Exception as e:  # noqa: BLE001
            _log(f"seneschal_followups: known_repos failed: {e}")
            return _error_list("seneschal_followups", "repo enumeration failed; see server logs")

    out: list[dict] = []
    processed_slugs = 0
    truncated_by_rate_limit = False
    for slug in slugs:
        if len(out) >= limit:
            break
        processed_slugs += 1
        # Mint a token per-slug. `AppNotInstalledError` → skip quietly.
        try:
            token = seneschal_token.mint_installation_token(slug)
        except seneschal_token.AppNotInstalledError:
            _log(f"App not installed on {slug}; skipping")
            continue
        except seneschal_token.TokenMintError as e:
            # A single-repo request that can't mint — surface as error.
            if repo is not None:
                _log(f"seneschal_followups: token mint failed for {slug}: {e}")
                return _error_list("seneschal_followups", f"token mint failed for {slug}")
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
            # Rate-limit cascade guard: if GitHub tells us we're throttled
            # or forbidden, every subsequent repo in this sweep will hit
            # the same wall. Break the loop so we don't burn the rest of
            # the tokens (and the caller's stdio buffer) on predictable
            # failures. 5 extra lines, stops a real bug observed in the
            # followup-heavy mornings.
            #
            # Round-3: flag the truncation so the caller can distinguish
            # "3 repos had followups" from "hit rate limit after repo 3".
            if resp.status_code in (403, 429):
                _log(
                    f"seneschal_followups: GitHub returned {resp.status_code} for {slug}; "
                    f"breaking loop to avoid cascading rate-limit failures"
                )
                truncated_by_rate_limit = True
                break
            resp.raise_for_status()
            issues = resp.json()
        except (requests.HTTPError, requests.RequestException) as e:
            _log(f"issues fetch failed for {slug}: {e}")
            if repo is not None:
                return _error_list("seneschal_followups", f"issues fetch failed for {slug}")
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
    # Round-3: if the sweep broke early on a rate-limit, append a sentinel
    # as the FINAL element so the caller can detect truncation without
    # having to inspect server logs. The `_`-prefixed keys mark it as
    # metadata rather than a real followup row — a caller that doesn't
    # know about the sentinel shape just sees an extra "empty" item.
    if truncated_by_rate_limit:
        out.append(
            {
                "_truncated": True,
                "_reason": "rate_limit",
                "_processed_slugs": processed_slugs,
            }
        )
    return out


@mcp.tool
def seneschal_dependency_usage(
    package_name: str, limit: int = DEFAULT_LIST_LIMIT
) -> list[dict]:
    """Grep every known repo's manifests for a package reference.

    Answers "if this package has a CVE, which repos do I need to patch?"
    Substring match across package.json, requirements.txt, pyproject.toml,
    go.mod, Package.swift, Cargo.toml. Because it's a raw substring
    match (no ecosystem-specific parser), short or common names can
    produce false positives — `scan_all("requests")` also matches
    `test-requests`, `requests-mock`, etc. Treat results as a
    "manifests to eyeball" list, not a proof of usage.

    Args:
        package_name: Package name / substring to match.
        limit: Max total hits to return (default 50).

    Returns:
        List of dicts with keys: repo, path, line, matched.
    """
    try:
        hits = dependency_grep.scan_all(package_name, limit=int(limit))
    except Exception as e:  # noqa: BLE001
        _log(f"seneschal_dependency_usage failed for package={package_name!r}: {e}")
        return _error_list("seneschal_dependency_usage", "internal error; see server logs")
    return [
        {"repo": h.repo, "path": h.path, "line": h.line, "matched": h.matched}
        for h in hits
    ]


def main():  # pragma: no cover
    """Run the MCP server over stdio (standard MCP transport)."""
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
