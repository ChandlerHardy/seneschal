"""Post-merge orchestration: glue between the pure modules and GitHub I/O.

Imported lazily by app.py on `pull_request/closed` webhook events when
`pr["merged"]` is True. Spawned on a background thread (same pattern as
`_queue_review`) so the webhook handler returns 200 fast and never blocks
GitHub's delivery loop on slow LLM / git operations.

Defensive: every disk + GitHub I/O failure is caught and logged. The
function never raises; on error it returns `{"error": <str>}` so the
caller can log a single line and move on.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

# Local sibling modules (these are pure).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402 — late binding so tests can patch `log`/`ensure_repo_synced` wholesale
import fs_safety  # noqa: E402 — path-safety primitives (safe_open_in_repo, etc.)
import github_api  # noqa: E402 — GitHub REST helpers live here post-refactor
import review_store  # noqa: E402
from fs_safety import now_iso, safe_open_in_repo  # noqa: E402
from github_api import PushProtectedError  # noqa: E402 — direct import, no re-export
from post_merge import changelog as changelog_mod  # noqa: E402
from post_merge import followups as followups_mod  # noqa: E402
from post_merge import release as release_mod  # noqa: E402

# Backward-compat alias for tests that imported the private name.
# `fs_safety.safe_open_in_repo` is the canonical home.
_safe_open_in_repo = safe_open_in_repo

# Process-local cache of which `owner/repo` combinations had `put_file` to
# main return 403. Entries are (monotonic_seconds, protected_bool). After
# `_PROTECTED_TTL_SEC` we re-probe — branch protection may have been
# removed and staying in auto-PR mode forever is wasteful.
#
# Uses `time.monotonic()` instead of `time.time()` so a wall-clock jump
# (NTP step forward/back, container wake-from-sleep) can't evict a fresh
# entry early or keep a stale entry alive forever. Monotonic is immune
# to those jumps.
_PROTECTED_REPOS: Dict[str, Tuple[float, bool]] = {}
_PROTECTED_TTL_SEC = 3600  # 1 hour


def _is_protected(repo_slug: str) -> bool:
    """Return True if `repo_slug` is currently known-protected (cache + TTL).

    An expired entry is evicted so the caller re-probes on the next write.
    """
    entry = _PROTECTED_REPOS.get(repo_slug)
    if entry is None:
        return False
    ts, protected = entry
    if not protected:
        return False
    if time.monotonic() - ts > _PROTECTED_TTL_SEC:
        # Expire: the next commit attempt will re-probe direct push.
        _PROTECTED_REPOS.pop(repo_slug, None)
        return False
    return True


def _mark_protected(repo_slug: str, protected: bool) -> None:
    """Record `repo_slug`'s protection state with a fresh timestamp."""
    _PROTECTED_REPOS[repo_slug] = (time.monotonic(), bool(protected))


def _read_local_changelog(repo_path: str, changelog_path: str) -> str:
    """Read CHANGELOG.md from the locally-synced clone. Empty string if missing.

    Wraps `_safe_open_in_repo` so a malicious symlink at `CHANGELOG.md`
    pointing outside the repo tree cannot exfiltrate host-sensitive
    contents into a subsequent `put_file` commit. The helper itself
    returns None on any safety violation, missing file, or I/O error;
    we normalize that to an empty string for callers that just want to
    branch on truthiness.

    Previously this function ran its own `os.path.exists + os.path.islink`
    precondition before calling `_safe_open_in_repo`. That was redundant
    — the helper's `os.lstat` walk + `O_NOFOLLOW` open already handles
    missing files + symlinks + traversal. The extra check only added a
    TOCTOU window between `os.path.exists` and the open.
    """
    if not repo_path:
        return ""
    content = _safe_open_in_repo(repo_path, changelog_path)
    return content or ""


# --------------------------------------------------------------------------
# Issue-body sanitization
# --------------------------------------------------------------------------

# The `body_excerpt` on a Followup is attacker-controllable (anyone who can
# post a review can seed the excerpt). When that excerpt is dropped into a
# `create_issue(body=...)` call, raw `@mentions` ping real users, `#123`
# autolinks cross-issues, markdown-image syntax loads tracking pixels on
# render, and HTML tags can reshape the issue body in unexpected ways.
#
# Strategy: wrap the excerpt in a fenced code block (so markdown doesn't
# interpret anything inside), and additionally neutralize @/# on each line
# by inserting a zero-width space so GitHub's autolinker doesn't match.
# Drop markdown-image syntax + HTML tags outright.

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_ZERO_WIDTH_SPACE = "​"


def _strip_md_images(text: str) -> str:
    """Remove markdown image syntax `![alt](url)`, balanced-paren aware.

    The obvious regex `!\\[[^\\]]*\\]\\([^)]*\\)` stops at the FIRST `)`
    and so mis-handles URLs with nested parens like
    `![alt](http://x/a(b).png)`. Hand-roll a depth counter: when we see
    `![`, scan to the matching `]`, then consume `(` and track paren
    depth until we hit the matching `)`.

    Silent fallthrough on malformed input (unclosed bracket / paren) —
    we return the text unchanged for that span so we never throw on
    attacker-controllable input.
    """
    if not text or "![" not in text:
        return text
    out = []
    i = 0
    n = len(text)
    while i < n:
        # Fast path for non-image chars.
        if text[i] != "!" or i + 1 >= n or text[i + 1] != "[":
            out.append(text[i])
            i += 1
            continue
        # Potential image start: `![`. Find matching `]`.
        j = i + 2
        while j < n and text[j] != "]":
            j += 1
        if j >= n or j + 1 >= n or text[j + 1] != "(":
            # Malformed — emit verbatim, advance 1.
            out.append(text[i])
            i += 1
            continue
        # Walk the balanced parens starting at j+1 (which is `(`).
        depth = 1
        k = j + 2
        while k < n and depth > 0:
            if text[k] == "(":
                depth += 1
            elif text[k] == ")":
                depth -= 1
            k += 1
        if depth != 0:
            # Unbalanced — emit verbatim.
            out.append(text[i])
            i += 1
            continue
        # Replace the full `![...](...)` span with a safe placeholder.
        out.append("[image removed]")
        i = k
    return "".join(out)


def _sanitize_issue_body(body_excerpt: str, pr_number: int, pr_url: str) -> str:
    """Turn a reviewer-supplied excerpt into a safe issue body.

    - strips markdown-image syntax (tracking-pixel vector)
    - strips HTML tags
    - neutralizes `@mention` / `#123` sigils with a zero-width space so
      GitHub's autolinker won't fire on issue creation
    - wraps the result in a fenced code block for an extra layer of
      literal rendering
    - appends a plain link back to the source PR so the issue is
      traceable
    """
    excerpt = body_excerpt or ""
    excerpt = _strip_md_images(excerpt)
    excerpt = _HTML_TAG_RE.sub("", excerpt)
    # Neutralize @/# by inserting a zero-width space after each, which
    # breaks GitHub's autolinker but keeps the text readable.
    excerpt = excerpt.replace("@", "@" + _ZERO_WIDTH_SPACE)
    excerpt = excerpt.replace("#", "#" + _ZERO_WIDTH_SPACE)
    # Defensive: ensure the fence itself isn't broken by a ``` in the
    # excerpt. Replace any triple-backtick with a single-backtick run.
    excerpt = excerpt.replace("```", "``​`")
    return (
        "```\n"
        f"{excerpt}\n"
        "```\n"
        "\n"
        f"Filed by Seneschal from PR #{int(pr_number)}: {pr_url}\n"
    )


# --------------------------------------------------------------------------
# Changelog step
# --------------------------------------------------------------------------


def _commit_changelog_direct(
    owner: str,
    repo: str,
    path: str,
    new_content: str,
    branch: str,
    message: str,
    token: str,
) -> None:
    """Attempt a direct push to `branch`. Returns None on success.

    Success is signaled by returning without raising. The previous
    `return True` was load-bearing nowhere (callers only inspected
    raised exceptions), so the bool was a type-lie. Failure modes:
      - `PushProtectedError` on 403 → caller switches to auto-PR mode.
      - `RuntimeError` on 409-retry-exhaustion → caller dead-letters.
      - any other Exception → bubbles to the outer orchestrator.
    """
    sha = github_api.get_file_sha(owner, repo, path, branch, token)
    github_api.put_file(
        owner=owner,
        repo=repo,
        path=path,
        content=new_content,
        message=message,
        branch=branch,
        sha=sha,
        token=token,
    )


def _commit_changelog_via_pr(
    owner: str,
    repo: str,
    path: str,
    new_content: str,
    base_branch: str,
    pr_number: int,
    message: str,
    token: str,
) -> Optional[int]:
    """Open (or reuse) an auto-PR with the changelog amendment.

    Returns the PR number, or None on failure.
    """
    branch_name = f"seneschal/changelog-{pr_number}"
    try:
        base_sha = github_api.get_default_branch_sha(owner, repo, base_branch, token)
        github_api.create_branch(owner, repo, branch_name, base_sha, token)
        sha = github_api.get_file_sha(owner, repo, path, branch_name, token)
        github_api.put_file(
            owner=owner,
            repo=repo,
            path=path,
            content=new_content,
            message=message,
            branch=branch_name,
            sha=sha,
            token=token,
        )
        pr = github_api.create_pull_request(
            owner=owner,
            repo=repo,
            title=f"chore(changelog): record #{pr_number}",
            body=f"Auto-generated by Seneschal — main is protected so the changelog update lands via PR.\n\nClosing #{pr_number} prompted this entry.",
            head=branch_name,
            base=base_branch,
            token=token,
            draft=False,
        )
        # Apply the seneschal:changelog label so the operator can filter.
        try:
            github_api.apply_labels(owner, repo, pr.get("number"), ["seneschal:changelog"], token)
        except Exception as e:  # noqa: BLE001
            app.log(f"[post_merge] apply_labels (changelog PR) failed: {e!r}")
        return int(pr.get("number") or 0) or None
    except Exception as e:  # noqa: BLE001
        app.log(f"[post_merge] changelog auto-PR failed for {owner}/{repo}#{pr_number}: {e!r}")
        return None


def _attempt_changelog_commit(
    owner: str,
    repo: str,
    pr_number: int,
    changelog_path: str,
    new_content: str,
    base_branch: str,
    commit_message: str,
    token: str,
) -> Tuple[str, Optional[str]]:
    """Attempt the direct-commit path. Returns (status, detail).

    Status values:
      - `"success"`  — direct commit went through
      - `"protected"`— branch rejected with 403; caller should fall back
                        to auto-PR mode
      - `"conflict"` — put_file gave up after 3 sha-conflict retries; the
                        change is LOST unless the caller dead-letters it
      - `"error"`    — any other exception
    """
    repo_slug = f"{owner}/{repo}"
    if _is_protected(repo_slug):
        return ("protected", None)
    try:
        _commit_changelog_direct(
            owner, repo, changelog_path, new_content,
            base_branch, commit_message, token,
        )
        return ("success", None)
    except PushProtectedError:
        app.log(f"[post_merge] {repo_slug} main protected — switching to auto-PR mode")
        _mark_protected(repo_slug, True)
        return ("protected", None)
    except RuntimeError as e:
        # put_file exhausts retries on repeated 409s and raises RuntimeError.
        msg = str(e)
        if "retries" in msg.lower() or "gave up" in msg.lower():
            return ("conflict", msg)
        return ("error", msg)
    except Exception as e:  # noqa: BLE001
        return ("error", repr(e))


def _dead_letter_changelog(
    owner: str,
    repo: str,
    pr_number: int,
    pr_title: str,
    config,
    token: str,
) -> Optional[int]:
    """File a GitHub issue when a changelog update couldn't be committed.

    Without this the merged-PR entry silently disappears from the
    changelog on sha-conflict-retry exhaustion.
    """
    label = config.post_merge.followup_label or "seneschal-followup"
    try:
        issue = github_api.create_issue(
            owner=owner,
            repo=repo,
            title=f"Seneschal: changelog update dropped for PR #{pr_number}",
            body=(
                f"Seneschal tried to append a changelog entry for "
                f"PR #{pr_number} ({pr_title!r}) but gave up after repeated "
                f"sha conflicts on the Contents API.\n\n"
                f"Manual fix: add the entry under `## [Unreleased]` in the "
                f"changelog yourself, or re-run the orchestrator.\n"
            ),
            labels=[label],
            token=token,
        )
        num = int(issue.get("number") or 0) or None
        app.log(
            f"[post_merge] dead-lettered changelog drop for "
            f"{owner}/{repo}#{pr_number} as issue #{num}"
        )
        return num
    except Exception as e:  # noqa: BLE001
        app.log(
            f"[post_merge] FAILED to dead-letter changelog drop "
            f"for {owner}/{repo}#{pr_number}: {e!r}"
        )
        return None


def _changelog_step(
    owner: str,
    repo: str,
    pr_number: int,
    pr_meta: dict,
    config,
    token: str,
    repo_path: str,
    breaking: bool = False,
) -> Tuple[bool, Optional[str]]:
    """Read CHANGELOG.md, insert the entry, push it.

    Returns (success, error_detail). `error_detail` is non-None only on
    the `"error"` terminal state so the orchestrator can surface it.
    """
    title = pr_meta.get("title") or ""
    pr_url = pr_meta.get("html_url") or f"https://github.com/{owner}/{repo}/pull/{pr_number}"
    kind = changelog_mod.classify_prefix(title) or "chore"
    entry = changelog_mod.format_unreleased_entry(pr_number, title, pr_url, breaking=breaking)
    existing = _read_local_changelog(repo_path, config.post_merge.changelog_path)
    new_content = changelog_mod.insert_unreleased_entry(existing, entry, kind, breaking=breaking)
    # W2: `insert_unreleased_entry` normalizes CRLF → LF internally and
    # always returns LF. Comparing against the raw `existing` (which may
    # carry CRLF from a Windows-origin file) would always be unequal and
    # trigger a spurious rewrite commit on every merge. Normalize the
    # RHS before compare so we correctly detect the no-op case.
    existing_normalized = changelog_mod.normalize_newlines(existing)
    if new_content == existing_normalized:
        # Nothing changed — skip the push.
        return (False, None)

    base_branch = config.post_merge.release_base_branch or "main"
    stripped = changelog_mod.strip_conventional_prefix(title)
    commit_message = f"chore(changelog): add #{pr_number} {stripped}"

    status, detail = _attempt_changelog_commit(
        owner, repo, pr_number,
        config.post_merge.changelog_path, new_content,
        base_branch, commit_message, token,
    )

    if status == "success":
        return (True, None)
    if status == "protected":
        pr_no = _commit_changelog_via_pr(
            owner, repo, config.post_merge.changelog_path, new_content,
            base_branch, pr_number, commit_message, token,
        )
        return (pr_no is not None, None)
    if status == "conflict":
        app.log(
            f"[post_merge] changelog commit gave up after retries for "
            f"{owner}/{repo}#{pr_number}: {detail}"
        )
        _dead_letter_changelog(owner, repo, pr_number, title, config, token)
        return (False, f"conflict: {detail}")
    # status == "error"
    app.log(
        f"[post_merge] changelog commit failed for {owner}/{repo}#{pr_number}: {detail}"
    )
    return (False, detail)


# --------------------------------------------------------------------------
# Followups step
# --------------------------------------------------------------------------


def _title_key(title: str) -> str:
    """Normalize a followup title for idempotent dedupe.

    Case-folded, whitespace-collapsed.
    """
    return " ".join((title or "").split()).casefold()


def _is_rate_limit_error(err: Exception) -> bool:
    """Return True if `err` looks like a GitHub 429 rate-limit response.

    Two recognition paths:
      - `requests.HTTPError` with `response.status_code == 429` (the
        typed signal when the error came from `session.raise_for_status()`
        in `github_api`)
      - message substring match on "rate limit" / "429" — defends against
        wrapped errors that lost the response object en route (e.g. an
        adapter re-raising as RuntimeError with the status in the text)

    W6 + Round 4 contract: other exception classes get `continue` so
    one transient 500 doesn't abort the loop, but 429 is special —
    continuing hammers 9 more requests that are GUARANTEED to fail
    and amplifies the pressure that triggered the limit.
    """
    try:
        import requests  # deferred — keeps fs_safety-style import ordering
    except ImportError:
        requests = None  # type: ignore

    if requests is not None and isinstance(err, requests.HTTPError):
        resp = getattr(err, "response", None)
        if resp is not None and getattr(resp, "status_code", None) == 429:
            return True
    msg = str(err).lower()
    if "429" in msg or "rate limit" in msg:
        return True
    return False


def _followups_step(
    owner: str,
    repo: str,
    pr_number: int,
    pr_meta: dict,
    config,
    token: str,
) -> Tuple[List[int], List[str]]:
    """Parse [FOLLOWUP] markers from the stored review and file issues for new ones.

    Returns (new_issue_numbers, new_titles) so the caller can persist both
    in the review record for future dedupe.

    Durability model (Round 4): mark_merged is called inline after each
    successful create_issue so a mid-loop persistence failure (disk
    full, permissions) doesn't cost the record of issues that already
    landed on GitHub. The outer caller in `handle_pr_merged` still calls
    mark_merged one more time with the full batch as a belt-and-braces
    consolidation, but per-issue persistence is the actual durability
    guarantee — a future mark_merged failure after 3 successful issues
    leaves all 3 titles persisted.
    """
    repo_slug = f"{owner}/{repo}"
    review = review_store.get_review(repo_slug, pr_number)
    if review is None:
        return ([], [])
    parsed = followups_mod.parse_followups(review.body)
    if not parsed:
        return ([], [])
    # Idempotence: dedupe by normalized title against the record's
    # previously-filed titles. Without this, any reviewer edit that
    # changes the followup *count* would re-file the whole set.
    already_titles = {_title_key(t) for t in (review.followups_filed_titles or [])}
    needed = [f for f in parsed if _title_key(f.title) not in already_titles]
    if not needed:
        return ([], [])
    label = config.post_merge.followup_label or "seneschal-followup"
    pr_url = pr_meta.get("html_url") or f"https://github.com/{owner}/{repo}/pull/{pr_number}"
    merged_at = pr_meta.get("merged_at") or now_iso()
    new_numbers: List[int] = []
    new_titles: List[str] = []
    # W6: previously a single transient create_issue failure (500, rate
    # limit) aborted the whole loop via `break`, so issues N+1..M were
    # never attempted. Partial persistence + title-keyed dedupe in
    # mark_merged makes retry safe for 5xx: continue past, persist
    # what succeeded, let a future retry pick up the missing ones.
    #
    # Round 4: rate-limit (429) is the ONE exception class where
    # `break` is the right answer. Continuing hammers 9 more requests
    # that are guaranteed to fail and amplifies the pressure that
    # triggered the limit in the first place.
    failures = 0
    for f in needed:
        try:
            body = _sanitize_issue_body(f.body_excerpt, pr_number, pr_url)
            issue = github_api.create_issue(
                owner=owner,
                repo=repo,
                title=f.title,
                body=body,
                labels=[label],
                token=token,
            )
            num = int(issue.get("number") or 0)
            if num:
                new_numbers.append(num)
                new_titles.append(f.title)
                # Round 4 per-issue durability: persist IMMEDIATELY
                # after each successful create_issue. If mark_merged
                # itself raises (disk full, permissions), log and
                # continue — the issue landed on GitHub, we just lost
                # one dedupe record for it. The trailing batched
                # mark_merged in handle_pr_merged still fires as a
                # belt-and-braces consolidation.
                try:
                    review_store.mark_merged(
                        repo_slug, pr_number, merged_at,
                        [num], followup_titles=[f.title],
                    )
                except Exception as persist_err:  # noqa: BLE001
                    app.log(
                        f"[post_merge] per-issue mark_merged failed for "
                        f"{repo_slug}#{pr_number} issue #{num}: "
                        f"{persist_err!r} (issue was created; retry "
                        f"may re-create as duplicate)"
                    )
        except Exception as e:  # noqa: BLE001
            failures += 1
            if _is_rate_limit_error(e):
                app.log(
                    f"[post_merge] create_issue hit rate limit on "
                    f"{repo_slug}#{pr_number} after filing {len(new_numbers)} "
                    f"issue(s); breaking to avoid amplifying pressure"
                )
                break
            app.log(
                f"[post_merge] create_issue failed on {repo_slug}#{pr_number} "
                f"for followup {f.title!r}: {e!r} (continuing; will retry "
                f"on next delivery)"
            )
            continue
    if failures and not new_numbers:
        # All attempts failed — log once at a higher level so operators
        # see the outage rather than one line per issue.
        app.log(
            f"[post_merge] ALL {failures} followup(s) failed to file on "
            f"{repo_slug}#{pr_number}; none were persisted"
        )
    return (new_numbers, new_titles)


# --------------------------------------------------------------------------
# Release step
# --------------------------------------------------------------------------


_UNRELEASED_RE = re.compile(
    r"^## \[Unreleased\][^\n]*\n(.*?)(?=^## |\Z)",
    re.MULTILINE | re.DOTALL,
)
_PYPROJECT_VERSION_RE = re.compile(
    r'^\s*version\s*=\s*"([^"]+)"',
    re.MULTILINE,
)


def _current_version(repo_path: str) -> Optional[str]:
    """Best-effort: discover the repo's current semver.

    Checks, in order:
      1. `pyproject.toml` — `version = "X"` in either `[project]` or
         `[tool.poetry]` table (we don't parse TOML, just match the line)
      2. `package.json` — `version` key
      3. `VERSION` file (literal)
      4. `git describe --tags --abbrev=0`

    Returns None if none of those yield a value that parses as semver.

    All file reads go through `_safe_open_in_repo` so a malicious
    symlink at any of these paths pointing outside the repo tree is
    refused (see `_safe_open_in_repo` docstring).
    """
    if not repo_path:
        return None
    # 1. pyproject.toml
    py_content = _safe_open_in_repo(repo_path, "pyproject.toml")
    if py_content:
        m = _PYPROJECT_VERSION_RE.search(py_content)
        if m:
            return m.group(1).strip()
    # 2. package.json
    pkg_content = _safe_open_in_repo(repo_path, "package.json")
    if pkg_content:
        try:
            data = json.loads(pkg_content)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            ver = data.get("version")
            if isinstance(ver, str) and ver.strip():
                return ver.strip()
    # 3. VERSION
    ver_content = _safe_open_in_repo(repo_path, "VERSION")
    if ver_content:
        ver = ver_content.strip()
        if ver:
            return ver
    # 4. git describe
    try:
        proc = subprocess.run(
            ["git", "-C", repo_path, "describe", "--tags", "--abbrev=0"],
            capture_output=True,
            timeout=5,
        )
        if proc.returncode == 0:
            out = proc.stdout.decode("utf-8", errors="replace").strip()
            if out:
                return out
    except (subprocess.SubprocessError, OSError):
        pass
    return None


def _release_branch_name(
    repo_path: str, bump_kind: str, current_version: Optional[str] = None,
) -> str:
    """Compute the release-PR branch name.

    If a current version is discoverable, compute the next version and
    use it. Otherwise use `seneschal/release-pending-<bump_kind>` (NOT
    `-next` — that placeholder hid the bump intent in the branch name).

    `current_version` is an optional caller-provided cache: `_release_step`
    calls `_current_version(repo_path)` once and threads the result
    through here so we don't re-read pyproject.toml + package.json +
    VERSION + shell out to git twice per release step.
    """
    current = current_version if current_version is not None else _current_version(repo_path)
    if current:
        try:
            new_version = release_mod.next_version(current, bump_kind)
            return f"seneschal/release-{new_version}"
        except ValueError:
            pass
    return f"seneschal/release-pending-{bump_kind}"


def _release_step(
    owner: str,
    repo: str,
    pr_number: int,
    pr_meta: dict,
    config,
    token: str,
    repo_path: str,
) -> Optional[int]:
    """Open or amend a release PR if accumulated bump >= configured threshold."""
    threshold = (config.post_merge.release_threshold or "").lower()
    if threshold not in {"patch", "minor", "major"}:
        return None
    existing = _read_local_changelog(repo_path, config.post_merge.changelog_path)
    if not existing:
        return None
    # Extract the Unreleased block.
    m = _UNRELEASED_RE.search(existing)
    if not m:
        return None
    unreleased_lines = m.group(1).split("\n")
    bump = release_mod.bump_kind(unreleased_lines)

    # Commit-body scan: a PR body may not carry `!` in the title but may
    # still include `BREAKING CHANGE:` in a commit message. Fetching the
    # PR's commits is the only way to catch that signal. If any commit
    # message body has that marker, force a major bump.
    try:
        commits = github_api.get_pr_commits(owner, repo, pr_number, token) or []
    except Exception as e:  # noqa: BLE001
        app.log(f"[post_merge] get_pr_commits failed for {owner}/{repo}#{pr_number}: {e!r}")
        commits = []
    if commits and _commits_signal_breaking(commits):
        bump = "major"

    order = {"patch": 0, "minor": 1, "major": 2}
    if order.get(bump, 0) < order.get(threshold, 0):
        return None

    # Cache the current version once per _release_step invocation.
    # Previously this was called twice (inside `_release_branch_name` and
    # again for the release-notes body below) — each call reads
    # pyproject.toml + package.json + VERSION and may shell out to git.
    current_ver = _current_version(repo_path)

    # If a release PR is already open, amend it instead of opening another.
    try:
        existing_prs = github_api.find_open_prs_with_label(owner, repo, "seneschal:release", token)
    except Exception as e:  # noqa: BLE001
        app.log(f"[post_merge] find_open_prs_with_label failed: {e!r}")
        existing_prs = []
    if existing_prs:
        return _amend_release_pr(
            owner, repo, existing_prs[0],
            config.post_merge.changelog_path, existing, token,
            release_base_branch=config.post_merge.release_base_branch or "main",
        )

    # Open a fresh release PR.
    branch = _release_branch_name(repo_path, bump, current_version=current_ver)
    base = config.post_merge.release_base_branch or "main"

    # Build a structured release-notes body via `release.render_release_notes`
    # instead of the previous hand-rolled string. The Unreleased block from
    # the changelog gets its header rewritten to `## [<new_version>] - <date>`
    # and the rest of the subsections (Added / Fixed / etc.) are preserved
    # verbatim — so the PR description shows exactly what will land in the
    # tagged release. Falls back to a minimal body if the version is
    # unknown or `next_version` rejects the current string as non-semver.
    unreleased_section = m.group(0) if m else ""
    new_version_for_body: Optional[str] = None
    if current_ver:
        try:
            new_version_for_body = release_mod.next_version(current_ver, bump)
        except ValueError:
            new_version_for_body = None
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if new_version_for_body and unreleased_section:
        release_notes_md = release_mod.render_release_notes(
            unreleased_section, new_version_for_body, today_iso,
        )
        pr_body = (
            f"Accumulated `## [Unreleased]` entries warrant a `{bump}` bump.\n\n"
            "## Release notes preview\n\n"
            f"{release_notes_md}\n\n"
            "---\n"
            "Finalize the version + cut the tag when ready."
        )
    else:
        pr_body = (
            f"Accumulated `## [Unreleased]` entries warrant a `{bump}` bump.\n\n"
            "Finalize the version + cut the tag when ready."
        )

    try:
        base_sha = github_api.get_default_branch_sha(owner, repo, base, token)
        github_api.create_branch(owner, repo, branch, base_sha, token)
        # Touch the changelog on the new branch so the PR has a diff.
        sha = github_api.get_file_sha(owner, repo, config.post_merge.changelog_path, branch, token)
        github_api.put_file(
            owner=owner,
            repo=repo,
            path=config.post_merge.changelog_path,
            content=existing,
            message=f"chore(release): prep release ({bump} bump)",
            branch=branch,
            sha=sha,
            token=token,
        )
        try:
            pr = github_api.create_pull_request(
                owner=owner,
                repo=repo,
                title=f"chore(release): {bump} release prep",
                body=pr_body,
                head=branch,
                base=base,
                token=token,
                draft=bool(config.post_merge.release_pr_draft),
            )
        except Exception as pr_err:  # noqa: BLE001
            # 422 race: another post-merge thread opened the release PR
            # between our find_open_prs_with_label + create_pull_request.
            # Re-check and fall through to amend-mode rather than bubbling.
            if _is_already_exists_error(pr_err):
                app.log(
                    f"[post_merge] release PR create hit 422 race; "
                    f"re-checking open PRs for {owner}/{repo}"
                )
                try:
                    retry_prs = github_api.find_open_prs_with_label(
                        owner, repo, "seneschal:release", token,
                    )
                except Exception:  # noqa: BLE001
                    retry_prs = []
                if retry_prs:
                    return _amend_release_pr(
                        owner, repo, retry_prs[0],
                        config.post_merge.changelog_path, existing, token,
                        release_base_branch=config.post_merge.release_base_branch or "main",
                    )
            raise
        try:
            github_api.apply_labels(owner, repo, pr.get("number"), ["seneschal:release"], token)
        except Exception as e:  # noqa: BLE001
            app.log(f"[post_merge] apply_labels (release PR) failed: {e!r}")
        return int(pr.get("number") or 0) or None
    except Exception as e:  # noqa: BLE001
        app.log(f"[post_merge] open release-PR failed for {owner}/{repo}: {e!r}")
        return None


def _amend_release_pr(
    owner: str,
    repo: str,
    existing_pr: dict,
    changelog_path: str,
    changelog_content: str,
    token: str,
    release_base_branch: str,
) -> Optional[int]:
    """Refresh the changelog on an already-open seneschal:release PR's branch.

    W2: `changelog_content` is the caller's snapshot, which was read
    BEFORE this merge's `_changelog_step` pushed its entry. Writing it
    back directly would overwrite the just-added entry with stale
    content. Re-fetch the canonical CHANGELOG from the release-base
    branch (typically `main`) right before the put_file so the amend
    reflects the latest state.

    Falls back to the caller's snapshot if the fresh fetch RAISES (network
    error, transient 5xx). A `(None, None)` return is treated as a
    successful fetch with no content (the base branch legitimately has no
    CHANGELOG yet — nothing failed). Only a raised exception tags the
    commit message with `[stale snapshot — fresh fetch failed]`.

    Blocker 2 (round 3): the fetch + put_file used to share ONE try
    block, so a fetch failure silently skipped the write — the
    docstring claimed a snapshot fallback but the code never reached
    it. Split into two try blocks: fetch in the first (fall back to
    snapshot + tag the commit message so reviewers see the amend was
    based on stale content), put_file in the second.

    Round 4 contract tightening:
      - Missing `head.ref` → `ValueError`. Previously the function
        returned the PR number silently, claiming success on zero work.
      - `put_file` raising → propagates. Previously the exception was
        caught + logged and the PR number was still returned, so the
        caller (`_release_step`) thought the amend landed when the
        release branch got no commit.
      - A `(None, _)` fetch (genuine 404 — base branch has no
        CHANGELOG yet) is treated as a *successful* empty fetch. The
        snapshot-tag only fires when the fetch RAISED.
      - `release_base_branch` is now required; callers already pass it.
    """
    head_ref = (existing_pr.get("head") or {}).get("ref")
    if not head_ref:
        # Silent no-op was hiding bugs — raise so the outer `_release_step`
        # catches, logs, and skips populating `result["release_pr"]`.
        raise ValueError(
            f"amend target has no head.ref; existing_pr={existing_pr!r}"
        )

    # Fetch-step: try to pull the canonical CHANGELOG from the release
    # base. Distinguish two failure modes cleanly:
    #   - fetch RAISED (network error, 5xx): fall back to caller snapshot
    #     AND tag the commit message so reviewers see the amend is built
    #     on possibly-stale content.
    #   - fetch returned `(None, _)` (genuine 404 — base branch legit has
    #     no CHANGELOG): ALSO fall back to caller snapshot, but DON'T
    #     tag the message as a failure — nothing failed, the branch is
    #     just empty of this file.
    fresh_fetch_ok = False
    # Normalize snapshot CRLF upfront. `insert_unreleased_entry` emits
    # LF-only output so the caller's snapshot is already LF — but if
    # this helper grows another callsite someday, this defends against
    # accidentally writing CRLF back on a LF branch and producing a
    # diff that's 100% line-ending noise.
    content_to_write = changelog_mod.normalize_newlines(changelog_content)
    try:
        fresh_content, _base_sha = github_api.get_file_content(
            owner, repo, changelog_path, release_base_branch, token,
        )
    except Exception as e:  # noqa: BLE001
        app.log(
            f"[post_merge] amend release-PR: fresh fetch failed "
            f"({e!r}); falling back to caller's snapshot"
        )
    else:
        # Fetch succeeded. `None` / empty just means the base branch has
        # no CHANGELOG (genuine 404 from the contents API). That's a
        # valid state — we don't tag the commit as stale.
        fresh_fetch_ok = True
        if fresh_content:
            content_to_write = changelog_mod.normalize_newlines(fresh_content)
        else:
            app.log(
                f"[post_merge] amend release-PR: fresh fetch of "
                f"{changelog_path!r} on {release_base_branch!r} returned empty; "
                f"using caller's snapshot (base branch has no changelog yet)"
            )

    commit_message = "chore(release): refresh CHANGELOG (Seneschal)"
    if not fresh_fetch_ok:
        commit_message += " [stale snapshot — fresh fetch failed]"

    # Put-step: independent try block — we WANT an exception here to
    # propagate (round-4 contract). Swallowing it and returning the PR
    # number would claim success on a failed write, which the caller
    # (`_release_step`) will then stash in `result["release_pr"]`.
    # Letting it raise lets the outer handler log + skip that key.
    sha = github_api.get_file_sha(owner, repo, changelog_path, head_ref, token)
    github_api.put_file(
        owner=owner,
        repo=repo,
        path=changelog_path,
        content=content_to_write,
        message=commit_message,
        branch=head_ref,
        sha=sha,
        token=token,
    )

    return int(existing_pr.get("number") or 0) or None


def _is_already_exists_error(err: Exception) -> bool:
    """Return True if `err` looks like a GitHub 422 "PR already exists".

    W3: Previously matched any 422 containing "pull request" as a
    substring — false positives on unrelated validation errors (e.g.
    "the pull request body is invalid" from a different validation
    failure). Tighten: require `"already exists"` specifically, since
    that's the fingerprint of GitHub's "A pull request already exists
    for ..." response body.
    """
    # requests-style HTTP errors carry a `response` attribute on
    # `HTTPError`. The API returns 422 with a body containing
    # "A pull request already exists for ...".
    msg = str(err).lower()
    if "422" in msg and "already exists" in msg:
        return True
    resp = getattr(err, "response", None)
    if resp is None:
        return False
    try:
        if getattr(resp, "status_code", None) == 422:
            text = (getattr(resp, "text", "") or "").lower()
            return "already exists" in text
    except Exception:  # noqa: BLE001
        return False
    return False


def _commits_signal_breaking(commits: List[dict]) -> bool:
    """Scan PR commit objects for a Conventional Commits breaking marker.

    GitHub's list-commits endpoint returns objects of the shape
    `{"commit": {"message": "..."}, ...}`. Each full message (title +
    body) is handed to `changelog_mod.is_breaking_title`, which checks
    both the `!` marker in the first line AND the line-anchored
    `BREAKING CHANGE:` footer in the body.

    Previously this function ran its own inline regex after the helper
    call — redundant, since `is_breaking_title` already checks the
    same line-anchored pattern. Unified via the helper so the breaking
    detection rules live in one place (`changelog.py`).
    """
    for c in commits:
        if not isinstance(c, dict):
            continue
        commit = c.get("commit") or {}
        msg = commit.get("message") or ""
        if changelog_mod.is_breaking_title(msg):
            return True
    return False


# --------------------------------------------------------------------------
# Top-level entrypoint
# --------------------------------------------------------------------------


def handle_pr_merged(
    owner: str,
    repo: str,
    pr_number: int,
    installation_id: int,
    pr_meta: dict,
    config,
) -> dict:
    """Top-level entrypoint. Sequences changelog → followups → release.

    Returns a status dict for caller logging; never raises. Per-PR locking
    happens at the app.py layer (`_per_pr_lock`) so retried webhook
    deliveries don't double-fire this function.
    """
    result: Dict = {
        "changelog_updated": False,
        "followups_filed": [],
        "release_pr": None,
    }
    try:
        token = github_api.get_installation_token(installation_id)

        # Sync the local clone so changelog reads + analyzer-style ops can
        # see the post-merge tree.
        head_ref = (pr_meta.get("base") or {}).get("ref") or config.post_merge.release_base_branch or "main"
        head_sha = pr_meta.get("merge_commit_sha") or (pr_meta.get("head") or {}).get("sha", "")
        try:
            repo_path = app.ensure_repo_synced(owner, repo, head_ref, head_sha, token)
        except Exception as e:  # noqa: BLE001
            app.log(f"[post_merge] repo sync failed for {owner}/{repo}#{pr_number}: {e!r}")
            repo_path = ""

        # Detect breaking-change intent early so the changelog entry can
        # be routed to `### Removed` + marked `**BREAKING**`.
        title = pr_meta.get("title") or ""
        breaking = changelog_mod.is_breaking_title(title)

        # 1. Changelog.
        if config.post_merge.changelog and repo_path:
            try:
                ok, err_detail = _changelog_step(
                    owner, repo, pr_number, pr_meta, config, token, repo_path,
                    breaking=breaking,
                )
                result["changelog_updated"] = ok
                if err_detail:
                    result.setdefault("error", err_detail)
            except Exception as e:  # noqa: BLE001
                app.log(f"[post_merge] changelog step failed for {owner}/{repo}#{pr_number}: {e!r}")

        # 2. Followups.
        new_followups: List[int] = []
        new_titles: List[str] = []
        if config.post_merge.followups:
            try:
                new_followups, new_titles = _followups_step(
                    owner, repo, pr_number, pr_meta, config, token,
                )
            except Exception as e:  # noqa: BLE001
                app.log(f"[post_merge] followups step failed for {owner}/{repo}#{pr_number}: {e!r}")
        result["followups_filed"] = new_followups

        # 3. Mark merged in the review store (records merged_at + dedup the
        # followup numbers + titles). Always do this when there's a stored
        # review, even if no followups fired this round — gives P2's index
        # a stable `merged_at` field.
        merged_at = pr_meta.get("merged_at") or now_iso()
        try:
            review_store.mark_merged(
                f"{owner}/{repo}",
                pr_number,
                merged_at,
                new_followups,
                followup_titles=new_titles,
            )
        except Exception as e:  # noqa: BLE001
            app.log(f"[post_merge] mark_merged failed for {owner}/{repo}#{pr_number}: {e!r}")

        # 4. Release PR (depends on the changelog already being updated).
        if config.post_merge.release_threshold and repo_path:
            try:
                result["release_pr"] = _release_step(
                    owner, repo, pr_number, pr_meta, config, token, repo_path,
                )
            except Exception as e:  # noqa: BLE001
                app.log(f"[post_merge] release step failed for {owner}/{repo}#{pr_number}: {e!r}")

        return result
    except Exception as e:  # noqa: BLE001
        app.log(f"[post_merge] orchestrator failed for {owner}/{repo}#{pr_number}: {e!r}\n{traceback.format_exc()}")
        result["error"] = f"{type(e).__name__}: {e}"
        return result
