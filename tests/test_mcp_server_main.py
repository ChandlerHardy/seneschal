"""Tests for `mcp_server.server.main` transport dispatch.

The `main` entry point chooses between stdio (the original MCP transport
local Claude Code uses via `bin/seneschal-mcp-server`) and HTTP (used by
the OCI deployment so a remote Claude Code on the tailnet can connect).

Selection logic:
  * `--http` CLI flag forces HTTP.
  * Otherwise `SENESCHAL_MCP_TRANSPORT=http` env var forces HTTP.
  * Otherwise default is stdio.

When HTTP is selected, host/port/path resolve in order:
  1. CLI args (`--host`, `--port`, `--path`)
  2. Env vars (`SENESCHAL_MCP_HOST`, `SENESCHAL_MCP_PORT`, `SENESCHAL_MCP_PATH`)
  3. Built-in defaults (`127.0.0.1`, `9101`, `/mcp`)

The 127.0.0.1 default is intentional: an operator copying the unit file
without setting `SENESCHAL_MCP_HOST` ends up with a loopback bind, not
an open public listener. The OCI systemd unit explicitly sets the
Tailscale IP.
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_server import server  # noqa: E402


def _clear_env(monkeypatch):
    for k in (
        "SENESCHAL_MCP_TRANSPORT",
        "SENESCHAL_MCP_HOST",
        "SENESCHAL_MCP_PORT",
        "SENESCHAL_MCP_PATH",
    ):
        monkeypatch.delenv(k, raising=False)


def test_main_defaults_to_stdio(monkeypatch):
    _clear_env(monkeypatch)
    with patch.object(server.mcp, "run") as mock_run:
        server.main([])
    mock_run.assert_called_once_with()


def test_main_http_flag_uses_built_in_defaults(monkeypatch):
    _clear_env(monkeypatch)
    with patch.object(server.mcp, "run") as mock_run:
        server.main(["--http"])
    mock_run.assert_called_once_with(
        transport="http", host="127.0.0.1", port=9101, path="/mcp"
    )


def test_main_env_var_selects_http(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SENESCHAL_MCP_TRANSPORT", "http")
    monkeypatch.setenv("SENESCHAL_MCP_HOST", "100.120.165.66")
    monkeypatch.setenv("SENESCHAL_MCP_PORT", "9101")
    with patch.object(server.mcp, "run") as mock_run:
        server.main([])
    mock_run.assert_called_once_with(
        transport="http", host="100.120.165.66", port=9101, path="/mcp"
    )


def test_main_cli_args_override_env(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SENESCHAL_MCP_HOST", "100.120.165.66")
    monkeypatch.setenv("SENESCHAL_MCP_PORT", "9999")
    monkeypatch.setenv("SENESCHAL_MCP_PATH", "/from-env")
    with patch.object(server.mcp, "run") as mock_run:
        server.main(["--http", "--host", "0.0.0.0", "--port", "8080", "--path", "/x"])
    mock_run.assert_called_once_with(
        transport="http", host="0.0.0.0", port=8080, path="/x"
    )


def test_main_http_flag_overrides_stdio_env(monkeypatch):
    """`--http` wins even when SENESCHAL_MCP_TRANSPORT is unset/stdio."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("SENESCHAL_MCP_TRANSPORT", "stdio")
    with patch.object(server.mcp, "run") as mock_run:
        server.main(["--http"])
    mock_run.assert_called_once_with(
        transport="http", host="127.0.0.1", port=9101, path="/mcp"
    )


def test_main_unknown_transport_env_falls_back_to_stdio(monkeypatch):
    """An invalid SENESCHAL_MCP_TRANSPORT value should not crash; we treat
    anything other than 'http' as stdio so a typo can't accidentally open
    a network listener."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("SENESCHAL_MCP_TRANSPORT", "websocket-typo")
    with patch.object(server.mcp, "run") as mock_run:
        server.main([])
    mock_run.assert_called_once_with()
