"""Seneschal — GitHub REST + App-auth helpers.

Extracted from `app.py` so GitHub-I/O lives in one module and the webhook
handler stays focused on request routing + process/thread state. All
GitHub-facing helpers (PR metadata, diff, files, reviews, comments,
labels, contents-API writes, branch creation, issue creation) land here.

Keep in sync with:
  - `app.py` (webhook handler) — imports these helpers for the auto-review
    path and for `_queue_post_merge`'s token mint.
  - `post_merge/orchestrator.py` — imports GitHub helpers directly from
    this module; still talks to `app.py` for `log`, `ensure_repo_synced`,
    and the `_per_pr_lock` primitive (those depend on process state).
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import List, Optional

import jwt
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from related_prs import OtherPR
from risk import PRFile


# Config
# `APP_ID` used to be duplicated here + in `seneschal_token` via a
# bi-directional deferred import. Round-3 consolidation: the source of
# truth lives in `seneschal_token.APP_ID`. Every consumer that needs the
# value imports it from there. This module still re-exports the name
# for backward-compat with `app.py`'s existing `from github_api import
# APP_ID` until the next wave can churn the import site.
from seneschal_token import APP_ID  # noqa: E402 — re-export for app.py compat
INSTALL_DIR = os.path.expanduser("~/seneschal")
PEM_PATH = os.path.join(INSTALL_DIR, "ch-code-reviewer.pem")


def _github_session():
    """Create a requests Session with retry/backoff for transient GitHub errors."""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def generate_jwt():
    """Sign a JWT for the Seneschal GitHub App.

    Delegates to `seneschal_token._generate_jwt` so the App ID, PEM
    path, env overrides (SENESCHAL_APP_ID, SENESCHAL_PEM_PATH), and
    expiry window live in one place. The old inline impl used a
    600s exp window; the shared impl uses 540s to leave a larger
    safety margin against GitHub's 10-minute hard cap for in-flight
    requests that hold a reference to the token.
    """
    from seneschal_token import _generate_jwt
    return _generate_jwt()


def get_installation_token(installation_id):
    token = generate_jwt()
    session = _github_session()
    resp = session.post(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    resp.raise_for_status()
    return resp.json()["token"]


def get_pr_diff(owner, repo, pr_number, token):
    session = _github_session()
    resp = session.get(
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3.diff",
        },
    )
    resp.raise_for_status()
    return resp.text


def get_pr_meta(owner, repo, pr_number, token):
    """Fetch PR metadata (title, body, head, etc.)."""
    session = _github_session()
    resp = session.get(
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    resp.raise_for_status()
    return resp.json()


def get_pr_files(owner, repo, pr_number, token):
    """Fetch the list of files in a PR. Returns List[PRFile]."""
    session = _github_session()
    files = []
    page = 1
    while True:
        resp = session.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
            },
            params={"per_page": 100, "page": page},
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        for f in batch:
            files.append(PRFile(
                filename=f["filename"],
                additions=f.get("additions", 0),
                deletions=f.get("deletions", 0),
                status=f.get("status", "modified"),
                # Only present when status == "renamed"; blank otherwise.
                previous_filename=f.get("previous_filename") or "",
            ))
        if len(batch) < 100:
            break
        page += 1
    return files


def get_other_open_prs(owner, repo, exclude_pr, token, max_prs=200):
    """Fetch other open PRs and their files. Returns List[OtherPR].

    Paginates the pulls endpoint instead of the previous one-shot
    `per_page=50` call — on a repo with >50 open PRs the tail was
    silently dropped, causing overlap detection to miss related work.
    """
    # Deferred import so this module doesn't depend on the log helper that
    # lives in app.py (which would create a circular import).
    from app import log

    session = _github_session()
    others = []
    page = 1
    fetched = 0
    while fetched < max_prs:
        resp = session.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
            },
            params={"state": "open", "per_page": 100, "page": page},
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        for pr in batch:
            if pr["number"] == exclude_pr:
                continue
            try:
                files = get_pr_files(owner, repo, pr["number"], token)
                others.append(OtherPR(
                    number=pr["number"],
                    title=pr["title"],
                    files=frozenset(f.filename for f in files),
                ))
            except Exception as e:  # noqa: BLE001
                log(f"Failed to fetch files for #{pr['number']}: {e}")
            fetched += 1
            if fetched >= max_prs:
                break
        if len(batch) < 100:
            break
        page += 1
    return others


class PushProtectedError(Exception):
    """Raised when GitHub returns 403 on a Contents-API write to a protected ref.

    The post-merge orchestrator catches this to switch from direct-commit
    mode to auto-PR mode (open a branch + PR instead of pushing to main).
    """


def create_issue(owner, repo, title, body, labels, token):
    """POST /repos/{owner}/{repo}/issues. Returns the issue dict."""
    session = _github_session()
    payload = {"title": str(title), "body": str(body or "")}
    if labels:
        payload["labels"] = list(labels)
    resp = session.post(
        f"https://api.github.com/repos/{owner}/{repo}/issues",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        },
        json=payload,
    )
    resp.raise_for_status()
    return resp.json()


def create_pull_request(owner, repo, title, body, head, base, token, draft=True):
    """POST /repos/{owner}/{repo}/pulls. Returns the PR dict."""
    session = _github_session()
    resp = session.post(
        f"https://api.github.com/repos/{owner}/{repo}/pulls",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        },
        json={
            "title": str(title),
            "body": str(body or ""),
            "head": str(head),
            "base": str(base),
            "draft": bool(draft),
        },
    )
    resp.raise_for_status()
    return resp.json()


def create_branch(owner, repo, new_ref, from_sha, token):
    """POST /repos/{owner}/{repo}/git/refs. Idempotent: existing ref returns its current state."""
    session = _github_session()
    full_ref = new_ref if new_ref.startswith("refs/") else f"refs/heads/{new_ref}"
    resp = session.post(
        f"https://api.github.com/repos/{owner}/{repo}/git/refs",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        },
        json={"ref": full_ref, "sha": str(from_sha)},
    )
    if resp.status_code == 422:
        # "Reference already exists" — fetch it and return.
        ref_only = full_ref[len("refs/"):] if full_ref.startswith("refs/") else full_ref
        existing = session.get(
            f"https://api.github.com/repos/{owner}/{repo}/git/ref/{ref_only}",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
            },
        )
        existing.raise_for_status()
        return existing.json()
    resp.raise_for_status()
    return resp.json()


def get_file_sha(owner, repo, path, branch, token):
    """GET /repos/{owner}/{repo}/contents/{path} — return the file's blob SHA, or None if missing."""
    session = _github_session()
    resp = session.get(
        f"https://api.github.com/repos/{owner}/{repo}/contents/{path}",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        },
        params={"ref": branch},
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        return data.get("sha")
    return None


def get_file_content(owner, repo, path, branch, token):
    """GET /repos/{owner}/{repo}/contents/{path} — return (content_str, sha) or (None, None).

    Used by the release-PR amend path to re-fetch the canonical
    CHANGELOG from the release branch right before writing back, so a
    just-merged changelog commit (pushed by a concurrent post-merge
    worker) isn't overwritten with a stale snapshot from the caller's
    local clone.
    """
    import base64

    session = _github_session()
    resp = session.get(
        f"https://api.github.com/repos/{owner}/{repo}/contents/{path}",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        },
        params={"ref": branch},
    )
    if resp.status_code == 404:
        return (None, None)
    resp.raise_for_status()
    data = resp.json() or {}
    if not isinstance(data, dict):
        return (None, None)
    sha = data.get("sha")
    encoded = data.get("content") or ""
    if data.get("encoding") != "base64":
        # GitHub has returned JSON listings for directories — don't try
        # to decode those as file content.
        return (None, sha)
    try:
        content = base64.b64decode(encoded).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return (None, sha)
    return (content, sha)


def get_default_branch_sha(owner, repo, branch, token):
    """GET /repos/{owner}/{repo}/git/ref/heads/{branch} — return the head SHA."""
    session = _github_session()
    resp = session.get(
        f"https://api.github.com/repos/{owner}/{repo}/git/ref/heads/{branch}",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    resp.raise_for_status()
    data = resp.json() or {}
    obj = data.get("object") or {}
    return obj.get("sha", "")


def put_file(owner, repo, path, content, message, branch, sha, token):
    """PUT /repos/{owner}/{repo}/contents/{path} — write a file via the Contents API.

    On 409 (sha mismatch from a concurrent write) re-fetches the file's
    current SHA and retries up to 3 times. On 403 raises PushProtectedError
    so the orchestrator can switch to auto-PR mode.
    """
    import base64

    session = _github_session()
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")

    for attempt in range(3):
        payload = {
            "message": str(message),
            "content": encoded,
            "branch": str(branch),
        }
        if sha:
            payload["sha"] = str(sha)
        resp = session.put(
            f"https://api.github.com/repos/{owner}/{repo}/contents/{path}",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
            },
            json=payload,
        )
        if resp.status_code == 403:
            raise PushProtectedError(
                f"PUT {path} on {owner}/{repo}@{branch} returned 403 — likely branch protection"
            )
        if resp.status_code == 409:
            # Concurrent write. Re-fetch the sha and retry unless we've
            # exhausted the retry budget — if we have, raise RuntimeError
            # with the "sha conflict after 3 retries" sentinel so the
            # orchestrator's `_attempt_changelog_commit` classifies this
            # as `"conflict"` and dead-letters the entry. Previously this
            # fell through to `resp.raise_for_status()` and surfaced an
            # HTTPError, which the orchestrator misclassified as `"error"`
            # and silently dropped the changelog entry on the floor.
            if attempt < 2:
                sha = get_file_sha(owner, repo, path, branch, token)
                continue
            raise RuntimeError(
                f"put_file: sha conflict after 3 retries on {owner}/{repo}/{path}"
            )
        resp.raise_for_status()
        return resp.json()
    # Defensive: loop only exits via return/raise above. If we somehow fall
    # through, surface a clear error rather than a silent None.
    raise RuntimeError(f"put_file: gave up after 3 retries on {owner}/{repo}/{path}")


def find_open_prs_with_label(owner, repo, label, token):
    """GET /repos/{owner}/{repo}/issues?labels=<label>&state=open — return PR dicts only."""
    session = _github_session()
    resp = session.get(
        f"https://api.github.com/repos/{owner}/{repo}/issues",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        },
        params={"labels": label, "state": "open", "per_page": 100},
    )
    resp.raise_for_status()
    out = []
    for item in resp.json() or []:
        if not isinstance(item, dict):
            continue
        if "pull_request" in item:
            # Hydrate the PR object so callers get head.ref.
            pr_resp = session.get(
                f"https://api.github.com/repos/{owner}/{repo}/pulls/{item['number']}",
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github+json",
                },
            )
            if pr_resp.status_code == 200:
                out.append(pr_resp.json())
    return out


_GET_PR_COMMITS_PAGE_CAP = 10  # 10 pages × 100/page = 1000-commit ceiling


def get_pr_commits(owner, repo, pr_number, token):
    """GET /repos/{owner}/{repo}/pulls/{pr_number}/commits — list of commit dicts.

    Paginates the standard GitHub way: follow `page=N` until the endpoint
    returns a short page (< 100 items) or an empty page, then stop.
    Previously a hard-coded `per_page=100` one-shot silently returned only
    the first 100 commits — PRs with 101+ commits missed BREAKING CHANGE
    signals in any commit past #100, which meant the release bump kind
    was computed against partial data.

    Capped at `_GET_PR_COMMITS_PAGE_CAP` pages (1000 commits) to bound the
    worst case. PRs with more than 1000 commits are vanishingly rare and
    usually not legitimate. On cap hit we log a warning and return what
    we have — better to ship a bounded scan than unbounded I/O.
    """
    # Deferred import to avoid a circular dependency with app.py (which
    # imports from github_api).
    from app import log

    session = _github_session()
    out: List[dict] = []
    for page in range(1, _GET_PR_COMMITS_PAGE_CAP + 1):
        resp = session.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/commits",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
            },
            params={"per_page": 100, "page": page},
        )
        resp.raise_for_status()
        batch = resp.json() or []
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
    else:
        # Loop exited via range exhaustion — we fetched the cap + the last
        # page was full, so there's very likely more. Log truncation so
        # operators see it in the logs rather than silently mis-detect
        # breaking changes on the unseen tail.
        log(
            f"get_pr_commits: truncated at {_GET_PR_COMMITS_PAGE_CAP} pages "
            f"({_GET_PR_COMMITS_PAGE_CAP * 100} commits) for "
            f"{owner}/{repo}#{pr_number}; breaking-change scan may miss "
            f"signals past this point"
        )
    return out


def apply_labels(owner, repo, pr_number, labels, token):
    """Add labels to a PR (additive, not replace)."""
    if not labels:
        return
    # Deferred import so this module doesn't depend on the log helper that
    # lives in app.py (which would create a circular import).
    from app import log

    session = _github_session()
    try:
        session.post(
            f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/labels",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
            },
            json={"labels": list(labels)},
        )
    except Exception as e:  # noqa: BLE001
        log(f"Failed to apply labels {labels}: {e}")


def parse_verdict(review_text):
    """Determine the GitHub review event from the review body text.

    Recognizes both formats Seneschal produces:

    1. Single-pass review (analyzer.py + one Claude call) — looks for the
       "NEEDS CHANGES" / "NEEDS_CHANGES" sentinel from that prompt's
       verdict rule, defaulting to APPROVE when absent.
    2. Full multi-persona review (full_review.py + slash command) —
       looks for the explicit ``**Verdict:** REQUEST_CHANGES|COMMENT|APPROVE``
       line that the /seneschal-review command writes near the top.

    The full-review path's COMMENT verdict is preserved so the bot can
    leave non-blocking feedback (warnings + minor) without auto-approving
    or hard-blocking the PR.

    Relocated from `app.py` in round 5 W3: the only caller was
    `post_review` here, which used to do a deferred `from app import
    parse_verdict` to reach it. Colocating the function with its sole
    caller removes the cross-module deferred import and shrinks
    `app.py`'s public surface.
    """
    first_lines = review_text[:1000].upper()

    # Seneschal full-review explicit verdict line (most specific, check first).
    if "**VERDICT:** REQUEST_CHANGES" in first_lines or "**VERDICT:** REQUEST CHANGES" in first_lines:
        return "REQUEST_CHANGES"
    if "**VERDICT:** APPROVE" in first_lines:
        return "APPROVE"
    if "**VERDICT:** COMMENT" in first_lines:
        return "COMMENT"

    # Single-pass legacy format.
    if "NEEDS CHANGES" in first_lines or "NEEDS_CHANGES" in first_lines:
        return "REQUEST_CHANGES"
    return "APPROVE"


def post_review(owner, repo, pr_number, body, token, inline_comments=None, *, head_sha=""):
    """Post a formal PR review (APPROVE or REQUEST_CHANGES).

    If inline_comments is provided, posts them as per-line review comments
    alongside the review body. Each comment should be a dict with keys:
    path, line, side, body.

    On success, persists the posted review to the on-disk review store so
    the MCP server can expose it to local Claude Code sessions later.
    Persistence failures are non-fatal (the review is already on GitHub).

    W6: `head_sha` is optional (keyword-only, default "") so existing
    call sites stay compatible, but when supplied it's written into the
    review-store frontmatter so P2's SQLite index carries the actual
    PR head SHA instead of a blank column.
    """
    # Deferred imports — `app.py` imports from `github_api`, so a top-level
    # `from app import ...` here would be circular. `log` + `_per_pr_lock`
    # still live in app.py; reach them lazily. `parse_verdict` now lives
    # in this module (round 5 W3).
    from app import log, _per_pr_lock
    from review_store import save_review

    verdict = parse_verdict(body)
    session = _github_session()
    payload = {"body": body, "event": verdict}
    if inline_comments:
        payload["comments"] = inline_comments
    resp = session.post(
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        },
        json=payload,
    )
    if resp.status_code >= 400 and inline_comments:
        # Inline comments can fail if line numbers don't map to the diff
        # (e.g. the file was later modified). Retry without them rather
        # than failing the whole review.
        log(f"Review with inline comments failed ({resp.status_code}): {resp.text[:200]}")
        log("Retrying without inline comments...")
        resp = session.post(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
            },
            json={"body": body, "event": verdict},
        )
    resp.raise_for_status()
    comment_suffix = f" with {len(inline_comments)} inline comment(s)" if inline_comments else ""
    log(f"Posted {verdict} review on {owner}/{repo}#{pr_number}{comment_suffix}")

    # Persist to the review store so the MCP server can surface this later.
    # W7: wrap the save_review call in the same per-PR lock the post-merge
    # orchestrator uses. Without it, a push-event review landing while a
    # merge event's mark_merged is in flight overwrites the file with a
    # fresh record (no merged_at, no followups_filed_titles) — losing the
    # post-merge state that just got persisted.
    try:
        review_json = resp.json() if resp.content else {}
        review_url = str(review_json.get("html_url", "")) if isinstance(review_json, dict) else ""
        with _per_pr_lock(owner, repo, pr_number):
            save_review(
                f"{owner}/{repo}",
                int(pr_number),
                verdict,
                review_url,
                body,
                head_sha=head_sha or "",
            )
    except Exception as e:  # noqa: BLE001
        log(f"Review store persist failed (non-fatal): {e}")

    return verdict


def post_comment(owner, repo, pr_number, body, token):
    """Post a regular issue comment (for fix attempt tracking)."""
    session = _github_session()
    session.post(
        f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        },
        json={"body": body},
    )


def react_to_comment(owner, repo, comment_id, token, content="eyes"):
    """React to an issue/PR comment so the user sees the trigger was
    received before the full review runs (~30 seconds). Failure is
    non-fatal — the review continues regardless.

    Args:
        comment_id: GitHub's numeric id for the triggering comment.
        content: one of GitHub's reaction contents. Default "eyes" (👀)
            is the standard "I saw this, working on it" signal.
    """
    # Deferred import to keep github_api.py free of direct app dependencies.
    from app import log

    try:
        session = _github_session()
        resp = session.post(
            f"https://api.github.com/repos/{owner}/{repo}/issues/comments/{comment_id}/reactions",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
            },
            json={"content": content},
            timeout=5,
        )
        if resp.status_code >= 400:
            log(f"React to comment {comment_id} failed ({resp.status_code}): {resp.text[:200]}")
    except Exception as e:  # noqa: BLE001
        log(f"React to comment {comment_id} raised (non-fatal): {e}")
