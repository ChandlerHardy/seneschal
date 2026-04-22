"""Scan added diff lines for leaked secrets.

Complements risk.py's filename-based secret check by looking at the
actual content of every added line. Catches the case where someone
hard-codes an API key into a regular source file.

False positives are tolerated — the alternative (missing a real leak)
is much worse. Every hit becomes a BLOCKER finding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Sequence

from diff_parser import parse_unified_diff_with_lines


# High-confidence patterns for common key formats. Each pattern must be
# specific enough to avoid matching random base64 strings in test data.
_PATTERNS = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key"),
    (re.compile(r"ASIA[0-9A-Z]{16}"), "AWS temporary access key"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{36,}"), "GitHub personal access token"),
    # gh[pousre]_ covers ghp_, gho_, ghu_, ghs_, ghr_ AND ghe_ (GitHub
    # Enterprise Server). Underscores allowed in the body for newer token
    # formats that embed installation IDs.
    (re.compile(r"gh[pousre]_[A-Za-z0-9_]{36,}"), "GitHub token"),
    (re.compile(r"sk-ant-api\d{2}-[A-Za-z0-9_-]{40,}"), "Anthropic API key"),
    (re.compile(r"sk-proj-[A-Za-z0-9_-]{40,}"), "OpenAI project key"),
    (re.compile(r"sk-[A-Za-z0-9]{40,}"), "OpenAI API key"),
    # Legacy Slack tokens: xoxb- / xoxa- / xoxp- / xoxr- / xoxs-.
    (re.compile(r"xox[baprs]-[0-9a-zA-Z-]{10,}"), "Slack token"),
    # Modern rotating / refresh tokens: xoxe-1-<long> and xoxe.xoxp-<long>.
    (re.compile(r"xoxe(?:\.xox[abp])?-[0-9a-zA-Z-]{10,}"), "Slack refresh token"),
    # App-level tokens: xapp-1-<app>-<timestamp>-<secret>.
    (re.compile(r"xapp-[0-9]-[A-Z0-9]+-[0-9]+-[0-9a-f]{40,}"), "Slack app token"),
    (re.compile(r"SG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}"), "SendGrid API key"),
    (re.compile(r"AIza[0-9A-Za-z_-]{35}"), "Google API key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key material"),
    # Generic high-entropy assignment — only matches when a secret-like name
    # is being assigned a long quoted string. Kept narrow to avoid false hits.
    (re.compile(
        r'(?i)(?:api[_-]?key|secret|password|token|auth)["\']?\s*[:=]\s*["\'][A-Za-z0-9_/+=\-]{20,}["\']'
    ), "hardcoded credential assignment"),
]


def redact(text):
    """Scrub any matched secret pattern from `text`.

    Public API promoted from private `_redact_snippet` / `_redact`
    duplicates that used to live in `review_index` and the MCP server.
    Every new egress channel (MCP tool response, GitHub-API
    passthrough) should run its string content through this once.

    Returns the input untouched when no pattern matches, so this is
    safe to call on any string.

    Round-3: non-str inputs (bytes, int, None, dicts) are returned
    unchanged instead of crashing. The old falsy short-circuit caught
    `None`/`""`/`0` but a truthy non-str (`b"sk-ant-..."`, `123`, a
    dict) fell through to `pattern.sub` which raised `TypeError`.
    Today all callers pass strings; keeping the defensive check costs
    one `isinstance` per call and protects future callers who forward
    `requests.Response.content` (bytes) or cached JSON values without
    first stringifying.
    """
    # Non-str: return unchanged. `pattern.sub` on bytes/int/etc. raises
    # TypeError — we'd rather silently pass the value through than abort
    # the whole MCP tool response on a latent type mismatch.
    if not isinstance(text, str):
        return text
    if not text:
        return text
    out = text
    for pattern, _kind in _PATTERNS:
        out = pattern.sub("***REDACTED***", out)
    return out


@dataclass
class SecretHit:
    kind: str
    file: str
    line: int
    preview: str

    def redacted_preview(self) -> str:
        """Return the hit preview with the secret partially masked.

        Replays every detection pattern against the preview so anything the
        scanner caught also gets masked here. The earlier 16+ char alnum
        heuristic missed short tokens (e.g. xoxb-1234567890) and leaked them
        into public PR comments, so we now apply the canonical pattern list
        and a tighter alnum fallback.

        The mask sentinel is intentionally non-alnum so the alnum-fallback
        sweep does not re-match its own previous output.
        """
        mask = "***"
        masked = self.preview.strip()
        for pattern, _kind in _PATTERNS:
            masked = pattern.sub(mask, masked)
        # Fallback: any 8+ char run of secret-shaped chars is suspicious in a
        # redacted preview. Over-redaction here is preferable to a leak.
        masked = re.sub(r"[A-Za-z0-9_/+=\-]{8,}", mask, masked)
        return masked[:100]


def scan_diff(diff_text: str) -> List[SecretHit]:
    """Return every hit in the PR's added lines."""
    files = parse_unified_diff_with_lines(diff_text)
    out: List[SecretHit] = []
    for filename, pairs in files.items():
        # Skip lock files (noisy, full of base64) and test fixtures.
        lower = filename.lower()
        if lower.endswith((".lock", "package-lock.json", "yarn.lock", "cargo.lock")):
            continue
        if "/fixtures/" in lower or "/testdata/" in lower:
            continue
        for line_num, content in pairs:
            for pattern, kind in _PATTERNS:
                if pattern.search(content):
                    out.append(SecretHit(
                        kind=kind,
                        file=filename,
                        line=line_num,
                        preview=content,
                    ))
                    break  # one hit per line is enough
    return out


def summarize_secrets(hits: Sequence[SecretHit]) -> str:
    if not hits:
        return "**Secret scan: clean** — no hardcoded credentials in added lines."
    by_kind: Dict[str, List[SecretHit]] = {}
    for h in hits:
        by_kind.setdefault(h.kind, []).append(h)
    lines = [f"**Secret scan: {len(hits)} potential leak(s)**", ""]
    for kind, items in sorted(by_kind.items()):
        lines.append(f"- **{kind}** ({len(items)}):")
        for item in items[:3]:
            lines.append(f"  - `{item.file}:{item.line}` - `{item.redacted_preview()}`")
    return "\n".join(lines)
