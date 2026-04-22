"""Grep manifest files across every known repo for a package reference.

This is the "if this package has a CVE, which of my repos use it?" tool.
We keep it dumb-text-grep (no manifest parsers) so it's uniform across
six ecosystems and has no dependencies beyond stdlib.

Supported manifests (per-repo, at the repo root):
  - package.json           → JS / TS dependencies + devDependencies
  - requirements.txt       → Python pip
  - pyproject.toml         → Python (PEP 621 [project.dependencies],
                              [tool.poetry.dependencies], and plain
                              `name = "version"` style)
  - go.mod                 → Go modules in `require (...)` blocks
  - Package.swift          → Swift `.package(url: "...")` entries
  - Cargo.toml             → Rust `[dependencies]` sections

We match `package_name` as a substring on each manifest line. That's
intentionally loose — the caller asks "where do I use `axios`?" and we
want to catch `"axios": "1.0"`, `axios==1.0`, `require github.com/axios`,
etc. without per-ecosystem parsing. False positives are rare because
manifest lines are short and package names are specific.

Results are capped at `limit` total hits (not per-repo) so the MCP tool
response stays bounded. Repos enumerated via `cross_repo.known_repos`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

import cross_repo
from fs_safety import safe_open_in_repo
from log import log as _neutral_log


# Per-repo manifests to probe. Keeping this tuple explicit makes it easy
# to audit at a glance which ecosystems we cover.
_MANIFEST_FILES = (
    "package.json",
    "requirements.txt",
    "pyproject.toml",
    "go.mod",
    "Package.swift",
    "Cargo.toml",
)

# Cap per-manifest file size to avoid pathological reads. A manifest
# larger than this is either not a real manifest or is a generated
# artifact (yarn.lock, not in our list anyway).
_MAX_MANIFEST_BYTES = 1_048_576  # 1 MB


@dataclass(frozen=True)
class Hit:
    """One line in a manifest that referenced the queried package."""

    repo: str       # "owner/name" slug
    path: str       # relative path inside the repo (e.g. "package.json")
    line: int       # 1-based line number in the manifest
    matched: str    # the raw matched line, stripped


def _log(msg: str) -> None:
    """Prefixed wrapper around the neutral stderr logger."""
    _neutral_log(f"[dependency_grep] {msg}")


def _read_manifest(repo_path: str, manifest_name: str) -> Optional[str]:
    """Read a manifest file from `repo_path`, capped at _MAX_MANIFEST_BYTES.

    Routes through `fs_safety.safe_open_in_repo` so a malicious repo that
    commits `package.json` as a symlink to `~/seneschal/ch-code-reviewer.pem`
    (or `/etc/passwd`, or any host path) returns None instead of reading
    the symlink target. Without this guard, a caller asking for a short
    prefix of the PEM via `seneschal_dependency_usage("-----BEGIN")`
    would exfiltrate that content through the MCP tool response.

    Size-capped BEFORE content read: we stat the candidate path first so
    a pathological manifest (generated lockfile, test fixture) doesn't
    hit the full-file read.
    """
    abs_path = os.path.join(repo_path, manifest_name)
    try:
        st = os.stat(abs_path)
    except OSError:
        return None
    if st.st_size > _MAX_MANIFEST_BYTES:
        return None
    return safe_open_in_repo(repo_path, manifest_name)


def scan_all(
    package_name: str,
    root: Optional[str] = None,
    limit: int = 50,
) -> List[Hit]:
    """Return every manifest line across every known repo that mentions
    `package_name`.

    Args:
      package_name: substring to match (case-sensitive — package names
        are generally case-sensitive in these ecosystems).
      root: override `SENESCHAL_REPOS_ROOT`. Passed through to
        `cross_repo.known_repos`.
      limit: hard cap on total hits returned. Keeps MCP responses
        bounded even when a common package shows up everywhere.
    """
    if not package_name or not package_name.strip():
        return []
    limit = max(1, int(limit))

    repos = cross_repo.known_repos(root=root)
    out: List[Hit] = []
    for kr in repos:
        if len(out) >= limit:
            break
        for manifest_name in _MANIFEST_FILES:
            if len(out) >= limit:
                break
            abs_path = os.path.join(kr.path, manifest_name)
            if not os.path.isfile(abs_path):
                continue
            text = _read_manifest(kr.path, manifest_name)
            if text is None:
                continue
            for lineno, raw in enumerate(text.splitlines(), start=1):
                if package_name in raw:
                    out.append(
                        Hit(
                            repo=kr.slug,
                            path=manifest_name,
                            line=lineno,
                            matched=raw.strip()[:200],
                        )
                    )
                    if len(out) >= limit:
                        break
    return out
