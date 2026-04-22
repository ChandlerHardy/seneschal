"""Path-safety primitives shared across Seneschal modules.

Consolidates the filesystem-traversal + repo-slug + sensitive-path
defenses that used to live in `post_merge/orchestrator.py`,
`review_store.py`, and `repo_config.py`. Keeping them in one module
means a single fix lands in one place when (not if) one of these
primitives needs tightening.

Threat model recap:
  - `safe_open_in_repo`: defends against malicious-PR symlinks that
    point the changelog/version reader at host-sensitive files like
    the GitHub App PEM.
  - `validate_repo_slug`: defends `review_store` path-joins against
    traversal via a user-supplied `owner/repo` parameter.
  - `safe_changelog_path` / `safe_branch_name`: defends against a
    rogue `.seneschal.yml` redirecting the auto-commit at protected
    workflow files / branch-protection bypasses.
"""

from __future__ import annotations

import errno
import os
import re
import stat
from datetime import datetime, timezone
from typing import Optional


# --------------------------------------------------------------------------
# Shared utility helpers
# --------------------------------------------------------------------------


def now_iso() -> str:
    """Return the current UTC time as an ISO-8601 Z-suffix string.

    Shared between review_store (frontmatter `timestamp` / `merged_at`)
    and post_merge.orchestrator (merge timestamps) so both paths produce
    byte-identical stamps.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Repo-slug validation (used by review_store)
# --------------------------------------------------------------------------

REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")


def validate_repo_slug(repo_slug: str) -> None:
    """Raise ValueError if repo_slug isn't a simple owner/repo form.

    Guards against path traversal via the repo_slug parameter (MCP
    clients are local but we still defend).
    """
    if not REPO_SLUG_RE.match(repo_slug):
        raise ValueError(f"invalid repo slug: {repo_slug!r}")


# --------------------------------------------------------------------------
# Sensitive path / filename deny-list (used by repo_config.changelog_path)
# --------------------------------------------------------------------------

# `changelog_path` flows to `github_api.put_file(path=...)`. A rogue
# `.seneschal.yml` committing `changelog_path: ../.github/workflows/x.yml`
# would let anyone with push access redirect Seneschal's auto-commit at
# a protected workflow file. Keep it confined to repo-relative, forward
# slashes only, no traversal segments.
#
# Deny-list: even after traversal rejection, explicit `.github/CODEOWNERS`
# or `SECURITY.md` is a valid relative path that would pass the naive
# safety check. A rogue config could redirect the auto-commit at any of
# these and wipe branch-protection reviewers, corrupt CI, or replace the
# license. Block them here.
#
# Frozenset so the list is inspectable from tests / operators.
SENSITIVE_PATH_SEGMENTS = frozenset({
    ".github",
    ".git",
})
SENSITIVE_FILENAMES = frozenset({
    "codeowners",
    ".gitattributes",
    ".gitignore",
    "security.md",
    "license",
    "license.md",
    "license.txt",
    ".env",
    "dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
})


def safe_changelog_path(path: str) -> Optional[str]:
    """Return `path` if it's a safe repo-relative file path, else None.

    Rejects:
      - absolute paths (`/foo`, `C:\\foo`)
      - backslashes (Windows-style, banned outright)
      - `..` segments (parent-dir traversal)
      - empty / whitespace-only strings
      - any path starting with `.github/` or `.git/` (sensitive dirs)
      - any file whose basename matches `SENSITIVE_FILENAMES` case-insensitive
        (CODEOWNERS, SECURITY.md, LICENSE, .env, Dockerfile, etc.)
    """
    if not path or not path.strip():
        return None
    if "\\" in path:
        return None
    if path.startswith("/"):
        return None
    norm = os.path.normpath(path)
    if norm.startswith("/") or norm == "..":
        return None
    # Split on POSIX `/` so we catch `../foo` even after normpath
    # normalizes it to `../foo` rather than collapsing it.
    parts = norm.split("/")
    for part in parts:
        if part == "..":
            return None
    # Deny-list: reject any path whose first segment is a sensitive dir
    # (.github/*, .git/*) OR whose basename is a sensitive filename
    # (CODEOWNERS, SECURITY.md, etc.). Compare case-insensitively because
    # some filesystems (macOS HFS+, Windows) are case-insensitive, and
    # GitHub itself treats CODEOWNERS / Codeowners / codeowners as the
    # same file at apply-time.
    lowered_parts = [p.lower() for p in parts if p]
    if lowered_parts:
        head = lowered_parts[0]
        if head in SENSITIVE_PATH_SEGMENTS:
            return None
        # Any segment named `.git` anywhere in the path (eg `foo/.git/HEAD`).
        if any(p == ".git" for p in lowered_parts):
            return None
        # Basename check.
        basename = lowered_parts[-1]
        if basename in SENSITIVE_FILENAMES:
            return None
    return norm


# `release_base_branch` lands in the `base` field of a create-pull-request
# call. A value like `main?admin=1` could be used to tack query params onto
# the downstream GitHub API URL in the rare future where we interpolate it
# into a path (e.g. `git/ref/heads/<branch>`). Validate against git's own
# ref-name rules (the strict subset we need).
_BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9_\-./]+$")


def safe_branch_name(name: str) -> Optional[str]:
    """Return `name` if it looks like a valid git branch name, else None.

    Applies git's ref-name rules (the strict subset we need):
      - `[A-Za-z0-9_\\-./]+` only
      - max 100 chars
      - no leading/trailing slash or dot
      - no consecutive dots (`..`)
    """
    if not name or not name.strip():
        return None
    if len(name) > 100:
        return None
    if not _BRANCH_NAME_RE.match(name):
        return None
    if name.startswith("/") or name.endswith("/"):
        return None
    if name.startswith(".") or name.endswith("."):
        return None
    if ".." in name:
        return None
    return name


# --------------------------------------------------------------------------
# Symlink-safe repo file read (used by post_merge.orchestrator)
# --------------------------------------------------------------------------


def safe_open_in_repo(repo_path: str, rel_path: str) -> Optional[str]:
    """Read `rel_path` from `repo_path`, refusing symlink traversal.

    Attacker vector: a malicious PR commits `CHANGELOG.md` (or any file
    this module reads) as a symlink pointing at host-sensitive paths
    like `~/seneschal/ch-code-reviewer.pem` or `/etc/passwd`. Without
    guarding, `_read_local_changelog` would return that file's contents
    and `put_file` would write them into the repo where the attacker
    has view access — a pem-key exfil.

    Defense (belt + suspenders):
      1. `os.path.realpath` both paths and confirm the resolved file is
         WITHIN the resolved repo tree via `os.path.commonpath`.
      2. `os.lstat` each INTERMEDIATE path component (W5 round 3): the
         old code only guarded the final path component against being a
         symlink via `O_NOFOLLOW`, but an attacker could replace an
         intermediate directory (e.g. `docs/`) with a symlink between
         realpath and open. We pre-check every component and reject if
         any is a symlink.
      3. `os.open(..., O_RDONLY | O_NOFOLLOW)` on the target so the
         kernel refuses to follow a symlink at read time — closes the
         TOCTOU window on the final component.

    Returns the file contents as a string, or None on any safety
    violation or I/O error. Logs a warning when traversal is blocked.
    """
    # Deferred import to avoid a top-level dependency on app.log (which
    # lives in app.py and pulls Flask transitively). The safety helpers
    # need to be importable from the MCP server too.
    from app import log

    if not repo_path or not rel_path:
        return None
    try:
        repo_root = os.path.realpath(repo_path)
    except OSError:
        return None
    candidate = os.path.join(repo_path, rel_path)
    try:
        resolved = os.path.realpath(candidate)
    except OSError:
        return None
    # commonpath() raises ValueError on mixed drives (Windows) or empty
    # paths; treat that defensively as "not in the repo tree".
    try:
        if os.path.commonpath([resolved, repo_root]) != repo_root:
            log(
                f"[post_merge] refused to read {rel_path!r} from {repo_path!r}: "
                f"resolves outside repo tree ({resolved!r})"
            )
            return None
    except ValueError:
        log(
            f"[post_merge] refused to read {rel_path!r}: "
            f"path comparison failed (mixed roots)"
        )
        return None
    # W5: intermediate-component symlink check. `O_NOFOLLOW` only guards
    # the final component. If the attacker symlinks `docs/` → `/etc`,
    # then `docs/CHANGELOG.md` would still open `/etc/CHANGELOG.md`
    # (realpath resolves the intermediate symlink), and our commonpath
    # check can be bypassed if the symlink target happens to match the
    # repo-root realpath. Walk each intermediate component with lstat
    # and refuse if any is a symlink.
    #
    # Pragmatic threat model: the attacker has push access but not
    # host FS write — they stage symlinks via the git tree. A perfect
    # openat-style traversal would be better but CPython doesn't
    # expose openat directly; lstat-per-component is good enough.
    rel_norm = os.path.normpath(rel_path)
    parts = [p for p in rel_norm.split(os.sep) if p and p != "."]
    current = repo_path
    # Walk intermediate dirs (everything except the final component).
    for intermediate in parts[:-1]:
        current = os.path.join(current, intermediate)
        try:
            st = os.lstat(current)
        except OSError:
            # Missing intermediate dir — will fail at open anyway.
            return None
        if stat.S_ISLNK(st.st_mode):
            log(
                f"[post_merge] refused to read {rel_path!r}: "
                f"intermediate component {intermediate!r} is a symlink"
            )
            return None
    # O_NOFOLLOW on the FINAL path component: if the target itself is a
    # symlink (even if its realpath lands inside the repo), refuse. This
    # closes a TOCTOU window where the file is swapped for a symlink
    # between the realpath check and the open().
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(candidate, flags)
    except OSError as e:
        # ELOOP = symlink loop, or (on O_NOFOLLOW systems) "is a symlink".
        if e.errno == errno.ELOOP:
            log(
                f"[post_merge] refused to read {rel_path!r}: "
                f"final path component is a symlink"
            )
        return None
    # Pin `encoding="utf-8"` explicitly: `os.fdopen(fd, "r")` would
    # default to `locale.getpreferredencoding()` which is ASCII on
    # `LANG=C`. A CHANGELOG with emoji/accented chars would raise
    # UnicodeDecodeError → caught here as OSError/UnicodeDecodeError →
    # returns None → caller reads empty → `put_file` overwrites full
    # release history. Locale-independent utf-8 is the correct default
    # for every file we read via this path (CHANGELOG.md, VERSION,
    # pyproject.toml, package.json).
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None
