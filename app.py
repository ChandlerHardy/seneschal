#!/usr/bin/env python3
"""Seneschal — GitHub App webhook handler for automated PR reviews.

The bot's public identity is "Seneschal CR" (slug `seneschal-cr` on
GitHub). On disk the install dir is `~/seneschal/`, the systemd unit
is `seneschal.service`, and log lines are prefixed `[seneschal]`. The
GitHub App slug is `ch-code-reviewer` (locked at App-creation time)
so the .pem filename still mentions it — that's the only legacy string.

The seneschal of a medieval household ran the estate: scheduled the
staff, settled disputes, and kept everything moving while the lord was
busy with other things. That's the role: discover work via heartbeat,
file issues, review PRs, keep the operator unblocked.
"""

import errno
import fcntl
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Tuple

import jwt
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, request, jsonify

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from analyzer import analyze_pr  # noqa: E402
from backend import get_backend  # noqa: E402
from ci_context import fetch_ci_results  # noqa: E402
from full_review import run_full_review  # noqa: E402
from history_context import find_adrs  # noqa: E402
from persona_loader import load_personas  # noqa: E402
from review_store import save_review  # noqa: E402
from related_prs import OtherPR  # noqa: E402
from repo_config import load_from_repo  # noqa: E402
from review_memory import load as load_memory  # noqa: E402
from risk import PRFile  # noqa: E402

app = Flask(__name__)

# Config
APP_ID = 3127694
INSTALL_DIR = os.path.expanduser("~/seneschal")
PEM_PATH = os.path.join(INSTALL_DIR, "ch-code-reviewer.pem")
WEBHOOK_SECRET_PATH = os.path.join(INSTALL_DIR, "webhook-secret.txt")
REPOS_DIR = "/mnt/block_volume/repos"

# Branch filter regex. The pre-Stage-1 default `^heartbeat/` was removed so
# any PR on an installed repo is reviewed; the GitHub App's installation
# list is now the only allowlist. Keep this as an env override so the
# operator can re-narrow it without a code change.
BRANCH_FILTER = os.environ.get("CODE_REVIEWER_BRANCH_FILTER", "")

# Global default for the heavyweight multi-persona path. Per-repo overrides
# come from `.ch-code-reviewer.yml` (full_review: true). Either source flips
# the bot from the diff-static-analyzer + single-pass review path to the
# parallel persona orchestrator in full_review.py.
FULL_REVIEW_DEFAULT = os.environ.get("CODE_REVIEWER_FULL_DEFAULT", "").lower() in {"1", "true", "yes"}

# Auto-review kill switch. Defaults to DISABLED so a fresh deploy of the
# webhook handler cannot start reviewing PRs without an explicit opt-in —
# and so an environment where the systemd unit was set up before this flag
# existed fails closed. Flip to 1/true/yes in the systemd unit Environment=
# line to re-enable webhook-driven auto-review on pull_request events.
#
# The `/seneschal review` PR-comment trigger below is unaffected: it is
# always-on because typing the command IS the explicit ask. The gate only
# suppresses automatic fire on PR open/push.
AUTOREVIEW_ENABLED = os.environ.get("SENESCHAL_AUTOREVIEW", "").lower() in {"1", "true", "yes"}

# PR-comment trigger authors. A comment matching COMMENT_TRIGGER_RE from an
# author in this set starts a review on the PR that carries the comment.
# Keep tight: while AUTOREVIEW_ENABLED is off, the comment trigger is the
# ONLY way to kick off a webhook-driven review, so anyone on this list
# effectively holds the bot's review-on-demand keys.
# Configured via SENESCHAL_TRIGGER_AUTHORS env var (comma-separated GitHub
# usernames). Empty means no one can trigger reviews.
COMMENT_TRIGGER_AUTHORS = frozenset(
    u.strip() for u in os.environ.get("SENESCHAL_TRIGGER_AUTHORS", "").split(",") if u.strip()
)

# Exact-line match for the trigger command, anchored with re.MULTILINE so
# (a) a multi-line comment quoting someone else's instructions ("do this:
# /seneschal review ...") cannot accidentally fire, and (b) a trailing
# newline still matches. Uses `\s+` between the verbs so `/seneschal  review`
# still works, but rejects `/seneschalreview` as a false positive.
COMMENT_TRIGGER_RE = re.compile(r"^/seneschal\s+review\s*$", re.MULTILINE)


def is_review_trigger_comment(body: str) -> bool:
    """Return True if the comment body contains the on-its-own-line trigger."""
    if not body:
        return False
    return bool(COMMENT_TRIGGER_RE.search(body))

# Repos cloned with a different local directory name than their GitHub
# repo name. New repos do not need an entry — auto-clone uses the GitHub
# name unchanged.
REPO_NAME_MAP = {
    "gnomestead": "gnomestead-ios",
}

LOG_PREFIX = "[seneschal]"


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


def log(msg):
    print(f"{LOG_PREFIX} {msg}", flush=True)


# GitHub installation tokens start with `ghs_` (modern) or the older
# `v1.` form; both appear in the `x-access-token:<token>@github.com` URL
# pattern that git echoes back in error messages. Redact the whole
# authority span so neither the token nor the proxy user survives.
_INSTALLATION_URL_PATTERN = re.compile(
    r"https://[^@\s]+@github\.com", re.IGNORECASE
)


def _scrub_installation_token(text: str) -> str:
    """Redact GitHub App installation tokens from log-bound strings."""
    return _INSTALLATION_URL_PATTERN.sub("https://<redacted>@github.com", text)


def get_webhook_secret():
    try:
        return Path(WEBHOOK_SECRET_PATH).read_text().strip()
    except FileNotFoundError:
        return None


def verify_signature(payload, signature):
    secret = get_webhook_secret()
    if not secret:
        # Fail closed: the endpoint is reachable from nginx, so a missing
        # secret means we cannot trust the caller and must reject every request.
        log("ERROR: webhook secret missing or empty — rejecting request")
        return False
    if not signature:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def generate_jwt():
    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + (10 * 60),
        "iss": str(APP_ID),
    }
    pem = Path(PEM_PATH).read_text()
    return jwt.encode(payload, pem, algorithm="RS256")


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


def get_pr_commits(owner, repo, pr_number, token):
    """GET /repos/{owner}/{repo}/pulls/{pr_number}/commits — list of commit dicts."""
    session = _github_session()
    resp = session.get(
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/commits",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        },
        params={"per_page": 100},
    )
    resp.raise_for_status()
    return resp.json() or []


def apply_labels(owner, repo, pr_number, labels, token):
    """Add labels to a PR (additive, not replace)."""
    if not labels:
        return
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


def _local_repo_path(repo: str) -> str:
    return os.path.join(REPOS_DIR, REPO_NAME_MAP.get(repo, repo))


_PER_PR_LOCK_DIR = os.path.expanduser("~/.seneschal/locks")

# In-process per-PR locks, keyed by (owner, repo, pr_number). Needed
# ABOVE the fcntl lock because Linux `flock(2)` is per-open-file-
# description: two threads in this same Python process each `os.open`
# the lockfile and each `LOCK_EX` succeeds independently, breaking the
# concurrency promise. Threading.Lock serializes same-process threads;
# fcntl serializes cross-process handlers.
# Key type is `(owner, repo, int_pr_or_sentinel)` — W4 fix allows the
# PR slot to be the `"__invalid__"` sentinel string when coercion fails.
_PER_PR_THREAD_LOCKS: Dict[Tuple[str, str, object], threading.Lock] = {}
_PER_PR_THREAD_LOCKS_GUARD = threading.Lock()


def _get_thread_lock(owner: str, repo: str, pr_number: int) -> threading.Lock:
    """Return the shared threading.Lock for (owner, repo, pr_number).

    W4: previously `int(pr_number) if isinstance(pr_number, int) else 0`
    — the else branch collapsed any non-int (including the string
    `"42"`) to 0, cross-serializing unrelated PRs passed as strings.
    Match the defensive pattern used by `_per_pr_lock`: `int(pr_number)`
    unconditionally, letting a genuine TypeError/ValueError bubble up
    cleanly on truly-invalid input.
    """
    try:
        pr_key = int(pr_number)
    except (TypeError, ValueError):
        # Preserve the previous behavior of not raising from inside the
        # lock-lookup path — but key on a sentinel dict that still
        # distinguishes "invalid" PRs from "pr #0". Use a string tag so
        # it can't collide with any real int key.
        pr_key = "__invalid__"
    key = (str(owner), str(repo), pr_key)
    with _PER_PR_THREAD_LOCKS_GUARD:
        lock = _PER_PR_THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PER_PR_THREAD_LOCKS[key] = lock
    return lock


@contextmanager
def _per_pr_lock(owner: str, repo: str, pr_number: int):
    """Serialize post-merge work for a single PR across webhook retries.

    GitHub retries webhook delivery on 5xx. Without this lock, two
    background threads for the same merge event could both read
    `followups_filed_titles=[]` and both file the same followup issue
    before either writes the updated review record back to disk.

    Two-layer lock:
      1. `threading.Lock` — serializes threads in the same process.
         Needed because Linux `flock(2)` is per-open-file-description;
         two threads each `os.open` the file and each `LOCK_EX` succeeds.
      2. `fcntl.flock` — serializes across processes (webhook handler
         restarts, side-by-side deployments). The kernel drops the lock
         on fd close, so crash-safety is free.

    File naming is safe because `owner`/`repo` come from the GitHub
    webhook payload (controlled) and `pr_number` is an integer — we
    still pass the components through a conservative regex to catch
    any hypothetical injection before it reaches the filesystem.
    """
    safe_owner = re.sub(r"[^A-Za-z0-9_.\-]", "_", str(owner))[:100]
    safe_repo = re.sub(r"[^A-Za-z0-9_.\-]", "_", str(repo))[:100]
    try:
        safe_pr = int(pr_number)
    except (TypeError, ValueError):
        safe_pr = 0
    os.makedirs(_PER_PR_LOCK_DIR, exist_ok=True)
    # W3: previously joined components with `_` — owner `a_b` + repo `c`
    # collides with owner `a` + repo `b_c` on the same PR number. Use
    # `+` as a separator: GitHub disallows it in both owner and repo
    # names (letters, digits, `-`, `_`, `.` only), so it can't appear in
    # the sanitized components and is collision-free by construction.
    lock_path = os.path.join(
        _PER_PR_LOCK_DIR, f"{safe_owner}+{safe_repo}+{safe_pr}.lock",
    )
    thread_lock = _get_thread_lock(owner, repo, safe_pr)
    thread_lock.acquire()
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError as e:
                if e.errno != errno.EBADF:
                    raise
    finally:
        thread_lock.release()


@contextmanager
def _repo_sync_lock(repo_path: str):
    """Serialize git fetch+checkout against a shared worktree.

    `review_pr` runs on a background thread and two concurrent webhook
    deliveries for the same repo (different PRs or rapid pushes) would
    otherwise race on `git fetch` / `git checkout --detach <head_sha>`.
    Second checkout clobbers the first before the analyzer + Claude
    review finish reading the tree, so findings and inline comments line
    up with the wrong SHA.

    Use an fcntl advisory lock on a sidecar file so the serialization
    survives a webhook-handler crash (kernel drops the lock on fd close)
    and also works if the bot is ever split into multiple processes.
    The lockfile lives at `<repo_path>.lock` so it is NEVER inside the
    worktree — `git clean -fd` would otherwise remove it.
    """
    os.makedirs(REPOS_DIR, exist_ok=True)
    lock_path = repo_path.rstrip("/") + ".lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError as e:
            if e.errno != errno.EBADF:
                raise


def ensure_repo_synced(owner: str, repo: str, head_ref: str, head_sha: str, token: str) -> str:
    """Make sure the cloned repo exists and the PR's head SHA is checked out.

    Auto-clones any installed-but-not-yet-cloned repo on first webhook,
    then fetches+checks-out the exact commit being reviewed so downstream
    tools (jcodemunch, blast radius, /review-pr) see the right tree.

    Returns the local path. Failures are logged but non-fatal — the caller
    can still run the diff-text-only path against an empty directory.
    """
    repo_path = _local_repo_path(repo)
    clone_url = f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"

    with _repo_sync_lock(repo_path):
        try:
            if not os.path.isdir(os.path.join(repo_path, ".git")):
                os.makedirs(REPOS_DIR, exist_ok=True)
                log(f"Cloning {owner}/{repo} -> {repo_path}")
                subprocess.run(
                    ["git", "clone", "--quiet", clone_url, repo_path],
                    check=True,
                    timeout=300,
                    capture_output=True,
                )
            else:
                # Refresh the remote URL so a rotated installation token still
                # works (the embedded token expires hourly).
                subprocess.run(
                    ["git", "-C", repo_path, "remote", "set-url", "origin", clone_url],
                    check=True,
                    timeout=10,
                    capture_output=True,
                )

            # Fetch the head ref and check out the SHA the webhook fired on.
            # We deliberately fetch by ref (not --all) so a noisy repo with
            # many stale branches doesn't dominate sync time.
            subprocess.run(
                ["git", "-C", repo_path, "fetch", "--quiet", "origin", head_ref],
                check=True,
                timeout=120,
                capture_output=True,
            )
            if head_sha:
                subprocess.run(
                    ["git", "-C", repo_path, "checkout", "--quiet", "--detach", head_sha],
                    check=True,
                    timeout=30,
                    capture_output=True,
                )
        except subprocess.CalledProcessError as e:
            # Git error output routinely echoes the full remote URL (which
            # contains the 1-hour installation token). Scrub before logging
            # so journalctl readers can't lift the token out of a transient
            # fetch failure.
            stderr = (e.stderr or b"").decode("utf-8", errors="replace")[:300]
            stderr = _scrub_installation_token(stderr)
            log(f"git sync failed for {owner}/{repo}: {type(e).__name__} :: {stderr}")
        except subprocess.TimeoutExpired as e:
            log(f"git sync timed out for {owner}/{repo}: {type(e).__name__}")
        finally:
            # Scrub the token out of the remote URL so it isn't sitting on disk
            # for the next caller to inadvertently leak. Runs even on fetch/
            # checkout failure so a crash mid-sync can't leave the clone_url
            # (with token) persisted in .git/config.
            try:
                subprocess.run(
                    ["git", "-C", repo_path, "remote", "set-url", "origin",
                     f"https://github.com/{owner}/{repo}.git"],
                    check=False,
                    timeout=10,
                    capture_output=True,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                pass

    return repo_path


def review_pr(owner, repo, pr_number, installation_id, head_ref, head_sha):
    """Run full review pipeline on a PR. Runs in background thread.

    Pipeline:
        1. Fetch PR metadata, files, diff, and other open PRs
        2. Sync the local clone to the PR's head SHA (auto-clones first time)
        3. Load per-repo config (.ch-code-reviewer.yml)
        4. Run analyzer: risk, scope, test gaps, related PRs, blast radius
        5. Post pre-review analysis comment + apply labels
        6. Invoke the LLM backend with analyzer output as additional context
        7. Post formal review (APPROVE or REQUEST_CHANGES)
    """
    try:
        log(f"Reviewing {owner}/{repo}#{pr_number} (branch: {head_ref}, sha: {head_sha[:8] if head_sha else '?'})")

        token = get_installation_token(installation_id)

        # 1. Fetch metadata + diff + files.
        meta = get_pr_meta(owner, repo, pr_number, token)
        pr_title = meta.get("title", "")
        diff = get_pr_diff(owner, repo, pr_number, token)

        if not diff.strip():
            log(f"Empty diff for {owner}/{repo}#{pr_number}, skipping")
            return

        files = get_pr_files(owner, repo, pr_number, token)

        # 2. Sync local clone (auto-clones on first webhook for a new repo).
        repo_path = ensure_repo_synced(owner, repo, head_ref, head_sha, token)

        # 3. Load per-repo config, review memory, ADRs, and other open PRs.
        config = load_from_repo(repo_path)
        memory = load_memory(repo_path)
        try:
            adrs = find_adrs(repo_path)
            if adrs:
                log(f"Discovered {len(adrs)} ADR(s) in {owner}/{repo}")
        except Exception as e:  # noqa: BLE001
            log(f"ADR discovery failed (non-fatal): {e}")
            adrs = []

        # Fetch CI check-runs for the PR's head SHA. All failures are
        # non-fatal — the analyzer ignores an unfetched CIResult.
        ci = fetch_ci_results(token, owner, repo, head_sha)
        if ci.fetched:
            log(
                f"CI for {owner}/{repo}@{head_sha[:8]}: "
                f"{ci.passing} pass / {ci.failing} fail / {ci.in_progress} in-progress"
            )
        else:
            log(f"CI fetch skipped or failed for {owner}/{repo}@{head_sha[:8]}")
        try:
            other_prs = get_other_open_prs(owner, repo, pr_number, token)
        except Exception as e:  # noqa: BLE001
            log(f"Failed to fetch other PRs: {e}")
            other_prs = []

        # 4. Run pre-review analysis. Blast radius is off by default (see
        # analyze_pr docstring) because compute_blast_radius shells out per
        # added symbol and would block the webhook handler for ~10s×N on
        # big PRs. Re-enable per-repo via .ch-code-reviewer.yml when the
        # latency cost is worth paying.
        analysis = analyze_pr(
            files=files,
            pr_title=pr_title,
            diff_text=diff,
            other_open_prs=other_prs,
            repo_dir=repo_path,
            config=config,
            memory=memory,
            adrs=adrs,
            ci=ci,
        )
        log(
            f"Analysis: risk={analysis.risk.level} score={analysis.risk.score}, "
            f"drifted={analysis.scope.drifted}, gaps={len(analysis.gaps)}, "
            f"related={len(analysis.related)}, blast_symbols={len(analysis.blast.symbols)}"
        )

        # 5. Post pre-review analysis comment + apply labels.
        post_comment(owner, repo, pr_number, analysis.body(), token)
        apply_labels(owner, repo, pr_number, analysis.labels(), token)

        # 5a. Full-review fork. When the per-repo config or env default flips
        # this on, dispatch the PR to the multi-persona reviewer. Each
        # persona gets its own backend call (parallel); results are
        # aggregated and posted as a single review via post_review.
        if config.full_review or FULL_REVIEW_DEFAULT:
            log(f"Full-review mode for {owner}/{repo}#{pr_number}")
            personas = load_personas(config.personas, repo_path)
            log(
                f"Personas for {owner}/{repo}#{pr_number}: "
                f"{[p.name for p in personas]}"
            )
            try:
                result = run_full_review(
                    pr_number=pr_number,
                    personas=personas,
                    pr_meta=meta,
                    diff_text=diff,
                )
                log(
                    f"Full review for {owner}/{repo}#{pr_number}: "
                    f"verdict={result.overall_verdict} personas={len(result.verdicts)}"
                )
                inline = analysis.inline_comments()
                post_review(
                    owner, repo, pr_number,
                    result.body, token,
                    inline_comments=inline,
                    head_sha=head_sha or "",
                )
            except Exception as e:  # noqa: BLE001
                log(f"Full review failed for {owner}/{repo}#{pr_number}: {e!r}")
                post_comment(
                    owner, repo, pr_number,
                    f"**[seneschal full-review]** failed before posting: `{type(e).__name__}`. "
                    "Check seneschal logs for details.",
                    token,
                )
            return

        # 6. Single-pass review via the LLM backend.
        review_diff = diff[:50000] + ("\n\n... (diff truncated at 50KB)" if len(diff) > 50000 else "")

        addendum = analysis.prompt_addendum()
        addendum_block = f"\n\n## Pre-computed context\n\n{addendum}\n" if addendum else ""

        review_prompt = (
            f"Review this PR diff (branch: {head_ref}). Be concise (under 300 words). Check for:\n"
            "1. Bugs or logic errors\n"
            "2. Missed edge cases\n"
            "3. Style inconsistencies with surrounding code\n"
            "4. Functional completeness — does the PR actually work as-is?\n\n"
            "Verdict rules:\n"
            "- NEEDS CHANGES if: the PR has bugs, missing files required for it to work "
            "(e.g. DB migration for schema changes, missing imports, broken build), "
            "or would fail at runtime.\n"
            "- LGTM if: the code is correct and functional. Non-blocking observations "
            "(design tradeoffs, future considerations) are fine under LGTM.\n"
            "- The test: 'If I merge this right now, does it work?' If no → NEEDS CHANGES.\n\n"
            "Format: Start with a verdict (LGTM or NEEDS CHANGES), then bullet points.\n"
            "If LGTM, say so briefly. Do not pad with praise.\n"
            f"{addendum_block}\n"
            f"Diff:\n{review_diff}"
        )

        review_system = (
            "You are a code reviewer. Review quality rules:\n"
            "- Check that new code matches existing patterns and conventions in the repo.\n"
            "- Verify error handling is consistent with the rest of the codebase.\n"
            "- Flag any changes that could break existing functionality.\n"
            "- Do not nitpick style — focus on correctness and maintainability.\n"
            "- Pre-computed analysis (risk, scope, test gaps, blast radius) has already been posted "
            "as a separate comment — do not repeat it. Focus on runtime correctness."
        )

        try:
            text = get_backend().invoke(
                review_prompt,
                system_prompt=review_system,
                timeout=300,
            )
        except Exception as e:  # noqa: BLE001
            # Log the full exception for the operator via journalctl, but
            # post only the exception type to the public PR — the message
            # can contain provider-specific URLs or metadata.
            log(f"Backend invoke failed for {owner}/{repo}#{pr_number}: {e!r}")
            post_comment(
                owner, repo, pr_number,
                f"**[seneschal]** Review backend failed: `{type(e).__name__}`. "
                "Check seneschal logs for details.",
                token,
            )
            return

        if not text:
            log(f"Empty review output for {owner}/{repo}#{pr_number}")
            return

        body = f"## Automated Review\n\n{text}\n\n---\n*Reviewed by Seneschal*"
        inline = analysis.inline_comments()
        verdict = post_review(
            owner, repo, pr_number, body, token,
            inline_comments=inline,
            head_sha=head_sha or "",
        )

        # Auto-fix is not available in the public backend (the fix loop
        # needs a tool-using agent, which the API path does not wire). If
        # changes were requested, log and leave the PR for the author.
        if verdict == "REQUEST_CHANGES":
            log(
                f"Auto-fix not available in the public backend for "
                f"{owner}/{repo}#{pr_number} — REQUEST_CHANGES left for author"
            )

    except Exception as e:
        log(f"Error reviewing {owner}/{repo}#{pr_number}: {e}")


# `/webhook/seneschal` is canonical. The two aliases (`/webhook/rook` from
# the brief Rook-codename phase, and `/webhook/code-reviewer` from the
# original install) let the GitHub App webhook URL be flipped over lazily.
# Drop the aliases once the App settings have been updated to seneschal.
def _queue_review(owner, repo, pr_number, installation_id, head_ref, head_sha, trigger):
    """Kick off review_pr on a background thread.

    Used by both the pull_request-event path (when AUTOREVIEW_ENABLED is on)
    and the issue_comment `/seneschal review` path. Keeps the two trigger
    sources funneling into a single enqueue helper so behavior stays
    consistent.
    """
    log(
        f"Queueing review for {owner}/{repo}#{pr_number} "
        f"(branch: {head_ref}, trigger: {trigger})"
    )
    thread = threading.Thread(
        target=review_pr,
        args=(owner, repo, pr_number, installation_id, head_ref, head_sha),
        daemon=True,
    )
    thread.start()


def _queue_post_merge(owner, repo, pr_number, installation_id, pr_meta):
    """Spawn the post-merge orchestrator on a background thread.

    Same pattern as `_queue_review`: webhook handler returns 200 fast,
    the orchestrator chugs on its own. Exceptions in the orchestrator
    are swallowed (it returns a status dict).

    W4: the slow work (installation-token mint, `ensure_repo_synced`,
    `load_from_repo`) happens INSIDE the thread — not in the webhook
    handler — so a cold clone doesn't exceed GitHub's delivery timeout
    and pile up retried Flask workers on the same PR.
    """
    from post_merge.orchestrator import handle_pr_merged

    log(f"Queueing post-merge for {owner}/{repo}#{pr_number}")

    def _runner():
        # Per-PR advisory lock: GitHub retries webhook delivery on 5xx,
        # so two threads might enter the orchestrator for the same merge
        # event. Without this, both read followups_filed_titles=[] and
        # both file the followup issue. Lock-holder wins, the retry
        # sees the persisted state and no-ops.
        with _per_pr_lock(owner, repo, pr_number):
            try:
                token = get_installation_token(installation_id)
                base_ref = (pr_meta.get("base") or {}).get("ref") or "main"
                head_sha = pr_meta.get("merge_commit_sha") or (pr_meta.get("head") or {}).get("sha", "")
                repo_path = ensure_repo_synced(owner, repo, base_ref, head_sha, token)
                config = load_from_repo(repo_path)
            except Exception as e:  # noqa: BLE001
                log(f"[post_merge] config load failed for {owner}/{repo}#{pr_number}: {e!r}")
                return
            result = handle_pr_merged(
                owner=owner,
                repo=repo,
                pr_number=pr_number,
                installation_id=installation_id,
                pr_meta=pr_meta,
                config=config,
            )
        log(f"[post_merge] {owner}/{repo}#{pr_number} done: {result}")

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()


def _handle_pull_request_event(data):
    """Auto-trigger path. Gated by AUTOREVIEW_ENABLED."""
    action = data.get("action")

    pr = data.get("pull_request") or {}

    # Merge events: route to post-merge orchestrator regardless of
    # AUTOREVIEW_ENABLED. The orchestrator no-ops on a default config so
    # repos that haven't opted in see no behavior change.
    #
    # W4: repo sync + config load happen INSIDE the background thread
    # (see `_queue_post_merge`), not synchronously in the webhook
    # handler. A cold clone of a large repo can take >10s — long enough
    # that GitHub's delivery timeout fires and the webhook is retried
    # while the first handler is still running, stacking Flask workers
    # on the same PR. The handler here only does payload validation
    # (which is cheap + already done above) and queues the thread.
    if action == "closed" and pr.get("merged"):
        owner = data["repository"]["owner"]["login"]
        repo = data["repository"]["name"]
        pr_number = pr["number"]
        installation_id = data["installation"]["id"]
        _queue_post_merge(owner, repo, pr_number, installation_id, pr)
        return jsonify({"status": "post_merge_queued", "pr": pr_number}), 200

    # Closed-but-not-merged falls through to the ignore branch below; a
    # status_ignore is the right answer for those.
    if action == "closed":
        log(f"Ignored pull_request/closed for unmerged PR #{pr.get('number')}")
        return jsonify({"status": "ignored", "action": action, "reason": "closed_not_merged"}), 200

    # Only review on PR open or synchronize (new push).
    if action not in ("opened", "synchronize"):
        log(f"Ignored pull_request action: {action}")
        return jsonify({"status": "ignored", "action": action}), 200

    head_ref = pr["head"]["ref"]
    head_sha = pr["head"]["sha"]

    # Branch filter is opt-in via env var. With no filter set, the GitHub
    # App's installation list is the source of truth — repos picked in the
    # App settings page get reviewed automatically (and auto-cloned on
    # first webhook), with no second config to keep in sync.
    if BRANCH_FILTER and not re.search(BRANCH_FILTER, head_ref):
        log(f"Ignored branch: {head_ref} (filter={BRANCH_FILTER!r})")
        return jsonify({"status": "ignored", "reason": "branch filter"}), 200

    owner = data["repository"]["owner"]["login"]
    repo = data["repository"]["name"]
    pr_number = pr["number"]
    installation_id = data["installation"]["id"]

    # Auto-review kill switch. We deliberately log what we WOULD have
    # reviewed so the operator can still audit delivery traffic without
    # any side effects. To re-enable, set SENESCHAL_AUTOREVIEW=1 in the
    # systemd unit and restart the service.
    if not AUTOREVIEW_ENABLED:
        log(
            f"Auto-review disabled — received pull_request/{action} for "
            f"{owner}/{repo}#{pr_number} (branch: {head_ref}) but skipping. "
            "Use a `/seneschal review` comment or set SENESCHAL_AUTOREVIEW=1 to enable."
        )
        return jsonify({
            "status": "autoreview_disabled",
            "pr": pr_number,
            "note": "use a /seneschal review PR comment, or set SENESCHAL_AUTOREVIEW=1 to re-enable",
        }), 200

    _queue_review(owner, repo, pr_number, installation_id, head_ref, head_sha, trigger="auto")
    return jsonify({"status": "review_queued", "pr": pr_number, "trigger": "auto"}), 200


def _handle_issue_comment_event(data):
    """Comment-trigger path. Not gated by AUTOREVIEW_ENABLED because typing
    `/seneschal review` IS the explicit ask.

    Filters applied in order:
      1. action == "created" (ignore edits and deletes)
      2. the issue is actually a PR (`issue.pull_request` is non-null) —
         issue_comment events fire for bare issues too
      3. comment author is in COMMENT_TRIGGER_AUTHORS and is not the bot
         itself (self-trigger guard — review bodies may contain the phrase)
      4. comment body matches COMMENT_TRIGGER_RE on its own line
    """
    action = data.get("action")
    if action != "created":
        log(f"Ignored issue_comment action: {action}")
        return jsonify({"status": "ignored", "action": action}), 200

    issue = data.get("issue") or {}
    if not issue.get("pull_request"):
        return jsonify({"status": "ignored", "reason": "not a PR comment"}), 200

    comment = data.get("comment") or {}
    author = ((comment.get("user") or {}).get("login") or "").strip()
    body = comment.get("body") or ""

    # Self-trigger guard. The review body Seneschal posts may legitimately
    # contain the string "/seneschal review" in prose when discussing the
    # trigger itself; drop our own comments before we even look at the body.
    if author.endswith("[bot]") or author == "seneschal-cr[bot]":
        return jsonify({"status": "ignored", "reason": "bot self-comment"}), 200

    if author not in COMMENT_TRIGGER_AUTHORS:
        log(f"Comment trigger ignored: author {author!r} not in allowlist")
        return jsonify({"status": "ignored", "reason": "author not allowlisted"}), 200

    if not is_review_trigger_comment(body):
        # Not every comment by an allowlisted author should be noisy in logs
        # — only log the trigger-shaped attempts that fell through somehow.
        return jsonify({"status": "ignored", "reason": "no trigger command"}), 200

    owner = data["repository"]["owner"]["login"]
    repo = data["repository"]["name"]
    pr_number = issue["number"]
    installation_id = data["installation"]["id"]

    # The issue_comment payload does not include the PR's head ref/sha
    # (the `issue.pull_request` sub-object is sparse). Fetch PR meta so
    # we can sync the clone to the right commit before reviewing.
    try:
        token = get_installation_token(installation_id)
        pr_meta = get_pr_meta(owner, repo, pr_number, token)
        head_ref = pr_meta["head"]["ref"]
        head_sha = pr_meta["head"]["sha"]
    except Exception as e:  # noqa: BLE001
        log(f"Comment trigger: failed to fetch PR meta for {owner}/{repo}#{pr_number}: {e}")
        return jsonify({"status": "error", "detail": "failed to fetch PR metadata"}), 500

    log(
        f"Comment trigger accepted: {owner}/{repo}#{pr_number} "
        f"by {author} on branch {head_ref}"
    )

    # Visual ack: react with 👀 so the user knows we got the command
    # before the 30-second review completes. Non-fatal on failure.
    comment_id = comment.get("id")
    if comment_id:
        react_to_comment(owner, repo, comment_id, token)

    _queue_review(
        owner, repo, pr_number, installation_id, head_ref, head_sha,
        trigger="comment",
    )
    return jsonify({
        "status": "review_queued",
        "pr": pr_number,
        "trigger": "comment",
        "author": author,
    }), 200


@app.route("/webhook/seneschal", methods=["POST"])
@app.route("/webhook/rook", methods=["POST"])
@app.route("/webhook/code-reviewer", methods=["POST"])
def webhook():
    payload = request.get_data()
    signature = request.headers.get("X-Hub-Signature-256")

    if not verify_signature(payload, signature):
        log("Signature verification failed")
        return jsonify({"error": "Invalid signature"}), 403

    event = request.headers.get("X-GitHub-Event")
    data = request.get_json() or {}

    if event == "pull_request":
        return _handle_pull_request_event(data)
    if event == "issue_comment":
        return _handle_issue_comment_event(data)

    log(f"Ignored event: {event}")
    return jsonify({"status": "ignored", "event": event}), 200


@app.route("/webhook/seneschal", methods=["GET"])
@app.route("/webhook/rook", methods=["GET"])
@app.route("/webhook/code-reviewer", methods=["GET"])
def health():
    pem_exists = Path(PEM_PATH).exists()
    return jsonify({
        "status": "running",
        "app_id": APP_ID,
        "pem_configured": pem_exists,
        "backend": "api",
    })


if __name__ == "__main__":
    # Port can be overridden via SENESCHAL_PORT to support side-by-side
    # deployments (e.g., run v2 on 9101 while v1 still serves 9100 until
    # the GitHub App webhook URL is cut over).
    _port = int(os.environ.get("SENESCHAL_PORT", "9100"))
    log(f"Starting Seneschal webhook handler on port {_port}")
    # Check the LLM backend is configured before we come up. We log a loud
    # WARNING (not a hard fail) so Flask still binds and GitHub does not
    # back off webhook deliveries, but the operator sees the problem
    # immediately in `journalctl` right next to the "Starting" line.
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log("WARNING: ANTHROPIC_API_KEY is not set in the environment.")
        log("WARNING: Reviews will fail until the key is configured in the systemd unit.")
    else:
        log("LLM backend: ApiBackend (ANTHROPIC_API_KEY present)")
    app.run(host="127.0.0.1", port=_port)
