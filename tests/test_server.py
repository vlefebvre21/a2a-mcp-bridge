"""Tests for the server module (env resolution + build_server)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from a2a_mcp_bridge import server as server_module


def test_resolve_agent_id_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("A2A_AGENT_ID", raising=False)
    with pytest.raises(SystemExit) as exc:
        server_module._resolve_agent_id()
    assert exc.value.code == 2


def test_resolve_agent_id_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A2A_AGENT_ID", "   ")
    with pytest.raises(SystemExit) as exc:
        server_module._resolve_agent_id()
    assert exc.value.code == 2


def test_resolve_agent_id_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A2A_AGENT_ID", "BAD ID!")
    with pytest.raises(SystemExit) as exc:
        server_module._resolve_agent_id()
    assert exc.value.code == 2


def test_resolve_agent_id_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A2A_AGENT_ID", "alice")
    assert server_module._resolve_agent_id() == "alice"


def test_resolve_db_path_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("A2A_DB_PATH", raising=False)
    path = server_module._resolve_db_path()
    assert path.endswith(".a2a-bus.sqlite")
    assert "~" not in path  # expanded


def test_resolve_db_path_custom(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "custom.sqlite"
    monkeypatch.setenv("A2A_DB_PATH", str(target))
    assert server_module._resolve_db_path() == str(target)


def test_build_server_registers_agent(tmp_path: Path) -> None:
    """build_server must upsert the agent and expose a FastMCP instance."""
    db = tmp_path / "bus.sqlite"
    mcp = server_module.build_server(agent_id="alice", db_path=str(db))
    assert mcp is not None
    # Verify agent was registered
    from a2a_mcp_bridge.store import Store

    store = Store(str(db))
    agents = store.list_agents(7)
    assert any(a.agent_id == "alice" for a in agents)


def test_main_exits_without_agent_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("A2A_AGENT_ID", raising=False)
    with pytest.raises(SystemExit) as exc:
        server_module.main()
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# v0.4 — tools.listChanged capability (Option A, future-proof) + agent_ping
# ---------------------------------------------------------------------------


def test_bridge_version_returns_string() -> None:
    """_bridge_version always returns a non-empty string (installed or 'unknown')."""
    v = server_module._bridge_version()
    assert isinstance(v, str)
    assert v  # never empty


def test_bridge_version_is_cached() -> None:
    """v0.4.1 — _bridge_version must be lru_cache'd to avoid repeated metadata scans."""
    from unittest.mock import patch

    # Clear the cache so the patched _pkg_version is actually observed
    server_module._bridge_version.cache_clear()
    with patch(
        "a2a_mcp_bridge.server._pkg_version", return_value="9.9.9-test"
    ) as spy:
        first = server_module._bridge_version()
        second = server_module._bridge_version()
        third = server_module._bridge_version()
    assert first == second == third == "9.9.9-test"
    # Cached: exactly one underlying lookup despite three calls
    assert spy.call_count == 1
    # Restore the real lookup for subsequent tests
    server_module._bridge_version.cache_clear()


def test_a2amcp_advertises_tools_changed_capability(tmp_path: Path) -> None:
    """The server handshake must declare tools.listChanged=True.

    Even though we don't dynamically add/remove tools today, declaring the
    capability lets clients subscribe to future updates without restart.
    """
    db = tmp_path / "bus.sqlite"
    mcp = server_module.build_server(agent_id="alice", db_path=str(db))
    # FastMCP's internal low-level server is at _mcp_server
    init_opts = mcp._mcp_server.create_initialization_options(
        notification_options=server_module.NotificationOptions(tools_changed=True),
    )
    caps = init_opts.capabilities
    assert caps.tools is not None
    assert caps.tools.listChanged is True


def test_agent_ping_returns_version_and_agent_id(tmp_path: Path) -> None:
    """The agent_ping tool must expose version + agent_id for staleness checks."""
    import anyio

    db = tmp_path / "bus.sqlite"
    mcp = server_module.build_server(agent_id="alice", db_path=str(db))

    # FastMCP exposes call_tool() to invoke registered tools by name
    async def _invoke() -> Any:
        return await mcp.call_tool("agent_ping", {})

    result = anyio.run(_invoke)
    # FastMCP.call_tool returns (content, structured) tuple in recent versions;
    # fall back to a dict-like shape for older versions.
    payload: dict[str, Any]
    if isinstance(result, tuple):
        # (content_list, structured_output) — we want structured_output
        payload = result[1] if len(result) > 1 and isinstance(result[1], dict) else {}
        if not payload and result[0]:
            # Older shape: list[TextContent] with JSON body
            import json

            first = result[0][0]
            text = getattr(first, "text", None)
            if text:
                payload = json.loads(text)
    elif isinstance(result, dict):
        payload = result
    else:
        payload = {}

    assert payload.get("server") == "a2a-mcp-bridge"
    assert payload.get("agent_id") == "alice"
    assert isinstance(payload.get("version"), str) and payload["version"]


def test_build_server_registers_agent_ping_tool(tmp_path: Path) -> None:
    """agent_ping must be listed among the registered MCP tools."""
    import anyio

    db = tmp_path / "bus.sqlite"
    mcp = server_module.build_server(agent_id="alice", db_path=str(db))

    async def _list() -> Any:
        return await mcp.list_tools()

    tools = anyio.run(_list)
    names = {t.name for t in tools}
    assert "agent_ping" in names
    # Sanity: the v0.1-v0.3 tools are still there
    assert {"agent_send", "agent_inbox", "agent_list", "agent_subscribe"} <= names
