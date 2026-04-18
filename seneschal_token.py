#!/usr/bin/env python3
"""Mint a Seneschal GitHub App installation token for a given repo.

Usage:
    python3 seneschal_token.py <owner/repo>

Prints the installation token to stdout on success. Exits non-zero with a
short error to stderr on failure (App not installed on the repo, missing
PEM, network error). Intended for use in shell pipelines:

    GH_TOKEN=$(~/seneschal/venv/bin/python ~/seneschal/seneschal_token.py \\
                  ChandlerHardy/foo) \\
      gh issue create --repo ChandlerHardy/foo ...

This helper exists so the heartbeat cron can file issues under
Seneschal[bot] instead of Chandler's personal account, creating a clear
separation between the operator and the automation. If the App isn't
installed on a given repo the helper exits non-zero so the caller can
fall back to the user's normal gh auth.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import jwt
import requests


APP_ID = 3127694
PEM_PATH = os.path.expanduser("~/seneschal/ch-code-reviewer.pem")


def _die(msg: str, code: int = 1) -> None:
    print(f"seneschal_token: {msg}", file=sys.stderr)
    sys.exit(code)


def _generate_jwt() -> str:
    try:
        pem = Path(PEM_PATH).read_text()
    except FileNotFoundError:
        _die(f"PEM not found at {PEM_PATH}")
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 540, "iss": str(APP_ID)}
    return jwt.encode(payload, pem, algorithm="RS256")


def _installation_id_for_repo(jwt_token: str, owner: str, repo: str) -> int:
    resp = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/installation",
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=10,
    )
    if resp.status_code == 404:
        _die(f"App not installed on {owner}/{repo}", code=2)
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


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        _die("usage: seneschal_token.py <owner/repo>")
    target = argv[1]
    if "/" not in target:
        _die("repo must be in owner/repo form")
    owner, repo = target.split("/", 1)

    jwt_token = _generate_jwt()
    try:
        inst_id = _installation_id_for_repo(jwt_token, owner, repo)
        token = _installation_token(jwt_token, inst_id)
    except requests.HTTPError as e:
        _die(f"GitHub API error: {e}")
    except requests.RequestException as e:
        _die(f"network error: {e}")

    print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
