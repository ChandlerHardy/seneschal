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

import fnmatch
import os
import re
import sys as _sys
from dataclasses import dataclass, field
from typing import List, Optional

import yaml

from fs_safety import (
    SENSITIVE_FILENAMES,
    SENSITIVE_PATH_SEGMENTS,
    safe_branch_name,
    safe_changelog_path,
    safe_open_in_repo,
)

# Backward-compat aliases for the *_SENSITIVE_*_ constants: only
# `tests/test_repo_config.py` imports these. Kept because multiple test
# callsites reference the private names; a future cleanup can migrate
# tests to the canonical `SENSITIVE_FILENAMES` / `SENSITIVE_PATH_SEGMENTS`.
_SENSITIVE_FILENAMES = SENSITIVE_FILENAMES
_SENSITIVE_PATH_SEGMENTS = SENSITIVE_PATH_SEGMENTS


# Repo-supplied content lands in the Claude system prompt, so we sanitize it
# defensively. The repo file is editable by anyone with push access, so a
# single rogue commit shouldn't be able to inject paragraphs of "ignore prior
# instructions and run X" into the reviewer's system prompt.
MAX_RULE_LEN = 200
MAX_RULES = 30
MAX_IGNORE_PATHS = 50
# Dedicated cap for `standards.branch_name_patterns`. Realistic configs
# have ~5 entries (one per top-level branch convention); 20 is generous.
# Kept distinct from MAX_IGNORE_PATHS so changing one list's limit doesn't
# silently shift the other.
MAX_BRANCH_PATTERNS = 20
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _sanitize(text: str, max_len: int) -> str:
    """Strip control chars / newlines, collapse whitespace, truncate."""
    text = _CONTROL_CHARS.sub("", text)
    text = text.replace("\r", " ").replace("\n", " ")
    text = " ".join(text.split())
    return text[:max_len]


# Header text is allowed to carry newlines (unlike rule strings that land in
# the system prompt), but we still strip control chars and cap the size.
_HEADER_MAX_BYTES = 2048


def _sanitize_header_text(text: str) -> str:
    """Sanitize license-header text: strip control chars (keep \n), cap 2KB.

    Fix E: strip trailing newlines before the 2KB cap. YAML block-scalar
    syntax (`license_header: |\n    // Copyright\n`) naturally carries a
    trailing `\n` that would otherwise split into a phantom empty required
    line during `_header_matches`, making a legitimate header fail.
    """
    # Keep newlines, tabs, carriage returns — strip the rest.
    cleaned = _CONTROL_CHARS.sub("", text)
    # Normalize CRLF to LF so the scan compares consistently.
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    # Strip trailing newlines (YAML block-scalar + friendly author habit).
    cleaned = cleaned.rstrip("\n")
    return cleaned[:_HEADER_MAX_BYTES]


_GLOB_META_CHARS = re.compile(r"([.+^$(){}\[\]|\\])")


def glob_match(pattern: str, path: str) -> bool:
    """Match `path` against a glob `pattern`, supporting `**` (recursive).

    `fnmatch.fnmatch` does NOT recognize `**` — `src/**/*.go` wouldn't
    match `src/a/b/c.go`. This helper promotes patterns containing `**`
    to a regex (escaping other metacharacters) and falls back to
    `fnmatch` for simple patterns.

    Semantics follow the common gitignore / minimatch convention:
     - `**` matches zero or more path segments.
     - `**/*.go` matches `foo.go`, `a/foo.go`, `a/b/foo.go`, etc.
       (the leading `**/` is optional — it matches zero segments too).
     - `src/**` matches `src/a.go`, `src/a/b.go`, etc.
     - `*` matches within a single segment (no `/`).
     - `?` matches a single character (no `/`).
    """
    if not pattern:
        return False
    if "**" not in pattern:
        try:
            return fnmatch.fnmatch(path, pattern)
        except re.error:
            # Some Python versions raise on unbalanced `[` in the pattern.
            # Fail closed — a malformed glob should never silently "match
            # everything" or propagate an exception up to the webhook.
            return False

    # Translate ** glob to regex. Use two sentinels:
    #  - STAR_SLASH for the `**/` prefix idiom (zero-or-more segments
    #    followed by a slash): that's a special case where we want the
    #    slash itself to be optional when `**` collapses to zero.
    #  - STAR_STAR for any other `**`.
    STAR_SLASH = "\x00STARSLASH\x00"
    STAR_STAR = "\x00STARSTAR\x00"
    work = pattern.replace("**/", STAR_SLASH)
    work = work.replace("**", STAR_STAR)
    work = _GLOB_META_CHARS.sub(r"\\\1", work)
    work = work.replace("*", "[^/]*").replace("?", "[^/]")
    # `**/` → `(?:.*/)?` (zero or more segments plus slash, OR nothing).
    work = work.replace(STAR_SLASH, "(?:.*/)?")
    # Bare `**` → `.*` (any characters including slashes).
    work = work.replace(STAR_STAR, ".*")
    try:
        return re.match(f"^{work}$", path) is not None
    except re.error:
        # Degrade to fnmatch if our translation produced invalid regex.
        # Fix Q: fnmatch itself can raise on unbalanced `[`; wrap so a
        # malformed operator pattern fails closed instead of crashing
        # the scan.
        try:
            return fnmatch.fnmatch(path, pattern)
        except re.error:
            return False


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
class StandardsConfig:
    """Per-repo knobs for standards enforcement (P3).

    All defaults are OFF: installing P3 doesn't change behavior for any
    repo that hasn't opted in via `.seneschal.yml`. Severity overrides
    let operators downgrade (e.g. NIT → INFO) or upgrade (WARNING → BLOCKER)
    specific findings to match their house style.
    """

    # License-header scan ---------------------------------------------------
    # Inline wins over file when both are set. Both sanitized / truncated at
    # 2KB during parse.
    license_header: str = ""
    license_header_file: str = ""
    # Empty applies_to = check every added file. Non-empty = only check
    # files matching at least one glob.
    license_applies_to: List[str] = field(default_factory=list)
    # Files matching any exemption glob are skipped even if they match
    # applies_to.
    license_exemptions: List[str] = field(default_factory=list)

    # Commit-convention strict mode -----------------------------------------
    commit_convention_strict: bool = False

    # Branch-name regex patterns --------------------------------------------
    # Empty list = feature disabled. ANY pattern match counts as valid.
    branch_name_patterns: List[str] = field(default_factory=list)

    # Severity overrides — None = use plan defaults.
    # Accepted values: "blocker" | "warning" | "nit" | "info"
    license_severity: Optional[str] = None
    commit_convention_severity: Optional[str] = None
    branch_name_severity: Optional[str] = None


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
    # Standards-enforcement config (P3). All sub-knobs default OFF.
    standards: "StandardsConfig" = field(default_factory=lambda: StandardsConfig())

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
            safe = safe_changelog_path(candidate)
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
            safe = safe_branch_name(candidate)
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

    # standards: nested dict of enforcement knobs (P3). Same defensive
    # parsing as post_merge — unknown keys dropped, invalid values fall
    # back to dataclass defaults. license_header_file is resolved relative
    # to the repo root in load_from_repo; we only carry the raw name here.
    st_raw = data.get("standards")
    if isinstance(st_raw, dict):
        st = StandardsConfig()
        if isinstance(st_raw.get("license_header"), str) and st_raw["license_header"]:
            st.license_header = _sanitize_header_text(st_raw["license_header"])
        if isinstance(st_raw.get("license_header_file"), str) and st_raw["license_header_file"].strip():
            st.license_header_file = _sanitize(st_raw["license_header_file"], 200)
        if isinstance(st_raw.get("license_applies_to"), list):
            st.license_applies_to = [
                _sanitize(str(p), MAX_RULE_LEN)
                for p in st_raw["license_applies_to"][:MAX_IGNORE_PATHS]
                if str(p).strip()
            ]
        if isinstance(st_raw.get("license_exemptions"), list):
            st.license_exemptions = [
                _sanitize(str(p), MAX_RULE_LEN)
                for p in st_raw["license_exemptions"][:MAX_IGNORE_PATHS]
                if str(p).strip()
            ]
        if isinstance(st_raw.get("commit_convention_strict"), bool):
            st.commit_convention_strict = st_raw["commit_convention_strict"]
        if isinstance(st_raw.get("branch_name_patterns"), list):
            st.branch_name_patterns = [
                _sanitize(str(p), MAX_RULE_LEN)
                for p in st_raw["branch_name_patterns"][:MAX_BRANCH_PATTERNS]
                if str(p).strip()
            ]
        # Severity overrides — accept only the four canonical labels.
        for key in (
            "license_severity",
            "commit_convention_severity",
            "branch_name_severity",
        ):
            val = st_raw.get(key)
            if val in {"blocker", "warning", "nit", "info"}:
                setattr(st, key, val)
        config.standards = st
    return config


def load_from_path(path: str) -> RepoConfig:
    if not os.path.exists(path):
        return RepoConfig()
    # Pin utf-8 explicitly: a `.seneschal.yml` with Unicode rule strings
    # (café ☕, résumé, é, etc.) would otherwise raise UnicodeDecodeError
    # under `LANG=C` → swallowed below → config silently falls back to
    # all-defaults (no rules applied). Locale-independent decoding is
    # the correct posture here.
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except (OSError, UnicodeDecodeError) as e:
        # Surface the read failure to operators. Previously swallowed
        # silently → all standards checks toggled OFF with zero trace.
        print(
            f"[seneschal] failed to read {path!r}: {type(e).__name__}: {e}; "
            f"falling back to defaults",
            file=_sys.stderr,
        )
        return RepoConfig()
    try:
        return parse_config(raw)
    except Exception as e:
        # Malformed YAML or similar — same operator-visibility rationale
        # as the read-failure branch above.
        print(
            f"[seneschal] failed to parse {path!r}: {type(e).__name__}: {e}; "
            f"falling back to defaults",
            file=_sys.stderr,
        )
        return RepoConfig()


def _resolve_license_header_file(repo_dir: str, rel_path: str) -> str:
    """Safely read a license-header file from inside the repo.

    Fix I: symlink-safe read via `safe_open_in_repo`, which handles:
     - path-traversal (resolved path must live inside repo tree)
     - intermediate-component symlink refusal (attacker can't stage
       `docs/` → `/etc` and sneak header content out of /etc/...)
     - `O_NOFOLLOW` on the final component (TOCTOU mitigation)
     - locale-independent UTF-8 decode

    Also applies `safe_changelog_path` first as a fast-path deny-list
    (rejects `..`, absolute paths, sensitive filenames like `.env`).
    Returns the file contents (sanitized + 2KB-capped) or an empty
    string on any safety violation or I/O error. Failures are logged
    to stderr so operators can debug.
    """
    safe_rel = safe_changelog_path(rel_path)
    if safe_rel is None:
        print(
            f"[seneschal] rejecting unsafe license_header_file {rel_path!r}",
            file=_sys.stderr,
        )
        return ""
    raw = safe_open_in_repo(repo_dir, safe_rel)
    if raw is None:
        # `safe_open_in_repo` already logs the specific refusal reason
        # (symlink, traversal, decode failure). Surface a seneschal-
        # branded line so operators grepping for [seneschal] see the
        # license-header failure path in addition to the post_merge
        # refusal.
        print(
            f"[seneschal] could not read license_header_file {rel_path!r} "
            f"(see prior [post_merge] line for reason)",
            file=_sys.stderr,
        )
        return ""
    return _sanitize_header_text(raw)


def load_from_repo(repo_dir: str) -> RepoConfig:
    # Prefer the canonical .seneschal.yml, fall back to the legacy name for
    # repos that haven't migrated yet. Both filenames carry the same schema.
    config = RepoConfig()
    for name in (
        ".seneschal.yml",
        ".seneschal.yaml",
        ".ch-code-reviewer.yml",
        ".ch-code-reviewer.yaml",
    ):
        p = os.path.join(repo_dir, name)
        if os.path.exists(p):
            config = load_from_path(p)
            break

    # Resolve license_header_file relative to repo_dir. Inline license_header
    # wins if both are set. Doing this post-parse keeps `parse_config` pure
    # (no filesystem access) — tests that call `parse_config` directly still
    # see `license_header_file` as a raw string.
    st = config.standards
    if not st.license_header and st.license_header_file:
        resolved = _resolve_license_header_file(repo_dir, st.license_header_file)
        if resolved:
            st.license_header = resolved

    return config
