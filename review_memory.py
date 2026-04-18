"""Per-repo review memory.

Accumulates feedback patterns over time in `.ch-code-reviewer-memory.md`
at the repo root. Future reviews read the memory file and inject its
content into the review system prompt so the reviewer learns a repo's
recurring patterns.

Designed to stay simple and human-editable: it's a flat markdown file
with one rule per line. The bot appends new rules; humans can edit,
reorder, or delete them freely.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import List, Optional


MEMORY_FILENAME = ".ch-code-reviewer-memory.md"
MAX_RULES = 50  # cap so the prompt stays small
MAX_RULE_LEN = 200  # per-rule cap; the file lives in a writable repo so we
                     # treat each line as untrusted user input
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _sanitize_rule(text: str) -> str:
    text = _CONTROL_CHARS.sub("", text)
    text = text.replace("\r", " ").replace("\n", " ")
    text = " ".join(text.split())
    return text[:MAX_RULE_LEN]


@dataclass
class ReviewMemory:
    rules: List[str] = field(default_factory=list)
    path: Optional[str] = None

    @property
    def is_empty(self) -> bool:
        return len(self.rules) == 0

    def prompt_block(self) -> str:
        if self.is_empty:
            return ""
        lines = ["## Repo-specific recurring patterns", ""]
        for rule in self.rules:
            lines.append(f"- {rule}")
        return "\n".join(lines)

    def add(self, rule: str) -> bool:
        """Add a rule if it's not already present (case-insensitive). Returns True if added."""
        rule = rule.strip()
        if not rule:
            return False
        normalized = rule.lower()
        for existing in self.rules:
            if existing.lower() == normalized:
                return False
        self.rules.append(rule)
        # Cap at MAX_RULES — drop oldest first.
        if len(self.rules) > MAX_RULES:
            self.rules = self.rules[-MAX_RULES:]
        return True

    def save(self) -> None:
        """Write the memory file atomically.

        Uses write-then-rename so a crash mid-save can't leave a half-written
        truncated file. Uses a unique mkstemp sibling rather than a fixed
        `.tmp` suffix so two concurrent writers don't truncate each other's
        in-flight writes (the earlier fixed name race let one save()
        replace a partial file into the target path).
        """
        if self.path is None:
            return
        target_dir = os.path.dirname(self.path) or "."
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(self.path)}.",
            suffix=".tmp",
            dir=target_dir,
        )
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write("# ch-code-reviewer memory\n")
                fh.write("# One rule per bullet. Edit freely — the bot will not overwrite your edits on load.\n\n")
                for rule in self.rules:
                    fh.write(f"- {rule}\n")
            os.replace(tmp_path, self.path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


def parse(raw: str) -> List[str]:
    rules: List[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- "):
            candidate = stripped[2:].strip()
        elif stripped.startswith("-"):
            candidate = stripped[1:].strip()
        else:
            continue
        sanitized = _sanitize_rule(candidate)
        if sanitized:
            rules.append(sanitized)
        if len(rules) >= MAX_RULES:
            break
    return rules


def load(repo_dir: str) -> ReviewMemory:
    if not repo_dir:
        return ReviewMemory()
    path = os.path.join(repo_dir, MEMORY_FILENAME)
    if not os.path.exists(path):
        return ReviewMemory(path=path)
    try:
        with open(path) as fh:
            raw = fh.read()
        return ReviewMemory(rules=parse(raw), path=path)
    except OSError:
        return ReviewMemory(path=path)
