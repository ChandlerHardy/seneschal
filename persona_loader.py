"""Load review personas from repo config.

A "persona" is a review perspective applied during full multi-persona
review. Seneschal ships six built-in personas (architect, security,
simplifier, design, data-integrity, edge-case) as Claude Code subagent
definitions — those get deployed to `~/.claude/agents/seneschal-*.md`
by install.sh and are spawned by subagent_type.

Users can also define their own personas in `.seneschal/personas/<name>.md`
within their target repo and reference them from `.seneschal.yml`:

    personas:
      - builtin: architect
      - builtin: security
      - file: .seneschal/personas/hipaa.md

File-based personas are simpler — just free-form prompt text, no
frontmatter required. They're spawned via the generic `general-purpose`
subagent with their content as the Task prompt.

Fallback: when no `personas:` is configured, all six builtins run
(matches the pre-v2 default behavior).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Optional

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# Defensive caps (both anti prompt-injection and prompt-budget guards).
MAX_PERSONA_PROMPT_LEN = 5000
MAX_PERSONAS_PER_REPO = 10

# Built-in persona short names → the Claude Code subagent_type they resolve to.
# Ship these as `agents/seneschal-*.md` and deploy via install.sh.
BUILTIN_PERSONAS = {
    "architect": "seneschal-architect",
    "security": "seneschal-security",
    "simplifier": "seneschal-simplifier",
    "design": "seneschal-design",
    "data-integrity": "seneschal-data-integrity",
    "edge-case": "seneschal-edge-case",
}


@dataclass(frozen=True)
class Persona:
    """A resolved review persona ready to spawn as a Task subagent.

    Either:
    - `subagent_type` is set (builtin) and `prompt_text` is empty, OR
    - `subagent_type` is None (file-based) and `prompt_text` holds the
      reviewer's instructions to pass into a general-purpose subagent.
    """

    name: str
    subagent_type: Optional[str]
    prompt_text: str
    source: str  # "builtin" or "file:<path>"


def _sanitize_prompt(text: str) -> str:
    """Strip control chars, cap length. Paragraph structure is preserved
    (we keep newlines, unlike the rule sanitizer which collapses them)."""
    text = _CONTROL_CHARS.sub("", text)
    return text[:MAX_PERSONA_PROMPT_LEN]


def default_personas() -> List[Persona]:
    """All six builtins — the fallback when `personas:` is unset."""
    return [
        Persona(
            name=name,
            subagent_type=subagent,
            prompt_text="",
            source="builtin",
        )
        for name, subagent in BUILTIN_PERSONAS.items()
    ]


def _load_builtin(name: str) -> Optional[Persona]:
    name = name.strip().lower()
    subagent = BUILTIN_PERSONAS.get(name)
    if subagent is None:
        return None
    return Persona(
        name=name, subagent_type=subagent, prompt_text="", source="builtin"
    )


def _load_file(rel_path: str, repo_root: str) -> Optional[Persona]:
    """Load a file-based persona from inside the target repo.

    Rejects path traversal (anything resolving outside repo_root). Returns
    None on any I/O error or missing file — the caller will fall back to
    defaults if every entry fails.
    """
    abs_repo = os.path.realpath(repo_root)
    abs_path = os.path.realpath(os.path.join(abs_repo, rel_path))
    # Must live inside repo_root
    if not (abs_path == abs_repo or abs_path.startswith(abs_repo + os.sep)):
        return None
    if not os.path.isfile(abs_path):
        return None
    try:
        with open(abs_path, "r") as fh:
            content = fh.read()
    except OSError:
        return None
    # Reject empty / whitespace-only files
    if not content.strip():
        return None
    name = os.path.splitext(os.path.basename(abs_path))[0]
    return Persona(
        name=name,
        subagent_type=None,
        prompt_text=_sanitize_prompt(content),
        source=f"file:{rel_path}",
    )


def load_personas(personas_config: list, repo_root: str) -> List[Persona]:
    """Resolve `personas:` config into ready-to-spawn Persona objects.

    Args:
        personas_config: raw list from `.seneschal.yml`. Each item should
            be a dict with either {"builtin": "<name>"} or {"file": "<path>"}.
            Any other shape is silently skipped.
        repo_root: absolute path to the target repo (for resolving file
            refs + path-traversal protection).

    Returns:
        List of Persona. If `personas_config` is empty, malformed, or every
        entry fails to resolve, returns the six builtins so the bot never
        silently runs zero reviewers.
    """
    if not personas_config:
        return default_personas()

    personas: List[Persona] = []
    for entry in personas_config[:MAX_PERSONAS_PER_REPO]:
        if not isinstance(entry, dict):
            continue
        if "builtin" in entry:
            p = _load_builtin(str(entry["builtin"]))
            if p is not None:
                personas.append(p)
        elif "file" in entry:
            p = _load_file(str(entry["file"]), repo_root)
            if p is not None:
                personas.append(p)

    if not personas:
        # All entries failed to resolve — safer to run defaults than skip review.
        return default_personas()
    return personas
