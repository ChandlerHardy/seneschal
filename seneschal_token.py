#!/usr/bin/env python3
"""Mint a Seneschal GitHub App installation token for a given repo.

This module exposes two surfaces:

  - Programmatic: `mint_installation_token(owner_or_slug) -> str`. Called
    from the MCP server (and any other in-process caller) to obtain a
    short-lived installation token without shelling out. Uses an in-process
    TTL cache so a single Python process doesn't hit GitHub's `/app/
    installations` endpoint on every tool call.

  - CLI: `python3 seneschal_token.py <owner/repo>`. Thin shell over the
    programmatic API. Prints the token to stdout on success; exits non-zero
    with a short stderr message on failure.

Usage:
    python3 seneschal_token.py <owner/repo>

Exit codes (CLI):
    0 — token minted and printed
    1 — usage error, bad PEM, network/HTTP failure (TokenMintError)
    2 — App not installed on the target repo (AppNotInstalledError)

Intended for shell pipelines:
    GH_TOKEN=$(~/seneschal/venv/bin/python ~/seneschal/seneschal_token.py \\
                  ChandlerHardy/foo) \\
      gh issue create --repo ChandlerHardy/foo ...

This helper exists so automated jobs can file issues under Seneschal[bot]
instead of Chandler's personal account. If the App isn't installed on a
given repo the helper exits 2 so the caller can fall back to the user's
normal gh auth.

Env overrides:
  SENESCHAL_APP_ID       — numeric App ID (default: 3127694)
  SENESCHAL_PEM_PATH     — path to App private key PEM (default: ~/seneschal/ch-code-reviewer.pem)
  SENESCHAL_GITHUB_TOKEN — if set, `mint_installation_token` returns this
                           value verbatim (for local/dev use when the App
                           isn't installed; a PAT with the same scopes is
                           a valid drop-in).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Tuple

import jwt
import requests

from fs_safety import validate_repo_slug
from log import log as _neutral_log


# Public App ID for the `seneschal-cr` GitHub App. `github_api.APP_ID`
# is the single source of truth; this module's env-override helper
# (`_get_app_id`) reads from that when SENESCHAL_APP_ID isn't set.
# The import is deferred inside `_get_app_id` so the MCP server (which
# imports seneschal_token) doesn't need to load github_api's Flask
# dependencies just to mint tokens.
_DEFAULT_PEM_PATH = os.path.expanduser("~/seneschal/ch-code-reviewer.pem")

# GitHub installation tokens expire at 60 min. Cache for 50 min to leave
# a 10-min safety margin for in-flight requests that hold a reference.
_TOKEN_TTL_SECONDS = 50 * 60

# In-process cache: slug -> (token, expires_at_epoch_seconds). Module-level
# dict is intentional — the MCP server is a single process and we want every
# tool invocation in that session to share the cache.
_CACHE: dict = {}


class TokenMintError(RuntimeError):
    """Raised when the token-minting pipeline fails for a non-404 reason
    (PEM missing, network failure, non-404 HTTP error). Distinct from
    AppNotInstalledError so callers can distinguish "should fall back to
    PAT" (this) from "should skip the repo" (AppNotInstalledError)."""


class AppNotInstalledError(RuntimeError):
    """Raised when GitHub returns 404 on the installation lookup for the
    given owner/repo. Callers should skip that repo rather than surface
    the error — the App just isn't installed there."""

    def __init__(self, slug: str):
        super().__init__(f"Seneschal App not installed on {slug}")
        self.slug = slug


def _log(msg: str) -> None:
    """Prefixed wrapper around the neutral stderr logger."""
    _neutral_log(f"[seneschal_token] {msg}")


def _clear_cache() -> None:
    """Reset the in-process token cache. Tests only."""
    _CACHE.clear()


def _get_app_id() -> int:
    """Resolve the App ID from env, falling back to github_api.APP_ID.

    `github_api.APP_ID` is the single source of truth for the default
    Seneschal App ID. This function lets operators override with
    `SENESCHAL_APP_ID` (for forks, test instances, or staging) without
    patching code. Deferred import of github_api to keep this module
    safe to import before github_api's heavier deps load.
    """
    # Deferred import: `github_api` carries requests/urllib3 and the
    # PR-analysis modules. The MCP server imports `seneschal_token`
    # eagerly at module-load time; deferring this keeps the import
    # surface minimal until a token actually gets minted.
    from github_api import APP_ID as _GITHUB_API_APP_ID
    default_app_id = _GITHUB_API_APP_ID

    raw = os.environ.get("SENESCHAL_APP_ID")
    if not raw:
        return default_app_id
    try:
        return int(raw)
    except (TypeError, ValueError):
        _log(f"ignoring invalid SENESCHAL_APP_ID={raw!r}; using default {default_app_id}")
        return default_app_id


# Backward-compat alias — kept because `test_app_id_falls_back_to_default`
# asserts equivalence to `_DEFAULT_APP_ID`. The constant is resolved at
# import time from `github_api.APP_ID` so both names point at the same
# value and tests don't have to juggle two.
try:
    from github_api import APP_ID as _DEFAULT_APP_ID  # noqa: E402
except ImportError:  # pragma: no cover — pre-existing env where app is absent
    _DEFAULT_APP_ID = 3127694


def _get_pem_path() -> str:
    return os.environ.get("SENESCHAL_PEM_PATH", _DEFAULT_PEM_PATH)


def _generate_jwt() -> str:
    """Sign a 9-minute JWT using the GitHub App's PEM."""
    pem_path = _get_pem_path()
    try:
        pem = Path(pem_path).read_text()
    except FileNotFoundError as e:
        raise TokenMintError(f"PEM not found at {pem_path}") from e
    except OSError as e:
        raise TokenMintError(f"could not read PEM at {pem_path}: {e}") from e
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 540, "iss": str(_get_app_id())}
    return jwt.encode(payload, pem, algorithm="RS256")


def _installation_id_for_repo(jwt_token: str, owner: str, repo: str) -> int:
    """Look up the installation ID for the App's install on this repo."""
    resp = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/installation",
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=10,
    )
    if resp.status_code == 404:
        raise AppNotInstalledError(f"{owner}/{repo}")
    resp.raise_for_status()
    return int(resp.json()["id"])


def _installation_token(jwt_token: str, installation_id: int) -> str:
    resp = requests.post(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["token"]


def _parse_slug(slug: str) -> Tuple[str, str]:
    """Validate + split an `owner/repo` slug. Raises ValueError otherwise.

    Delegates to `fs_safety.validate_repo_slug` as the single source of
    truth for what counts as a safe slug — the earlier bespoke check
    accepted characters (`?`, `#`, `&`, spaces, newlines) that would
    then interpolate unescaped into the GitHub API URL at
    `/repos/{owner}/{repo}/installation`, opening a URL-injection path.
    """
    if slug is None:
        raise ValueError("slug must be in owner/repo form, got None")
    validate_repo_slug(slug)
    owner, _, repo = slug.partition("/")
    return owner, repo


def mint_installation_token(owner_or_slug: str) -> str:
    """Return a valid installation token for `owner_or_slug`.

    Behavior:
      1. If `SENESCHAL_GITHUB_TOKEN` is set, return that verbatim. Useful
         for local dev where the App isn't installed; a PAT with the same
         scopes is a drop-in.
      2. Check the in-process cache; return if fresh (< 50 min old).
      3. Otherwise, sign a JWT with the App PEM, look up the installation
         ID via /repos/<slug>/installation, exchange for an installation
         token, cache, and return.

    Args:
      owner_or_slug: `owner/repo` slug. (Org-wide tokens via just `owner`
        are not supported; GitHub requires an installation ID bound to a
        specific install, and the install can span the whole org — we
        still look it up via an example repo in that org.)

    Raises:
      ValueError: malformed slug.
      AppNotInstalledError: /repos/<slug>/installation returned 404.
      TokenMintError: any other failure (PEM missing, network error, 500
        from GitHub, etc.).
    """
    # Validate the slug FIRST — before the PAT short-circuit — so a
    # malformed slug can never reach the downstream GitHub API call,
    # whether we're minting via App JWT or handing back a PAT. Previously
    # the PAT path returned the token before any validation ran, so a
    # caller with `SENESCHAL_GITHUB_TOKEN` set and any arbitrary string
    # (including URL-injection payloads) got back a valid token and the
    # caller's downstream code could then interpolate that untrusted
    # slug into a request URL.
    owner, repo = _parse_slug(owner_or_slug)
    slug = f"{owner}/{repo}"

    # PAT override — one env var beats the whole App dance.
    pat = os.environ.get("SENESCHAL_GITHUB_TOKEN")
    if pat:
        return pat

    # Cache hit?
    cached = _CACHE.get(slug)
    if cached is not None:
        token, expires_at = cached
        if expires_at > time.time():
            return token
        # Expired — fall through to a fresh mint.
        _CACHE.pop(slug, None)

    try:
        jwt_token = _generate_jwt()
        inst_id = _installation_id_for_repo(jwt_token, owner, repo)
        token = _installation_token(jwt_token, inst_id)
    except AppNotInstalledError:
        raise
    except requests.HTTPError as e:
        raise TokenMintError(f"GitHub API error for {slug}: {e}") from e
    except requests.RequestException as e:
        raise TokenMintError(f"network error for {slug}: {e}") from e

    _CACHE[slug] = (token, time.time() + _TOKEN_TTL_SECONDS)
    return token


def _die(msg: str, code: int = 1) -> None:
    print(f"seneschal_token: {msg}", file=sys.stderr)
    sys.exit(code)


def main(argv: list) -> int:
    """CLI entry point. Thin wrapper over `mint_installation_token` with
    stderr + exit-code mapping preserved from the pre-refactor script."""
    if len(argv) != 2:
        _die("usage: seneschal_token.py <owner/repo>")
    target = argv[1]
    try:
        token = mint_installation_token(target)
    except ValueError as e:
        _die(str(e), code=1)
    except AppNotInstalledError as e:
        _die(str(e), code=2)
    except TokenMintError as e:
        _die(str(e), code=1)
    print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
