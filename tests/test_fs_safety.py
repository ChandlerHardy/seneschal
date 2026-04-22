"""Tests for fs_safety path-safety primitives.

These complement the coverage already in
`test_post_merge_orchestrator.py` (symlink traversal, intermediate-
component lstat) by exercising the encoding + locale concerns that
land on the `safe_open_in_repo` read path.

The round-3 pre-P1 refactor moved `safe_open_in_repo` into this
module. `os.fdopen(fd, "r")` had no `encoding=` kwarg, so on
`LANG=C` any non-ASCII byte in CHANGELOG.md would raise
UnicodeDecodeError → caught as OSError → None → caller rebuilds a
blank changelog → put_file overwrites full release history.
"""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fs_safety import safe_open_in_repo  # noqa: E402


# --------------------------------------------------------------------------
# Encoding: reads must pin utf-8 regardless of process locale
# --------------------------------------------------------------------------


def test_safe_open_in_repo_reads_utf8_content(tmp_path):
    """A CHANGELOG.md with emoji + accented chars must round-trip."""
    repo = tmp_path / "repo"
    repo.mkdir()
    content = "## [Unreleased]\n- fix: café ☕ — resolved\n"
    (repo / "CHANGELOG.md").write_text(content, encoding="utf-8")

    with patch("app.log"):
        out = safe_open_in_repo(str(repo), "CHANGELOG.md")

    assert out == content, (
        "safe_open_in_repo dropped non-ASCII content — missing encoding="
        "utf-8 on os.fdopen lets the process locale govern decoding."
    )


def test_safe_open_in_repo_pins_utf8_on_fdopen(tmp_path, monkeypatch):
    """Round-3 Blocker fingerprint: `os.fdopen(fd, "r")` without
    `encoding=` uses `locale.getpreferredencoding()` → ASCII on
    `LANG=C`. Any non-ASCII byte raises UnicodeDecodeError → caught as
    OSError → returns None → orchestrator sees empty changelog →
    `put_file` wipes release history.

    Directly inspect the call to `os.fdopen` to verify `encoding="utf-8"`
    is passed, which is locale-independent. Env-var mocks don't reliably
    change CPython's already-initialized locale at test time; a direct
    call-args check is the stable regression signal.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    content = "## [Unreleased]\n- feat: résumé emoji 🎉\n"
    (repo / "CHANGELOG.md").write_text(content, encoding="utf-8")

    captured_kwargs = {}
    real_fdopen = os.fdopen

    def _spy_fdopen(fd, mode="r", *args, **kwargs):
        captured_kwargs.setdefault("mode", mode)
        captured_kwargs.setdefault("encoding", kwargs.get("encoding"))
        return real_fdopen(fd, mode, *args, **kwargs)

    monkeypatch.setattr("fs_safety.os.fdopen", _spy_fdopen)
    with patch("app.log"):
        out = safe_open_in_repo(str(repo), "CHANGELOG.md")

    assert out == content
    assert captured_kwargs.get("encoding") == "utf-8", (
        "Round-3 Blocker regression: safe_open_in_repo did not pin "
        "encoding='utf-8' on os.fdopen. Under LANG=C this would crash "
        "on non-ASCII bytes and return None, wiping the changelog."
    )


def test_safe_open_in_repo_returns_none_on_invalid_utf8(tmp_path):
    """If a file isn't valid utf-8, the read should fail closed (None)
    rather than return a mojibake string. This keeps the fallback
    semantics that callers (`_read_local_changelog`) already handle."""
    repo = tmp_path / "repo"
    repo.mkdir()
    # Write raw bytes that aren't a valid utf-8 sequence (\xff).
    (repo / "CHANGELOG.md").write_bytes(b"prefix \xff\xfe bad bytes\n")

    with patch("app.log"):
        out = safe_open_in_repo(str(repo), "CHANGELOG.md")

    assert out is None, (
        "Invalid utf-8 should return None (caller falls back to empty), "
        "not silently produce a mojibake string we'd write back via put_file."
    )
