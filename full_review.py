"""Full multi-persona PR review — slash command launcher.

Invokes `/seneschal-review <pr_number>` via `claude -p` from inside the
cloned repo. The slash command (deployed by install.sh to
~/.claude/commands/seneschal-review.md on the host) spawns the reviewer
personas in parallel via the Task tool, aggregates their findings into
`.claude/plans/seneschal-review-<N>.md`, AND posts the result to GitHub
as a formal PR review via `~/bin/seneschal-post`.

This module is just a launcher — it does not read the output state
file or post anything itself. The slash command handles both. The local
manual path (`/seneschal-review N` from a Mac) and the bot path (this
launcher on OCI) converge on the same posting code path.

Used when the user wants thorough multi-perspective coverage on top of
the cheap signals from `analyzer.py` — gated by RepoConfig.full_review
or the CODE_REVIEWER_FULL_DEFAULT env var in app.py.

Persona input state file:
    .claude/plans/seneschal-personas-<N>.json  — which personas to run

    The slash command reads this before spawning Task subagents. If the
    file is missing, the slash command falls back to its own default set
    (the six builtins). The schema is:

        {
          "pr_number": 123,
          "personas": [
            {"name": "architect", "subagent_type": "seneschal-architect",
             "prompt_text": "", "source": "builtin"},
            {"name": "hipaa", "subagent_type": null,
             "prompt_text": "You are a HIPAA reviewer...",
             "source": "file:.seneschal/personas/hipaa.md"}
          ]
        }
"""

from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional

from persona_loader import Persona


def _write_persona_state(pr_number: int, repo_path: str, personas: List[Persona]) -> Path:
    """Serialize the persona list as JSON to the state file the slash command reads."""
    state_rel = f".claude/plans/seneschal-personas-{pr_number}.json"
    state_path = Path(repo_path) / state_rel
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "pr_number": int(pr_number),
                "personas": [asdict(p) for p in personas],
            },
            indent=2,
        )
    )
    return state_path


def run_full_review(
    pr_number: int,
    repo_path: str,
    *,
    personas: Optional[List[Persona]] = None,
    timeout: int = 1200,
) -> str:
    """Run /seneschal-review on the cloned repo; return a status string.

    Args:
        pr_number: the PR number to review
        repo_path: the local clone of the repo (cwd for the claude invocation)
        personas: configured personas to run. If None, the slash command
            falls back to its builtin default of all six personas.
        timeout: hard wall clock limit in seconds (default 20m)

    Returns:
        A short human-readable status string suitable for logging. The
        slash command handles posting to GitHub itself, so this caller
        only needs to know whether the launcher succeeded.

    Raises nothing — failures come back as status strings prefixed with
    `(full-review failed: ...)` so app.py can decide whether to post a
    fallback failure comment.
    """
    state_rel = f".claude/plans/seneschal-review-{pr_number}.md"
    state_path = Path(repo_path) / state_rel

    # Clear any stale OUTPUT state from a previous review of the same PR.
    try:
        state_path.unlink()
    except FileNotFoundError:
        pass

    # Write INPUT state (persona list) — slash command reads this.
    # Also clear any stale input from a previous run.
    input_path = Path(repo_path) / f".claude/plans/seneschal-personas-{pr_number}.json"
    try:
        input_path.unlink()
    except FileNotFoundError:
        pass
    if personas:
        _write_persona_state(pr_number, repo_path, personas)

    cmd = (
        f"cd {shlex.quote(repo_path)} && "
        f"claude -p '/seneschal-review {int(pr_number)}' "
        f"--dangerously-skip-permissions --max-turns 60"
    )
    try:
        result = subprocess.run(
            ["bash", "-l", "-c", cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"(full-review failed: timed out after {timeout}s)"
    except Exception as e:  # noqa: BLE001
        return f"(full-review failed: {e})"

    # The slash command's last line of stdout is its summary, e.g.
    # "seneschal-review: COMMENT · 8 finding(s) · posted https://..."
    summary = ""
    for line in (result.stdout or "").splitlines()[::-1]:
        line = line.strip()
        if line.startswith("seneschal-review:"):
            summary = line
            break

    if state_path.exists() and result.returncode == 0:
        return summary or "(full-review completed; no summary line found)"

    err = (result.stderr or "").strip()[:300]
    return (
        f"(full-review failed: rc={result.returncode}, "
        f"state_file_written={state_path.exists()}, stderr={err!r})"
    )
