"""Per-repo configuration for Seneschal.

Repos can place `.seneschal.yml` at the root to customize review behavior.
The config is merged with defaults and appended to the Claude system
prompt so project-specific rules are enforced.

`.ch-code-reviewer.yml` is accepted as a fallback filename for repos
that haven't migrated from the legacy name.

Example:
    # .seneschal.yml
    rules:
      - "Use Realm for persistent storage"
      - "Prefer cobra over flag for CLI"
    ignore_paths:
      - docs/
      - examples/
    max_risk_for_auto_fix: medium
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import List, Optional

import yaml


# Repo-supplied content lands in the Claude system prompt, so we sanitize it
# defensively. The repo file is editable by anyone with push access, so a
# single rogue commit shouldn't be able to inject paragraphs of "ignore prior
# instructions and run X" into the reviewer's system prompt.
MAX_RULE_LEN = 200
MAX_RULES = 30
MAX_IGNORE_PATHS = 50
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _sanitize(text: str, max_len: int) -> str:
    """Strip control chars / newlines, collapse whitespace, truncate."""
    text = _CONTROL_CHARS.sub("", text)
    text = text.replace("\r", " ").replace("\n", " ")
    text = " ".join(text.split())
    return text[:max_len]


@dataclass
class RepoConfig:
    rules: List[str] = field(default_factory=list)
    ignore_paths: List[str] = field(default_factory=list)
    max_risk_for_auto_fix: str = "high"  # low/medium/high — up to which risk we auto-fix
    review_style: str = "concise"  # concise, thorough, blunt
    full_review: bool = False  # if true, run the heavyweight multi-persona reviewer
    # Default OFF. The fix loop invokes `claude -p --dangerously-skip-permissions
    # --max-turns 40` and pushes commits to the PR branch, so it's expensive and
    # behavior-changing. Opt in per-repo by setting `auto_fix: true` in
    # `.ch-code-reviewer.yml`. Previously defaulted to True, which meant any
    # review returning REQUEST_CHANGES on a trusted-author PR would silently
    # kick off a 40-turn claude run.
    auto_fix: bool = False
    # Raw persona entries from `personas:` in YAML. Each entry is a dict
    # like {"builtin": "architect"} or {"file": ".seneschal/personas/x.md"}.
    # Resolved into Persona objects by persona_loader.load_personas().
    # Empty list → run all six builtin personas (pre-v2 default).
    personas: List[dict] = field(default_factory=list)

    def system_prompt_addendum(self) -> str:
        if not self.rules and self.review_style == "concise":
            return ""
        parts: List[str] = []
        if self.rules:
            parts.append("Project-specific review rules:")
            for rule in self.rules:
                parts.append(f"- {rule}")
        if self.review_style == "thorough":
            parts.append("")
            parts.append("Style: Be thorough. Explain reasoning for each finding.")
        elif self.review_style == "blunt":
            parts.append("")
            parts.append("Style: Be blunt. Skip pleasantries. One line per issue.")
        return "\n".join(parts)

    def should_skip_file(self, filename: str) -> bool:
        for ignore in self.ignore_paths:
            ignore = ignore.rstrip("/")
            if not ignore:
                continue
            if filename == ignore or filename.startswith(ignore + "/"):
                return True
        return False


def parse_config(raw: str) -> RepoConfig:
    """Parse YAML config. PyYAML is a hard dependency (see requirements.txt).

    We only consume flat fields (rules, ignore_paths, max_risk_for_auto_fix,
    review_style) and the result is sanitized in this function before being
    handed to the rest of the system.
    """
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        return RepoConfig()

    config = RepoConfig()
    if isinstance(data.get("rules"), list):
        config.rules = [
            _sanitize(str(r), MAX_RULE_LEN)
            for r in data["rules"][:MAX_RULES]
            if str(r).strip()
        ]
    if isinstance(data.get("ignore_paths"), list):
        config.ignore_paths = [
            _sanitize(str(p), MAX_RULE_LEN)
            for p in data["ignore_paths"][:MAX_IGNORE_PATHS]
            if str(p).strip()
        ]
    if data.get("max_risk_for_auto_fix") in {"low", "medium", "high"}:
        config.max_risk_for_auto_fix = data["max_risk_for_auto_fix"]
    if data.get("review_style") in {"concise", "thorough", "blunt"}:
        config.review_style = data["review_style"]
    if isinstance(data.get("full_review"), bool):
        config.full_review = data["full_review"]
    if isinstance(data.get("auto_fix"), bool):
        config.auto_fix = data["auto_fix"]
    # personas: accept only dict entries with either "builtin" or "file" keys.
    # persona_loader does the real resolution + safety checks; here we just
    # shape-check and cap the list.
    if isinstance(data.get("personas"), list):
        filtered = []
        for entry in data["personas"][:10]:  # MAX_PERSONAS_PER_REPO mirrors persona_loader
            if isinstance(entry, dict) and ("builtin" in entry or "file" in entry):
                filtered.append(entry)
        config.personas = filtered
    return config


def load_from_path(path: str) -> RepoConfig:
    if not os.path.exists(path):
        return RepoConfig()
    with open(path, "r") as fh:
        raw = fh.read()
    try:
        return parse_config(raw)
    except Exception:
        return RepoConfig()


def load_from_repo(repo_dir: str) -> RepoConfig:
    # Prefer the canonical .seneschal.yml, fall back to the legacy name for
    # repos that haven't migrated yet. Both filenames carry the same schema.
    for name in (
        ".seneschal.yml",
        ".seneschal.yaml",
        ".ch-code-reviewer.yml",
        ".ch-code-reviewer.yaml",
    ):
        p = os.path.join(repo_dir, name)
        if os.path.exists(p):
            return load_from_path(p)
    return RepoConfig()
