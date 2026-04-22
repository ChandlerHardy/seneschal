"""LLM backend abstraction for Seneschal.

The public repo ships exactly ONE backend: `ApiBackend`, which wraps the
Anthropic Messages API. This is the Terms-of-Service-safe path: every
public user brings their own `ANTHROPIC_API_KEY` and pays per token.

Previous versions shelled out to `claude -p` to route reviews through a
Claude Max consumer subscription. That path was removed from the public
repo because framing a self-hosted reviewer around sidestepping
per-token billing via a consumer subscription is on the wrong side of
Anthropic's consumer Terms. If an operator wants to re-create the CLI
path for personal infrastructure, they can implement the `Backend`
protocol in a private module and inject it via `set_backend_for_tests`
(or a factory override). This public module has no knowledge of it.

Env vars:
    ANTHROPIC_API_KEY   — required. Raises at construction if unset.
    ANTHROPIC_BASE_URL  — optional; defaults to `https://api.anthropic.com`.
                          Lets operators point at an Anthropic-compatible
                          endpoint.
    SENESCHAL_MODEL     — optional; defaults to `claude-sonnet-4-5-20250929`.
"""

from __future__ import annotations

import os
from typing import Callable, Optional, Protocol, runtime_checkable

import anthropic

# Defaults. Override via env or constructor args.
DEFAULT_MODEL = "claude-sonnet-4-5-20250929"
DEFAULT_BASE_URL = "https://api.anthropic.com"

# Max-token caps. A `max_turns=1` call is a short probe-style response;
# anything else is a PR review body. These caps bound the per-call cost.
SINGLE_TURN_MAX_TOKENS = 256
MULTI_TURN_MAX_TOKENS = 4096


@runtime_checkable
class Backend(Protocol):
    """LLM backend protocol.

    Implementations take a prompt + optional system prompt and return the
    assistant's final text. They are responsible for their own auth,
    base-URL routing, and timeout handling. Callers never see HTTP
    plumbing.
    """

    def invoke(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        max_turns: int = 25,
        timeout: int = 300,
    ) -> str:
        ...


class ApiBackend:
    """Backend that calls the Anthropic Messages API.

    Turn semantics
    --------------
    Without tool use on our side, a single `messages.create` call is one
    assistant turn. The review path in this repo does not wire tools, so
    `invoke` makes exactly one API call and returns its text.

    We keep `max_turns` in the signature (and cap `max_tokens` based on
    it) so a future tool-use loop can be added without changing callers.
    For now: `max_turns == 1` means "short response" (probe, verdict-only
    check); anything else means "full review body".

    Prompt caching
    --------------
    The system prompt is sent as a `text` block with
    `cache_control={"type": "ephemeral"}`. On cache hit, subsequent
    reviews of the same repo (same persona + same ADR context) pay a
    fraction of the input-token cost. This is API-only — the CLI path
    could not opt into it.
    """

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        logger: Callable[[str], None] = print,
    ):
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is required to construct ApiBackend. "
                "Set it in the systemd unit (Environment=ANTHROPIC_API_KEY=...) "
                "or export it in the shell before starting seneschal."
            )
        self._api_key = key
        self._base_url = (
            base_url
            or os.environ.get("ANTHROPIC_BASE_URL")
            or DEFAULT_BASE_URL
        )
        self._model = (
            model
            or os.environ.get("SENESCHAL_MODEL")
            or DEFAULT_MODEL
        )
        self._logger = logger
        self._client = anthropic.Anthropic(
            api_key=self._api_key,
            base_url=self._base_url,
        )

    def invoke(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        max_turns: int = 25,
        timeout: int = 300,
    ) -> str:
        """Call the Anthropic Messages API and return the first text block.

        Args:
            prompt: user-role message content.
            system_prompt: optional system prompt. Wrapped in a
                `cache_control=ephemeral` text block so repeated reviews
                amortize the tokens.
            max_turns: turn budget hint; caps `max_tokens` conservatively
                when 1 (probe-style), more generously otherwise.
            timeout: per-request wall-clock limit in seconds.
        """
        max_tokens = (
            SINGLE_TURN_MAX_TOKENS if max_turns <= 1 else MULTI_TURN_MAX_TOKENS
        )

        kwargs: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "timeout": timeout,
        }

        if system_prompt:
            kwargs["system"] = [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]

        try:
            response = self._client.messages.create(**kwargs)
        except Exception as e:  # noqa: BLE001
            self._logger(f"backend: anthropic.messages.create failed: {e}")
            raise

        # The Messages API returns a list of content blocks; we return the
        # text of the first one. For tool-use responses we'd need to iterate,
        # but this path is plain text.
        content = getattr(response, "content", None) or []
        if not content:
            self._logger("backend: anthropic.messages.create returned empty content")
            return ""
        first = content[0]
        text = getattr(first, "text", None)
        if text is None:
            # Defensive: if the first block is a non-text block type (e.g.
            # tool_use), fall back to a stringified form. The public review
            # path never sets tools, so this branch is belt-and-suspenders.
            return str(first)
        return text


# ---------------------------------------------------------------------------
# Factory / singleton
# ---------------------------------------------------------------------------

_backend_singleton: Optional[Backend] = None


def get_backend() -> Backend:
    """Return the module-level backend, constructing on first call.

    Tests use `set_backend_for_tests(...)` to inject a fake without the
    real constructor running (which would require a live API key).
    """
    global _backend_singleton
    if _backend_singleton is None:
        _backend_singleton = ApiBackend()
    return _backend_singleton


def set_backend_for_tests(backend: Optional[Backend]) -> None:
    """Replace the singleton. Pass `None` to clear and force a fresh
    construction on the next `get_backend()` call."""
    global _backend_singleton
    _backend_singleton = backend
