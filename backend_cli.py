"""CliBackend — personal-deployment backend wrapping `claude -p`.

PRIVATE FORK ONLY. This module lives in `seneschal-personal`, not in
the public `seneschal` repo.

The public repo ships only `ApiBackend` for TOS reasons: framing a
publicly-distributed self-hosted reviewer around a Claude Max consumer
subscription is on the wrong side of Anthropic's consumer Terms. This
fork is for personal deployment only — the operator's own
infrastructure, authenticated with the operator's own subscription,
running the operator's own code review. That shape is personal use.

When `SENESCHAL_BACKEND=cli` is set (see `backend.get_backend`), the
factory returns a `CliBackend` instead of `ApiBackend`. No
`ANTHROPIC_API_KEY` is required in that mode.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from typing import Callable, Optional


class CliBackend:
    """Backend that shells out to `claude -p --dangerously-skip-permissions`.

    Matches the invocation shape the public repo used pre-P0:
        cat <prompt_file> | claude -p --dangerously-skip-permissions \\
            --max-turns N [--append-system-prompt "<system>"]

    All dynamic text goes through shell-quoted temp files to avoid any
    quoting-injection surface, same as the old `run_claude()` helper.
    Returns the assistant's final stdout text; raises on failure with a
    sanitized error.
    """

    def __init__(
        self,
        *,
        claude_bin: str = "claude",
        logger: Callable[[str], None] = print,
    ):
        self._claude_bin = claude_bin
        self._logger = logger

    def invoke(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        max_turns: int = 25,
        max_tokens: Optional[int] = None,  # ignored — CLI has no equivalent
        timeout: int = 300,
    ) -> str:
        """Run `claude -p` with `prompt` as stdin.

        `max_tokens` is accepted for protocol compatibility with
        `ApiBackend` but has no effect — the CLI has no per-call token
        cap, only `--max-turns`.
        """
        prompt_file = _write_temp(prompt)
        cleanup = [prompt_file]

        cmd = (
            f"cat {shlex.quote(prompt_file)} | "
            f"{shlex.quote(self._claude_bin)} -p --dangerously-skip-permissions "
            f"--max-turns {int(max_turns)}"
        )

        if system_prompt:
            sys_file = _write_temp(system_prompt)
            cleanup.append(sys_file)
            cmd += f' --append-system-prompt "$(cat {shlex.quote(sys_file)})"'

        try:
            result = subprocess.run(
                ["bash", "-l", "-c", cmd],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"CliBackend: claude -p timed out after {timeout}s") from None
        except FileNotFoundError:
            raise RuntimeError(
                f"CliBackend: `{self._claude_bin}` not found on $PATH"
            ) from None
        finally:
            for f in cleanup:
                try:
                    os.unlink(f)
                except OSError:
                    pass

        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()

        if result.returncode != 0:
            # Don't surface stderr verbatim — it can contain auth detail.
            self._logger(
                f"backend_cli: claude -p exit={result.returncode} "
                f"stderr_len={len(stderr)}"
            )
            raise RuntimeError(
                f"CliBackend: claude -p returned exit code {result.returncode}"
            )

        if not stdout:
            self._logger("backend_cli: claude -p returned empty stdout")
            return ""

        return stdout


def _write_temp(content: str, suffix: str = ".txt") -> str:
    """Write content to a temp file, return path. Caller unlinks."""
    fh = tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False)
    fh.write(content)
    fh.close()
    return fh.name
