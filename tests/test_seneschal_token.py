"""Tests for seneschal_token: installation-token minting + in-process cache.

The module exposes both a programmatic API (`mint_installation_token`)
and a CLI main; the CLI is a thin shell over the API so most tests exercise
the API directly. All GitHub/JWT calls are mocked.
"""

import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import seneschal_token  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_cache():
    """Each test starts with an empty token cache."""
    seneschal_token._clear_cache()
    yield
    seneschal_token._clear_cache()


def _mock_response(status_code=200, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body if json_body is not None else {}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        import requests
        err = requests.HTTPError(f"{status_code}")
        err.response = resp
        resp.raise_for_status.side_effect = err
    return resp


# --------------------------------------------------------------------------
# Happy path: mint, cache, return.
# --------------------------------------------------------------------------


def test_mint_returns_token_from_github(monkeypatch):
    monkeypatch.setattr(seneschal_token, "_generate_jwt", lambda: "fake-jwt")
    with patch("seneschal_token.requests.get") as rget, patch(
        "seneschal_token.requests.post"
    ) as rpost:
        rget.return_value = _mock_response(json_body={"id": 42})
        rpost.return_value = _mock_response(json_body={"token": "ghs_faketoken123"})
        tok = seneschal_token.mint_installation_token("owner/repo")
    assert tok == "ghs_faketoken123"


def test_mint_caches_tokens_in_process(monkeypatch):
    """Second call for the same slug within the TTL returns the cached token."""
    monkeypatch.setattr(seneschal_token, "_generate_jwt", lambda: "fake-jwt")
    calls = {"get": 0, "post": 0}

    def _fake_get(*args, **kwargs):
        calls["get"] += 1
        return _mock_response(json_body={"id": 42})

    def _fake_post(*args, **kwargs):
        calls["post"] += 1
        return _mock_response(json_body={"token": "ghs_cached"})

    with patch("seneschal_token.requests.get", side_effect=_fake_get), patch(
        "seneschal_token.requests.post", side_effect=_fake_post
    ):
        tok1 = seneschal_token.mint_installation_token("owner/repo")
        tok2 = seneschal_token.mint_installation_token("owner/repo")
    assert tok1 == tok2 == "ghs_cached"
    assert calls["get"] == 1
    assert calls["post"] == 1


def test_mint_cache_expires_after_ttl(monkeypatch):
    """A cache entry older than 50 minutes must be refreshed."""
    monkeypatch.setattr(seneschal_token, "_generate_jwt", lambda: "fake-jwt")
    tokens = iter(["old-token", "new-token"])

    def _fake_post(*args, **kwargs):
        return _mock_response(json_body={"token": next(tokens)})

    with patch("seneschal_token.requests.get") as rget, patch(
        "seneschal_token.requests.post", side_effect=_fake_post
    ):
        rget.return_value = _mock_response(json_body={"id": 42})
        # First mint.
        tok1 = seneschal_token.mint_installation_token("owner/repo")
        # Fast-forward the cache timestamp past the TTL.
        (_, stored_exp) = seneschal_token._CACHE["owner/repo"]
        seneschal_token._CACHE["owner/repo"] = ("old-token", time.time() - 100)
        tok2 = seneschal_token.mint_installation_token("owner/repo")
    assert tok1 == "old-token"
    assert tok2 == "new-token"


# --------------------------------------------------------------------------
# Error paths: 404 → AppNotInstalledError, network → TokenMintError.
# --------------------------------------------------------------------------


def test_mint_raises_app_not_installed_on_404(monkeypatch):
    monkeypatch.setattr(seneschal_token, "_generate_jwt", lambda: "fake-jwt")
    with patch("seneschal_token.requests.get") as rget:
        rget.return_value = _mock_response(status_code=404)
        with pytest.raises(seneschal_token.AppNotInstalledError) as excinfo:
            seneschal_token.mint_installation_token("owner/repo")
    # Exception carries the slug so callers can build a useful message.
    assert "owner/repo" in str(excinfo.value)


def test_mint_raises_token_mint_error_on_network_failure(monkeypatch):
    import requests

    monkeypatch.setattr(seneschal_token, "_generate_jwt", lambda: "fake-jwt")
    with patch("seneschal_token.requests.get") as rget:
        rget.side_effect = requests.ConnectionError("DNS failed")
        with pytest.raises(seneschal_token.TokenMintError):
            seneschal_token.mint_installation_token("owner/repo")


def test_mint_raises_token_mint_error_on_http_error(monkeypatch):
    monkeypatch.setattr(seneschal_token, "_generate_jwt", lambda: "fake-jwt")
    with patch("seneschal_token.requests.get") as rget, patch(
        "seneschal_token.requests.post"
    ) as rpost:
        rget.return_value = _mock_response(json_body={"id": 42})
        rpost.return_value = _mock_response(status_code=500)
        with pytest.raises(seneschal_token.TokenMintError):
            seneschal_token.mint_installation_token("owner/repo")


# --------------------------------------------------------------------------
# Slug normalization — accept both "owner" and "owner/repo".
# --------------------------------------------------------------------------


def test_mint_rejects_invalid_slug():
    with pytest.raises(ValueError):
        seneschal_token.mint_installation_token("")
    with pytest.raises(ValueError):
        seneschal_token.mint_installation_token("no-slash-in-this-one")


# --------------------------------------------------------------------------
# APP_ID env override.
# --------------------------------------------------------------------------


def test_app_id_reads_env_override(monkeypatch):
    """SENESCHAL_APP_ID env var overrides the hardcoded default."""
    monkeypatch.setenv("SENESCHAL_APP_ID", "9999999")
    assert seneschal_token._get_app_id() == 9999999


def test_app_id_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("SENESCHAL_APP_ID", raising=False)
    assert seneschal_token._get_app_id() == seneschal_token._DEFAULT_APP_ID


def test_app_id_ignores_bad_env_value(monkeypatch):
    monkeypatch.setenv("SENESCHAL_APP_ID", "not-a-number")
    assert seneschal_token._get_app_id() == seneschal_token._DEFAULT_APP_ID


# --------------------------------------------------------------------------
# CLI main — exit-code mapping for each exception.
# --------------------------------------------------------------------------


def test_cli_main_prints_token_on_success(monkeypatch, capsys):
    monkeypatch.setattr(
        seneschal_token, "mint_installation_token", lambda slug: "ghs_xyz"
    )
    rc = seneschal_token.main(["prog", "owner/repo"])
    out = capsys.readouterr()
    assert rc == 0
    assert out.out.strip() == "ghs_xyz"


def test_cli_main_exits_2_on_app_not_installed(monkeypatch, capsys):
    def _raise(slug):
        raise seneschal_token.AppNotInstalledError(slug)

    monkeypatch.setattr(seneschal_token, "mint_installation_token", _raise)
    with pytest.raises(SystemExit) as exc:
        seneschal_token.main(["prog", "owner/repo"])
    assert exc.value.code == 2


def test_cli_main_exits_1_on_mint_error(monkeypatch, capsys):
    def _raise(slug):
        raise seneschal_token.TokenMintError("boom")

    monkeypatch.setattr(seneschal_token, "mint_installation_token", _raise)
    with pytest.raises(SystemExit) as exc:
        seneschal_token.main(["prog", "owner/repo"])
    assert exc.value.code == 1


def test_cli_main_usage_error_on_bad_args(capsys):
    with pytest.raises(SystemExit) as exc:
        seneschal_token.main(["prog"])
    assert exc.value.code != 0
