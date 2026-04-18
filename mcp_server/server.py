"""Seneschal MCP server — read-only history query tools.

Exposes four tools to local Claude Code sessions via the Model Context
Protocol (stdio transport):

    seneschal_last_review(repo)             → summary of most recent review
    seneschal_review_history(repo, limit)   → list of recent review summaries
    seneschal_review_text(repo, pr_number)  → full body of a specific review
    seneschal_repo_memory(repo, repo_root)  → contents of the repo's
                                              .seneschal-memory.md file

All tools are read-only. The server does NOT call the GitHub API or
spawn `claude -p` — it only reads files persisted by the webhook
handler at ~/.seneschal/reviews/.

Run with:
    python -m mcp_server.server
"""

from __future__ import annotations

import os
import sys

# Ensure the parent dir is on sys.path so `import review_store` works
# when this module is run as `python -m mcp_server.server`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from review_store import (  # noqa: E402
    get_repo_memory,
    get_review,
    last_review,
    list_reviews,
)

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
        "Seneschal review history and repo memory. Use these tools to look up "
        "what previous Seneschal reviews said about a PR, summarize a repo's "
        "recent review activity, or inspect the curated review-memory rules "
        "stored in a repo's .seneschal-memory.md."
    ),
)


def _summary_or_none(rec):
    """Convert a ReviewRecord to a summary dict, or return None if missing."""
    return rec.summary() if rec is not None else None


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
    return {"summary": rec.summary(), "body": rec.body}


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


def main():  # pragma: no cover
    """Run the MCP server over stdio (standard MCP transport)."""
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
