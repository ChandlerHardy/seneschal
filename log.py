"""Neutral stderr logger shared by modules that must not pull in Flask.

Several modules (`fs_safety`, `review_index`, `cross_repo`, `dependency_grep`,
`seneschal_token`, `mcp_server.server`) used to carry copy-pasted stderr
loggers or deferred `from app import log` imports to avoid dragging Flask
into the MCP process. This module consolidates that into one tiny helper.

Call pattern:
    from log import log
    log("[module] something happened")

Behavior:
  - Writes to stderr with a trailing newline and flushes.
  - Silently swallows OSError so a write failure doesn't crash the caller.
  - Deliberately stdlib-only and Flask-free.
"""

from __future__ import annotations

import sys


def log(msg: str) -> None:
    """Write `msg` to stderr. Never raises."""
    try:
        sys.stderr.write(f"{msg}\n")
        sys.stderr.flush()
    except OSError:
        pass
