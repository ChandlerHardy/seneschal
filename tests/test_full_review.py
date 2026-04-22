"""Tests for full_review: backend-driven multi-persona review.

The old slash-command launcher (which shelled out to `claude -p`) was
removed in P0. These tests exercise the new shape: parallel
`backend.invoke` calls, aggregated body, verdict aggregation.

Real backends are never called — we inject a fake Backend.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from full_review import (  # noqa: E402
    FullReviewResult,
    _aggregate_verdict,
    _parse_persona_verdict,
    _strip_frontmatter,
    run_full_review,
)
from persona_loader import Persona  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeBackend:
    """Records every invoke() call; returns per-persona canned text."""

    def __init__(self, responses: dict | None = None, delay: float = 0.0):
        # map persona-name-substring → canned response body
        self._responses = responses or {}
        self._delay = delay
        self._lock = threading.Lock()
        self.calls: list[dict] = []
        self.concurrent = 0
        self.peak_concurrent = 0

    def invoke(self, prompt, system_prompt=None, max_turns=25, timeout=300):
        with self._lock:
            self.concurrent += 1
            self.peak_concurrent = max(self.peak_concurrent, self.concurrent)
            self.calls.append({
                "prompt": prompt,
                "system_prompt": system_prompt,
                "max_turns": max_turns,
                "timeout": timeout,
            })
        try:
            if self._delay:
                time.sleep(self._delay)
            for key, resp in self._responses.items():
                if key in prompt:
                    return resp
            return "No findings."
        finally:
            with self._lock:
                self.concurrent -= 1


def _builtin(name: str) -> Persona:
    return Persona(
        name=name,
        subagent_type=f"seneschal-{name}",
        prompt_text="",
        source="builtin",
    )


def _file_persona(name: str, prompt: str) -> Persona:
    return Persona(
        name=name,
        subagent_type=None,
        prompt_text=prompt,
        source=f"file:.seneschal/personas/{name}.md",
    )


# ---------------------------------------------------------------------------
# _strip_frontmatter
# ---------------------------------------------------------------------------


def test_strip_frontmatter_removes_yaml_block():
    src = "---\nname: x\ntools: []\n---\nBody starts here.\n"
    assert _strip_frontmatter(src) == "Body starts here.\n"


def test_strip_frontmatter_passes_through_when_absent():
    src = "# Heading\n\nNo frontmatter here.\n"
    assert _strip_frontmatter(src) == src


def test_strip_frontmatter_leaves_text_if_closing_missing():
    src = "---\nname: x\n(no closing marker)\n"
    assert _strip_frontmatter(src) == src


# ---------------------------------------------------------------------------
# verdict parsing + aggregation
# ---------------------------------------------------------------------------


def test_parse_persona_verdict_explicit_verdict_line():
    assert _parse_persona_verdict("**Verdict:** APPROVE\n") == "APPROVE"
    assert _parse_persona_verdict("**Verdict:** REQUEST_CHANGES\n") == "REQUEST_CHANGES"
    assert _parse_persona_verdict("**Verdict:** COMMENT\n") == "COMMENT"


def test_parse_persona_verdict_falls_back_to_heuristics():
    assert _parse_persona_verdict("NEEDS CHANGES: missing migration") == "REQUEST_CHANGES"
    assert _parse_persona_verdict("LGTM") == "APPROVE"
    assert _parse_persona_verdict("BLOCKER: null pointer") == "REQUEST_CHANGES"
    assert _parse_persona_verdict("Some notes, no verdict.") == "COMMENT"


def test_aggregate_verdict_any_blocker_wins():
    assert _aggregate_verdict(["APPROVE", "REQUEST_CHANGES", "COMMENT"]) == "REQUEST_CHANGES"


def test_aggregate_verdict_majority_approve():
    assert _aggregate_verdict(["APPROVE", "APPROVE", "COMMENT"]) == "APPROVE"


def test_aggregate_verdict_falls_back_to_comment():
    assert _aggregate_verdict(["COMMENT", "COMMENT"]) == "COMMENT"


def test_aggregate_verdict_empty():
    assert _aggregate_verdict([]) == "COMMENT"


# ---------------------------------------------------------------------------
# run_full_review — body shape + calls + parallelism + aggregation
# ---------------------------------------------------------------------------


def test_run_full_review_calls_backend_once_per_persona():
    backend = _FakeBackend()
    personas = [_builtin("architect"), _builtin("security"), _builtin("design")]
    with tempfile.TemporaryDirectory() as repo:
        result = run_full_review(
            pr_number=42,
            personas=personas,
            pr_meta={"title": "test pr"},
            diff_text="diff --git a/x b/x\n",
            backend=backend,
        )
    assert len(backend.calls) == 3
    assert isinstance(result, FullReviewResult)


def test_run_full_review_body_contains_per_persona_sections():
    backend = _FakeBackend(responses={
        "architect perspective": "architect says: **Verdict:** APPROVE\nlooks fine",
        "security perspective": "security says: **Verdict:** APPROVE\nno issues",
    })
    personas = [_builtin("architect"), _builtin("security")]
    with tempfile.TemporaryDirectory() as repo:
        result = run_full_review(
            pr_number=1,
            personas=personas,
            pr_meta={"title": "feat: x"},
            diff_text="+added line",
            backend=backend,
        )

    body = result.body
    assert "## Multi-persona review" in body
    assert "### architect" in body
    assert "### security" in body
    assert "architect says:" in body
    assert "security says:" in body
    assert "**Verdict:** APPROVE" in body  # overall verdict line
    assert result.overall_verdict == "APPROVE"
    assert result.verdicts == ["APPROVE", "APPROVE"]


def test_run_full_review_request_changes_when_any_persona_blocks():
    backend = _FakeBackend(responses={
        "architect perspective": "**Verdict:** APPROVE\nfine",
        "security perspective": "**Verdict:** REQUEST_CHANGES\nfound a secret",
    })
    personas = [_builtin("architect"), _builtin("security")]
    with tempfile.TemporaryDirectory() as repo:
        result = run_full_review(
            pr_number=1,
            personas=personas,
            pr_meta={"title": "t"},
            diff_text="d",
            backend=backend,
        )
    assert result.overall_verdict == "REQUEST_CHANGES"
    assert "**Verdict:** REQUEST_CHANGES" in result.body


def test_run_full_review_passes_persona_prompt_as_system_prompt_for_file_personas():
    backend = _FakeBackend()
    personas = [_file_persona("hipaa", "Focus on PHI handling.")]
    with tempfile.TemporaryDirectory() as repo:
        run_full_review(
            pr_number=1,
            personas=personas,
            pr_meta={"title": "t"},
            diff_text="d",
            backend=backend,
        )
    assert len(backend.calls) == 1
    sys_prompt = backend.calls[0]["system_prompt"]
    assert "PHI" in sys_prompt


def test_run_full_review_loads_builtin_agent_body_when_available(tmp_path_factory=None):
    # Point the agent search at a tempdir that contains a crafted agent file.
    backend = _FakeBackend()
    personas = [_builtin("architect")]

    import full_review as fr

    # Temporarily point the search dir list at a fresh tempdir and drop in
    # a minimal agent file. Restore at the end.
    with tempfile.TemporaryDirectory() as agent_dir:
        agent_file = os.path.join(agent_dir, "seneschal-architect.md")
        with open(agent_file, "w") as fh:
            fh.write(
                "---\n"
                "name: seneschal-architect\n"
                "tools: []\n"
                "---\n"
                "You are the ARCHITECT lane. Unique marker: XYZZY-ARCH.\n"
            )

        original = fr._AGENT_SEARCH_DIRS[:]
        fr._AGENT_SEARCH_DIRS[:] = [agent_dir]
        try:
            with tempfile.TemporaryDirectory() as repo:
                run_full_review(
                    pr_number=1,
                    personas=personas,
                    pr_meta={"title": "t"},
                    diff_text="d",
                    backend=backend,
                )
        finally:
            fr._AGENT_SEARCH_DIRS[:] = original

    sys_prompt = backend.calls[0]["system_prompt"]
    assert "XYZZY-ARCH" in sys_prompt


def test_run_full_review_builtin_falls_back_to_generic_when_file_missing():
    backend = _FakeBackend()
    personas = [_builtin("nonexistent-builtin")]

    import full_review as fr

    with tempfile.TemporaryDirectory() as empty_dir:
        original = fr._AGENT_SEARCH_DIRS[:]
        fr._AGENT_SEARCH_DIRS[:] = [empty_dir]
        try:
            with tempfile.TemporaryDirectory() as repo:
                run_full_review(
                    pr_number=1,
                    personas=personas,
                    pr_meta={"title": "t"},
                    diff_text="d",
                    backend=backend,
                )
        finally:
            fr._AGENT_SEARCH_DIRS[:] = original

    sys_prompt = backend.calls[0]["system_prompt"]
    assert "nonexistent-builtin" in sys_prompt
    # Generic fallback mentions "reviewer".
    assert "reviewer" in sys_prompt.lower()


def test_run_full_review_invokes_in_parallel():
    backend = _FakeBackend(delay=0.05)
    personas = [_builtin("architect"), _builtin("security"), _builtin("design")]
    with tempfile.TemporaryDirectory() as repo:
        start = time.time()
        run_full_review(
            pr_number=1,
            personas=personas,
            pr_meta={"title": "t"},
            diff_text="d",
            backend=backend,
        )
        elapsed = time.time() - start

    # Serial would be ~0.15s; parallel should comfortably complete in under
    # 0.12s on any normal machine. Generous margin avoids flakes.
    assert elapsed < 0.12, f"parallel dispatch looked serial: {elapsed}s"
    assert backend.peak_concurrent >= 2


def test_run_full_review_empty_personas_returns_skeleton():
    backend = _FakeBackend()
    with tempfile.TemporaryDirectory() as repo:
        result = run_full_review(
            pr_number=7,
            personas=[],
            pr_meta={"title": "t"},
            diff_text="d",
            backend=backend,
        )
    assert backend.calls == []
    assert result.overall_verdict == "COMMENT"
    assert "skipping" in result.body.lower() or "no personas" in result.body.lower()


def test_run_full_review_persona_failure_surfaces_in_body_without_crashing():
    class _BrokenBackend:
        def invoke(self, *args, **kwargs):
            raise RuntimeError("boom")

    personas = [_builtin("architect"), _builtin("security")]
    with tempfile.TemporaryDirectory() as repo:
        result = run_full_review(
            pr_number=1,
            personas=personas,
            pr_meta={"title": "t"},
            diff_text="d",
            backend=_BrokenBackend(),
        )
    assert "architect" in result.body
    assert "security" in result.body
    assert "failed" in result.body.lower()


def test_run_full_review_truncates_large_diff_before_backend_call():
    backend = _FakeBackend()
    personas = [_builtin("architect")]
    huge_diff = "x" * 200_000  # 200KB; exceeds the 50KB cap

    with tempfile.TemporaryDirectory() as repo:
        run_full_review(
            pr_number=1,
            personas=personas,
            pr_meta={"title": "t"},
            diff_text=huge_diff,
            backend=backend,
        )

    user_prompt = backend.calls[0]["prompt"]
    # The whole 200KB must not have been shipped.
    assert len(user_prompt) < 100_000
    assert "truncated" in user_prompt.lower()
