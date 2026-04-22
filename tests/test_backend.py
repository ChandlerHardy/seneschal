"""Tests for the LLM backend abstraction.

The public repo ships a single `Backend` implementation — `ApiBackend` —
which wraps the Anthropic Messages API. These tests exercise the
invocation shape without issuing real network calls: we patch
`anthropic.Anthropic` to a MagicMock so we can assert on the arguments
the backend hands to `messages.create`.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend as backend_mod  # noqa: E402
from backend import (  # noqa: E402
    ApiBackend,
    Backend,
    get_backend,
    set_backend_for_tests,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_anthropic_response(text: str = "ok", stop_reason: str = "end_turn"):
    """Build an object shaped like an Anthropic Messages API response."""
    response = MagicMock()
    content_block = MagicMock()
    content_block.text = text
    response.content = [content_block]
    response.stop_reason = stop_reason
    return response


def _env(**kwargs):
    """Context manager replacement: clear + set specific env vars for a test."""
    return patch.dict(os.environ, kwargs, clear=False)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_api_backend_requires_anthropic_api_key():
    # Blow away any real key that may be set on the developer's machine.
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            ApiBackend()


def test_api_backend_reads_key_from_env():
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=True):
        with patch("backend.anthropic.Anthropic") as AnthropicCls:
            ApiBackend()
            AnthropicCls.assert_called_once()
            kwargs = AnthropicCls.call_args.kwargs
            assert kwargs["api_key"] == "sk-ant-test"
            # Default base URL.
            assert kwargs["base_url"] == "https://api.anthropic.com"


def test_api_backend_honors_base_url_override():
    with patch.dict(
        os.environ,
        {
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic/v1",
        },
        clear=True,
    ):
        with patch("backend.anthropic.Anthropic") as AnthropicCls:
            ApiBackend()
            kwargs = AnthropicCls.call_args.kwargs
            assert kwargs["base_url"] == "https://api.z.ai/api/anthropic/v1"


# ---------------------------------------------------------------------------
# invoke() behavior
# ---------------------------------------------------------------------------


def test_invoke_calls_messages_create_with_expected_args():
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=True):
        with patch("backend.anthropic.Anthropic") as AnthropicCls:
            client = MagicMock()
            AnthropicCls.return_value = client
            client.messages.create.return_value = _mock_anthropic_response("hello")

            b = ApiBackend()
            text = b.invoke(
                "user prompt here",
                system_prompt="you are a reviewer",
                max_turns=1,
                timeout=42,
            )

            assert text == "hello"
            client.messages.create.assert_called_once()
            kwargs = client.messages.create.call_args.kwargs

            # Model is the default unless SENESCHAL_MODEL overrides it.
            assert kwargs["model"] == "claude-sonnet-4-5-20250929"
            # User prompt lands in the single user-role message.
            assert kwargs["messages"] == [
                {"role": "user", "content": "user prompt here"},
            ]
            # Timeout passes through.
            assert kwargs["timeout"] == 42


def test_invoke_returns_text_from_first_content_block():
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=True):
        with patch("backend.anthropic.Anthropic") as AnthropicCls:
            client = MagicMock()
            AnthropicCls.return_value = client
            client.messages.create.return_value = _mock_anthropic_response(
                "first block text"
            )

            b = ApiBackend()
            assert b.invoke("hi") == "first block text"


def test_invoke_sets_cache_control_on_system_prompt():
    """Prompt caching is a meaningful cost win on large constant system prompts.
    The ApiBackend wraps the system prompt in a TextBlockParam list with
    cache_control={"type": "ephemeral"}. Verify that shape.
    """
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=True):
        with patch("backend.anthropic.Anthropic") as AnthropicCls:
            client = MagicMock()
            AnthropicCls.return_value = client
            client.messages.create.return_value = _mock_anthropic_response()

            b = ApiBackend()
            b.invoke("user prompt", system_prompt="SYSTEM TEXT")

            kwargs = client.messages.create.call_args.kwargs
            assert "system" in kwargs
            system = kwargs["system"]
            # Must be a list of blocks (not a bare str) so we can attach cache_control.
            assert isinstance(system, list)
            assert len(system) == 1
            assert system[0]["type"] == "text"
            assert system[0]["text"] == "SYSTEM TEXT"
            assert system[0]["cache_control"] == {"type": "ephemeral"}


def test_invoke_without_system_prompt_omits_system_key():
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=True):
        with patch("backend.anthropic.Anthropic") as AnthropicCls:
            client = MagicMock()
            AnthropicCls.return_value = client
            client.messages.create.return_value = _mock_anthropic_response()

            b = ApiBackend()
            b.invoke("user prompt")  # no system prompt

            kwargs = client.messages.create.call_args.kwargs
            # Either absent entirely or explicitly None/empty — key check is it
            # is NOT a populated list, since that would burn cache tokens.
            assert "system" not in kwargs or not kwargs["system"]


def test_invoke_uses_generous_default_max_tokens():
    """Default max_tokens must be generous — a truncated review loses its
    verdict line. Earlier versions coupled max_tokens to max_turns and
    capped a 1-turn review at 256 tokens, which silently auto-APPROVEd any
    review long enough to truncate. Guard against that regression."""
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=True):
        with patch("backend.anthropic.Anthropic") as AnthropicCls:
            client = MagicMock()
            AnthropicCls.return_value = client
            client.messages.create.return_value = _mock_anthropic_response()

            b = ApiBackend()
            b.invoke("p")
            kwargs = client.messages.create.call_args.kwargs
            # At least enough headroom for a thorough persona review.
            assert kwargs["max_tokens"] >= 2048


def test_invoke_accepts_explicit_max_tokens_override():
    """Callers can pass max_tokens explicitly for probe-style calls."""
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=True):
        with patch("backend.anthropic.Anthropic") as AnthropicCls:
            client = MagicMock()
            AnthropicCls.return_value = client
            client.messages.create.return_value = _mock_anthropic_response()

            b = ApiBackend()
            b.invoke("p", max_tokens=128)
            kwargs = client.messages.create.call_args.kwargs
            assert kwargs["max_tokens"] == 128


def test_invoke_raises_on_max_tokens_truncation():
    """stop_reason='max_tokens' means the verdict line was likely cut;
    bubble the failure rather than silently returning a truncated body."""
    from backend import TruncatedResponseError
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=True):
        with patch("backend.anthropic.Anthropic") as AnthropicCls:
            client = MagicMock()
            AnthropicCls.return_value = client
            truncated = _mock_anthropic_response("partial...")
            truncated.stop_reason = "max_tokens"
            client.messages.create.return_value = truncated

            b = ApiBackend()
            try:
                b.invoke("p")
                raise AssertionError("expected TruncatedResponseError")
            except TruncatedResponseError:
                pass


def test_scrub_api_key_redacts_anthropic_keys():
    """Exception messages on auth paths can echo the API key. Redact."""
    from backend import _scrub_api_key
    msg = "Auth failed: key sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890 invalid"
    scrubbed = _scrub_api_key(msg)
    assert "sk-ant-" not in scrubbed
    assert "<redacted>" in scrubbed


def test_scrub_api_key_redacts_bearer_tokens():
    from backend import _scrub_api_key
    msg = "401: Bearer abcdef0123456789ABCDEF0123 expired"
    scrubbed = _scrub_api_key(msg)
    assert "Bearer abcdef" not in scrubbed
    assert "<redacted>" in scrubbed


def test_invoke_uses_model_from_env_when_set():
    with patch.dict(
        os.environ,
        {
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "SENESCHAL_MODEL": "claude-haiku-4-5-20251001",
        },
        clear=True,
    ):
        with patch("backend.anthropic.Anthropic") as AnthropicCls:
            client = MagicMock()
            AnthropicCls.return_value = client
            client.messages.create.return_value = _mock_anthropic_response()

            b = ApiBackend()
            b.invoke("p")
            kwargs = client.messages.create.call_args.kwargs
            assert kwargs["model"] == "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# Factory: get_backend singleton + set_backend_for_tests
# ---------------------------------------------------------------------------


def test_get_backend_returns_singleton():
    # Reset any prior state in the module, then stub construction.
    set_backend_for_tests(None)
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=True):
        with patch("backend.anthropic.Anthropic"):
            a = get_backend()
            b = get_backend()
            assert a is b
    set_backend_for_tests(None)


def test_set_backend_for_tests_replaces_singleton():
    class FakeBackend:
        def invoke(self, prompt, system_prompt=None, max_turns=25, timeout=300):
            return f"FAKE::{prompt}"

    fake = FakeBackend()
    set_backend_for_tests(fake)
    try:
        b = get_backend()
        assert b is fake
        assert b.invoke("hello") == "FAKE::hello"
    finally:
        set_backend_for_tests(None)


# ---------------------------------------------------------------------------
# Protocol shape
# ---------------------------------------------------------------------------


def test_backend_protocol_has_invoke():
    # This is a structural-typing check — the Backend protocol has `invoke`.
    # We assert the ApiBackend conforms by calling it through the Protocol type.
    set_backend_for_tests(None)
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=True):
        with patch("backend.anthropic.Anthropic") as AnthropicCls:
            client = MagicMock()
            AnthropicCls.return_value = client
            client.messages.create.return_value = _mock_anthropic_response("ok")

            b: Backend = ApiBackend()
            assert b.invoke("p") == "ok"
    set_backend_for_tests(None)


def test_api_backend_custom_logger_receives_error_messages():
    """The constructor accepts an injected logger; when `invoke` fails, the
    logger (not `print`) receives the diagnostic. This is the contract that
    matters for dependency injection: error paths are observable."""
    messages = []

    def logger(msg):
        messages.append(msg)

    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=True):
        with patch("backend.anthropic.Anthropic") as AnthropicCls:
            client = MagicMock()
            AnthropicCls.return_value = client
            client.messages.create.side_effect = RuntimeError("kaboom")

            b = ApiBackend(logger=logger)
            with pytest.raises(RuntimeError):
                b.invoke("p")

            # The injected logger saw the failure; default `print` was bypassed.
            assert any("kaboom" in m or "failed" in m.lower() for m in messages), messages
