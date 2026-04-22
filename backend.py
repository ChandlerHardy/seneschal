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
protocol in a private module and inject it via `set_backend()`. This
public module has no knowledge of it.

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

# Default max_tokens for review bodies. Enough headroom for a thorough
# persona review (~1.5K output tokens is typical; 4096 leaves room for
# the occasional verbose security or architecture finding without
# triggering a `max_tokens` stop_reason). Callers can override per-call
# via `invoke(max_tokens=...)` when they know the response shape.
DEFAULT_MAX_TOKENS = 4096


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
        max_tokens: Optional[int] = None,
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

    `max_turns` is retained in the signature so a future tool-use loop
    can be added without changing callers, but it does NOT clamp
    `max_tokens` — review bodies need generous headroom regardless of
    how many turns we're budgeting for. Use `max_tokens` explicitly when
    you know the response shape (e.g. a probe that only needs a few
    tokens).

    Truncation detection
    --------------------
    If the API returns `stop_reason="max_tokens"`, `invoke` raises a
    `TruncatedResponseError`. A truncated response is usually missing
    its verdict line and would otherwise silently downgrade the PR
    verdict; bubbling the error lets callers decide whether to retry
    with a larger cap, degrade, or surface the failure.

    Prompt caching
    --------------
    The system prompt is sent as a `text` block with
    `cache_control={"type": "ephemeral"}`. On cache hit, subsequent
    reviews of the same repo (same persona + same ADR context) pay a
    fraction of the input-token cost. Anthropic's prompt-cache minimum
    block size (~1024 tokens at time of writing) applies — prompts
    smaller than that are silently not cached.
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
        max_tokens: Optional[int] = None,
        timeout: int = 300,
    ) -> str:
        """Call the Anthropic Messages API and return the first text block.

        Args:
            prompt: user-role message content.
            system_prompt: optional system prompt. Wrapped in a
                `cache_control=ephemeral` text block so repeated reviews
                amortize the tokens (subject to Anthropic's ~1024-token
                minimum block size).
            max_turns: turn budget hint. Reserved for future tool-use
                loops; does not affect `max_tokens` today.
            max_tokens: per-call output cap. Defaults to
                `DEFAULT_MAX_TOKENS` (4096) — enough for a typical
                persona review. Override when you need a shorter probe.
            timeout: per-request wall-clock limit in seconds.

        Raises:
            TruncatedResponseError: if the API returns
                `stop_reason="max_tokens"`. Truncated responses usually
                drop the verdict line and would silently degrade the PR
                verdict; callers catch this explicitly.
        """
        effective_max_tokens = max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS

        kwargs: dict = {
            "model": self._model,
            "max_tokens": effective_max_tokens,
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
            # Scrub anything that looks like an API key out of the error
            # string before logging. Anthropic SDK errors typically don't
            # carry the key, but AuthenticationError paths on custom
            # proxies can, and we log to journalctl which is widely
            # readable on OCI.
            self._logger(
                f"backend: anthropic.messages.create failed: {_scrub_api_key(str(e))}"
            )
            raise

        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "max_tokens":
            raise TruncatedResponseError(
                f"Anthropic response truncated at max_tokens={effective_max_tokens}. "
                "The review body likely lost its verdict line; retry with a higher cap."
            )

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


class TruncatedResponseError(RuntimeError):
    """Raised when Anthropic returns stop_reason='max_tokens'.

    A truncated response is almost always missing its trailing verdict
    line, so silently returning it would downgrade the PR verdict. The
    caller decides whether to retry, degrade, or surface the failure.
    """


# Anthropic API keys start with `sk-ant-` and are 100+ chars; z.ai / other
# proxies can issue looser tokens. Match broadly on common prefixes and
# any long base64-ish bearer run so proxy-issued keys also get scrubbed.
import re as _re  # noqa: E402 — localized to the scrubber

_API_KEY_PATTERN = _re.compile(
    r"(sk-ant-[A-Za-z0-9_\-]{20,}|Bearer\s+[A-Za-z0-9_\-.=]{20,})"
)


def _scrub_api_key(text: str) -> str:
    """Redact anything that looks like an API key out of an error string."""
    return _API_KEY_PATTERN.sub("<redacted>", text)


# ---------------------------------------------------------------------------
# Factory / singleton
# ---------------------------------------------------------------------------

_backend_singleton: Optional[Backend] = None


def get_backend() -> Backend:
    """Return the module-level backend, constructing on first call.

    Tests — and private-fork backends — use `set_backend(...)` to inject
    a different implementation without running the default constructor
    (which would require a live API key).
    """
    global _backend_singleton
    if _backend_singleton is None:
        _backend_singleton = ApiBackend()
    return _backend_singleton


def set_backend(backend: Optional[Backend]) -> None:
    """Replace the module-level backend singleton.

    Pass `None` to clear and force a fresh construction on the next
    `get_backend()` call. Used by:
        - tests that inject a fake/mock backend
        - private forks that register a non-default backend (e.g. one
          built around a different auth or transport)
    """
    global _backend_singleton
    _backend_singleton = backend


# Backward-compat alias. The test suite originally called this name; keep
# it as a thin pass-through so downstream test files don't break.
set_backend_for_tests = set_backend
