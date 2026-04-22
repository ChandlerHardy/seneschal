"""Full multi-persona PR review — backend-driven.

Each configured persona gets its own backend.invoke() call in parallel.
Per-persona texts are aggregated into a single review body that is then
posted to GitHub by the caller via `post_review`.

The old `claude -p '/seneschal-review N'` slash-command launcher was
removed in P0: it depended on a consumer CLI backend, which the public
repo no longer ships. Public users get the API backend (`backend.py`).

Persona system prompts
----------------------
Builtin personas (e.g. `architect`, `security`) are Claude Code subagent
definitions at `~/.claude/agents/seneschal-*.md`. We read the file,
strip the YAML frontmatter, and use the remaining markdown as the
system prompt. File-based personas already carry their prompt text on
the `Persona` object.

If a builtin's agent file is absent on the host (the deployer didn't
ship it yet), we fall back to a minimal generic reviewer instruction
and flag the persona in its section output.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from backend import Backend, get_backend
from persona_loader import Persona


# Host locations where install.sh deploys the builtin persona agents.
# We also check the in-repo `agents/` directory as a dev fallback.
_AGENT_SEARCH_DIRS = [
    os.path.expanduser("~/.claude/agents"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "agents"),
]

# Cap the diff we show each persona; the backend caller also truncates,
# but we want the per-persona prompt to stay well under the model's
# context budget even in parallel.
_DIFF_CAP_BYTES = 50_000


@dataclass
class FullReviewResult:
    """Aggregated result of a multi-persona review."""

    body: str
    verdicts: List[str] = field(default_factory=list)
    overall_verdict: str = "COMMENT"


def _strip_frontmatter(text: str) -> str:
    """Remove a leading YAML frontmatter block (``---\n...\n---\n``).

    If there is no frontmatter, returns the input unchanged.
    """
    if not text.startswith("---"):
        return text
    # Find the closing ---.
    after_first = text[3:]
    end_marker = after_first.find("\n---")
    if end_marker == -1:
        return text
    # Skip past the closing marker + its trailing newline(s).
    remainder = after_first[end_marker + len("\n---"):]
    return remainder.lstrip("\n")


def _load_builtin_agent_body(name: str) -> Optional[str]:
    """Return the markdown body of `seneschal-<name>.md` if found.

    Searches install-layout (`~/.claude/agents/`) first, then the in-repo
    `agents/` dir as a dev fallback.
    """
    filename = f"seneschal-{name}.md"
    for directory in _AGENT_SEARCH_DIRS:
        path = Path(directory) / filename
        if path.is_file():
            try:
                raw = path.read_text()
            except OSError:
                continue
            return _strip_frontmatter(raw)
    return None


def _resolve_system_prompt(persona: Persona) -> str:
    """Resolve the persona into a system prompt string.

    - File-based persona: its `prompt_text` is already the prompt.
    - Builtin persona: load the agent markdown body, fall back to a
      generic instruction if the file is missing on the host.
    """
    if persona.prompt_text:
        return persona.prompt_text
    body = _load_builtin_agent_body(persona.name)
    if body:
        return body
    return (
        f"You are the {persona.name} reviewer in a multi-persona PR review. "
        "Be concise. Focus on findings that would block merge. Cap output at "
        "12 findings. If you see nothing, reply with `No findings.`"
    )


def _build_user_prompt(persona_name: str, pr_meta: dict, diff_text: str) -> str:
    pr_title = pr_meta.get("title", "") if isinstance(pr_meta, dict) else ""
    diff_excerpt = diff_text[:_DIFF_CAP_BYTES]
    if len(diff_text) > _DIFF_CAP_BYTES:
        diff_excerpt += "\n\n... (diff truncated)"
    return (
        f"Review this PR from the {persona_name} perspective.\n\n"
        f"PR title: {pr_title}\n\n"
        f"Diff:\n{diff_excerpt}"
    )


def _parse_persona_verdict(text: str) -> str:
    """Lightweight per-persona verdict parse.

    Mirrors the shape used by app.parse_verdict / analyzer layers but
    without importing `app` (avoid circular). Falls back to COMMENT when
    the persona does not emit an explicit verdict line.
    """
    first_lines = (text or "")[:1000].upper()
    if "**VERDICT:** REQUEST_CHANGES" in first_lines or "**VERDICT:** REQUEST CHANGES" in first_lines:
        return "REQUEST_CHANGES"
    if "**VERDICT:** APPROVE" in first_lines:
        return "APPROVE"
    if "**VERDICT:** COMMENT" in first_lines:
        return "COMMENT"
    # Heuristic fallbacks matching single-pass format.
    if "NEEDS CHANGES" in first_lines or "NEEDS_CHANGES" in first_lines:
        return "REQUEST_CHANGES"
    if "BLOCKER" in first_lines:
        return "REQUEST_CHANGES"
    if "LGTM" in first_lines:
        return "APPROVE"
    return "COMMENT"


def _aggregate_verdict(verdicts: List[str]) -> str:
    """Overall verdict from per-persona verdicts.

    - Any REQUEST_CHANGES wins (one blocker blocks the PR).
    - Otherwise APPROVE if at least half the personas approved.
    - Otherwise COMMENT.
    """
    if not verdicts:
        return "COMMENT"
    if any(v == "REQUEST_CHANGES" for v in verdicts):
        return "REQUEST_CHANGES"
    approvals = sum(1 for v in verdicts if v == "APPROVE")
    if approvals * 2 >= len(verdicts):
        return "APPROVE"
    return "COMMENT"


def _invoke_persona(
    backend: Backend,
    persona: Persona,
    pr_meta: dict,
    diff_text: str,
    timeout: int,
) -> tuple[str, str]:
    """Run one persona; return (persona_name, assistant_text).

    Exceptions are caught and surfaced in the returned text so the
    aggregator always produces a complete body.
    """
    try:
        text = backend.invoke(
            _build_user_prompt(persona.name, pr_meta, diff_text),
            system_prompt=_resolve_system_prompt(persona),
            max_turns=1,
            timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001
        text = f"_(persona `{persona.name}` failed: {e})_"
    return persona.name, (text or "").strip()


def run_full_review(
    pr_number: int,
    repo_path: str,
    *,
    personas: Optional[List[Persona]] = None,
    pr_meta: Optional[dict] = None,
    diff_text: str = "",
    timeout: int = 300,
    backend: Optional[Backend] = None,
) -> FullReviewResult:
    """Run one backend call per persona in parallel; aggregate into a review.

    Args:
        pr_number: PR the review is for (used in the body header).
        repo_path: local clone path (reserved for future context loading).
        personas: personas to run. Required — the caller resolves via
            persona_loader and passes in the resolved list.
        pr_meta: GitHub PR metadata dict; uses `title` for the prompt.
        diff_text: the PR diff to review. Capped internally.
        timeout: per-persona wall-clock limit.
        backend: injectable backend for tests; defaults to `get_backend()`.

    Returns:
        FullReviewResult with the aggregated body, per-persona verdicts,
        and overall verdict.
    """
    if not personas:
        # Aggregator called with no personas is a programming error upstream,
        # but return a skeleton rather than raise so the webhook handler
        # doesn't crash mid-request.
        return FullReviewResult(
            body=(
                f"## Multi-persona review\n\n"
                f"_No personas configured for PR #{pr_number}; skipping._"
            ),
            verdicts=[],
            overall_verdict="COMMENT",
        )

    backend_obj: Backend = backend if backend is not None else get_backend()
    pr_meta = pr_meta or {}

    # Parallel fan-out. Max workers capped to len(personas) so a misconfigured
    # 50-persona setup doesn't hammer the SDK with 50 threads.
    results: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=len(personas)) as pool:
        futures = {
            pool.submit(
                _invoke_persona,
                backend_obj,
                p,
                pr_meta,
                diff_text,
                timeout,
            ): p.name
            for p in personas
        }
        for fut in as_completed(futures):
            name, text = fut.result()
            results[name] = text

    # Preserve the caller's persona ordering in the rendered body.
    sections: List[str] = ["## Multi-persona review", ""]
    verdicts: List[str] = []
    for p in personas:
        text = results.get(p.name, "_(no response)_").strip() or "_(empty response)_"
        sections.append(f"### {p.name}")
        sections.append("")
        sections.append(text)
        sections.append("")
        verdicts.append(_parse_persona_verdict(text))

    overall = _aggregate_verdict(verdicts)

    # Emit an explicit verdict line the existing parse_verdict recognizes,
    # so post_review picks the right GitHub review event.
    sections.insert(1, f"**Verdict:** {overall}")
    sections.insert(2, "")

    body = "\n".join(sections).rstrip() + "\n"
    return FullReviewResult(body=body, verdicts=verdicts, overall_verdict=overall)
