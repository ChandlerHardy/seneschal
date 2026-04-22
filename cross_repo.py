"""Enumerate local git repos under SENESCHAL_REPOS_ROOT.

Seneschal's MCP tools need to know which repos the operator keeps on
disk so we can:

  - Aggregate reviews across repos in the SQLite index
  - Discover ADRs across every repo the operator owns
  - Query GitHub for followup issues, iterating over a known slug list
    instead of hitting the full `/user/repos` endpoint
  - Grep for a dependency across every manifest

We never shell out to `git` (process-cost per repo adds up for a dev
whose `~/repos/` has 50+ checkouts). Instead we open each
`<repo>/.git/config` and pattern-match the `[remote "origin"]` URL.

Only GitHub origins are returned — every other MCP tool here is
GitHub-native (installation tokens, issues API, etc.), so non-GitHub
remotes would produce confusing 404s. GitLab / Bitbucket repos under
the same root are silently skipped, not errored on.

Cache lifetime: process-local. The MCP server is a long-running stdio
process; if the operator clones a new repo they can restart the MCP
server to pick it up. This is documented in docs/mcp-server.md.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Optional

from log import log as _neutral_log


# Capture GitHub origins in both forms seen in `.git/config`:
#   url = git@github.com:owner/repo.git       (SSH)
#   url = https://github.com/owner/repo.git   (HTTPS)
#   url = https://user:pat@github.com/owner/repo   (credentialed HTTPS, no .git)
#
# `[:/]` separator handles both, trailing `.git` optional (some orgs
# check out without it), `\r?` absorbs Windows-style line endings.
_GITHUB_URL_RE = re.compile(
    r"github\.com[:/]([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+?)(?:\.git)?\s*$",
    re.MULTILINE,
)

# In-process cache keyed by the resolved root path. Module-level dict so
# all callers share state — the MCP server is single-process; multiple
# tool calls in one session hit the cache on reuse.
_CACHE: dict = {}


@dataclass(frozen=True)
class KnownRepo:
    """A local git checkout with a GitHub origin URL."""

    slug: str     # "owner/repo"
    path: str     # absolute path to the working tree


def _log(msg: str) -> None:
    """Prefixed wrapper around the neutral stderr logger."""
    _neutral_log(f"[cross_repo] {msg}")


def _clear_cache() -> None:
    """Drop the per-root cache. Exposed for tests; production callers
    restart the MCP server if they want a fresh enumeration."""
    _CACHE.clear()


def _resolve_root(root: Optional[str] = None) -> str:
    if root is None:
        root = os.environ.get(
            "SENESCHAL_REPOS_ROOT", os.path.expanduser("~/repos")
        )
    return os.path.abspath(os.path.expanduser(root))


def _parse_origin(config_text: str) -> Optional[tuple]:
    """Pull the first GitHub (owner, repo) pair out of a `.git/config`.

    We don't use configparser because git's config is close to INI but
    not quite — section lines like `[remote "origin"]` trip stricter
    parsers. Our needs are narrow (one regex is enough)."""
    m = _GITHUB_URL_RE.search(config_text)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    # Strip any trailing `.git` that the lazy regex left behind
    # (the non-greedy + optional group handles most cases already).
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not owner or not repo:
        return None
    return (owner, repo)


def known_repos(root: Optional[str] = None) -> List[KnownRepo]:
    """Enumerate GitHub-origin repos under `root`.

    Resolution order:
      1. Explicit arg.
      2. `SENESCHAL_REPOS_ROOT` env var.
      3. `~/repos`.

    Results are cached per resolved root for the lifetime of the process.
    Call `_clear_cache()` to force a rescan (tests only; production
    restarts the MCP server).

    Does not follow symlinks out of `root` — an operator's `~/repos`
    with a symlinked checkout is fine, but a symlink pointing at
    `/` shouldn't cause us to walk the whole filesystem.
    """
    abs_root = _resolve_root(root)
    if abs_root in _CACHE:
        return list(_CACHE[abs_root])

    if not os.path.isdir(abs_root):
        _CACHE[abs_root] = []
        return []

    # Resolve the root so we can compare realpaths of children — a
    # symlink out of the tree won't match this prefix.
    root_real = os.path.realpath(abs_root)

    out: List[KnownRepo] = []
    try:
        entries = sorted(os.listdir(abs_root))
    except OSError as e:
        _log(f"listdir failed on {abs_root}: {e}")
        _CACHE[abs_root] = []
        return []

    for name in entries:
        p = os.path.join(abs_root, name)
        # Skip symlinks that escape the root. `os.path.realpath` resolves
        # the entire chain; if the child doesn't live under root_real,
        # we refuse to walk it (path-traversal defense for enumeration).
        try:
            real_child = os.path.realpath(p)
        except OSError:
            continue
        if not (real_child == root_real or real_child.startswith(root_real + os.sep)):
            continue
        if not os.path.isdir(p):
            continue
        git_config = os.path.join(p, ".git", "config")
        if not os.path.isfile(git_config):
            continue
        try:
            with open(git_config, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as e:
            _log(f"failed to read {git_config}: {e}")
            continue
        parsed = _parse_origin(text)
        if parsed is None:
            continue
        owner, repo = parsed
        out.append(KnownRepo(slug=f"{owner}/{repo}", path=p))

    _CACHE[abs_root] = out
    return list(out)
