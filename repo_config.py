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
import sys as _sys
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


# `changelog_path` flows to `app.put_file(path=...)`. A rogue
# `.seneschal.yml` committing `changelog_path: ../.github/workflows/x.yml`
# would let anyone with push access redirect Seneschal's auto-commit at
# a protected workflow file. Keep it confined to repo-relative, forward
# slashes only, no traversal segments.
def _safe_changelog_path(path: str) -> Optional[str]:
    """Return `path` if it's a safe repo-relative file path, else None.

    Rejects:
      - absolute paths (`/foo`, `C:\\foo`)
      - backslashes (Windows-style, banned outright)
      - `..` segments (parent-dir traversal)
      - empty / whitespace-only strings
    """
    if not path or not path.strip():
        return None
    if "\\" in path:
        return None
    if path.startswith("/"):
        return None
    norm = os.path.normpath(path)
    if norm.startswith("/") or norm == "..":
        return None
    # Split on POSIX `/` so we catch `../foo` even after normpath
    # normalizes it to `../foo` rather than collapsing it.
    for part in norm.split("/"):
        if part == "..":
            return None
    return norm


# `release_base_branch` lands in the `base` field of a create-pull-request
# call. A value like `main?admin=1` could be used to tack query params onto
# the downstream GitHub API URL in the rare future where we interpolate it
# into a path (e.g. `git/ref/heads/<branch>`). Validate against git's own
# ref-name rules (the strict subset we need).
_BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9_\-./]+$")


def _safe_branch_name(name: str) -> Optional[str]:
    """Return `name` if it looks like a valid git branch name, else None.

    Applies git's ref-name rules (the strict subset we need):
      - `[A-Za-z0-9_\\-./]+` only
      - max 100 chars
      - no leading/trailing slash or dot
      - no consecutive dots (`..`)
    """
    if not name or not name.strip():
        return None
    if len(name) > 100:
        return None
    if not _BRANCH_NAME_RE.match(name):
        return None
    if name.startswith("/") or name.endswith("/"):
        return None
    if name.startswith(".") or name.endswith("."):
        return None
    if ".." in name:
        return None
    return name


@dataclass
class PostMergeConfig:
    """Per-repo knobs for post-merge stewardship (P1).

    Defaults are conservative: changelog and followups stay OFF unless the
    repo opts in via `.seneschal.yml`. The orchestrator no-ops on a default
    config so installing P1 doesn't change behavior for repos that haven't
    asked for it.
    """

    changelog: bool = False
    changelog_path: str = "CHANGELOG.md"
    release_base_branch: str = "main"
    # "" (off), "patch", "minor", "major" — the lowest bump kind that
    # should trigger a release-PR. "minor" = open a release PR once the
    # accumulated unreleased bump is minor or higher.
    release_threshold: str = ""
    release_pr_draft: bool = True
    followups: bool = False
    followup_label: str = "seneschal-followup"


@dataclass
class RepoConfig:
    rules: List[str] = field(default_factory=list)
    ignore_paths: List[str] = field(default_factory=list)
    max_risk_for_auto_fix: str = "high"  # low/medium/high — retained for config backward-compat
    review_style: str = "concise"  # concise, thorough, blunt
    full_review: bool = False  # if true, run the heavyweight multi-persona reviewer
    # Retained as a parsed field so existing `.seneschal.yml` files with
    # `auto_fix: true` continue to load without error. No code in the public
    # repo consumes this value — the auto-fix loop required an agentic
    # backend that is not shipped here.
    auto_fix: bool = False
    # Raw persona entries from `personas:` in YAML. Each entry is a dict
    # like {"builtin": "architect"} or {"file": ".seneschal/personas/x.md"}.
    # Resolved into Persona objects by persona_loader.load_personas().
    # Empty list → run all six builtin personas (pre-v2 default).
    personas: List[dict] = field(default_factory=list)
    # Post-merge stewardship config (P1). All sub-knobs default OFF.
    post_merge: "PostMergeConfig" = field(default_factory=lambda: PostMergeConfig())

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

    # post_merge: nested dict of stewardship knobs. Same defensive parsing
    # as the top-level fields — unknown keys are dropped, invalid values
    # fall back to the dataclass default.
    pm_raw = data.get("post_merge")
    if isinstance(pm_raw, dict):
        pm = PostMergeConfig()
        if isinstance(pm_raw.get("changelog"), bool):
            pm.changelog = pm_raw["changelog"]
        if isinstance(pm_raw.get("changelog_path"), str) and pm_raw["changelog_path"].strip():
            candidate = _sanitize(pm_raw["changelog_path"], 200)
            safe = _safe_changelog_path(candidate)
            if safe is not None:
                pm.changelog_path = safe
            else:
                # Reject + fall back to default. `print` rather than `log`
                # because repo_config is imported from contexts (the MCP
                # server) that don't wire `app.log`.
                print(
                    f"[seneschal] rejecting unsafe changelog_path {pm_raw['changelog_path']!r}; "
                    f"falling back to default {pm.changelog_path!r}",
                    file=_sys.stderr,
                )
        if isinstance(pm_raw.get("release_base_branch"), str) and pm_raw["release_base_branch"].strip():
            candidate = _sanitize(pm_raw["release_base_branch"], 100)
            safe = _safe_branch_name(candidate)
            if safe is not None:
                pm.release_base_branch = safe
            else:
                print(
                    f"[seneschal] rejecting unsafe release_base_branch "
                    f"{pm_raw['release_base_branch']!r}; falling back to "
                    f"default {pm.release_base_branch!r}",
                    file=_sys.stderr,
                )
        if pm_raw.get("release_threshold") in {"patch", "minor", "major"}:
            pm.release_threshold = pm_raw["release_threshold"]
        if isinstance(pm_raw.get("release_pr_draft"), bool):
            pm.release_pr_draft = pm_raw["release_pr_draft"]
        if isinstance(pm_raw.get("followups"), bool):
            pm.followups = pm_raw["followups"]
        if isinstance(pm_raw.get("followup_label"), str) and pm_raw["followup_label"].strip():
            pm.followup_label = _sanitize(pm_raw["followup_label"], 100)
        config.post_merge = pm
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
