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


# ---------------------------------------------------------------------------
# v0.8 — MCP wrapper must wire validate_tool_params
#
# Regression guard: prior to v0.8, validate_tool_params() was tested in
# isolation only (see tests/test_basic.py). Nothing asserted that the
# MCP wrappers built by build_server() actually invoke it. A refactor
# that dropped the validate_tool_params() call from a wrapper would
# leave every unit test green while silently disabling input validation.
#
# These tests exercise the full dispatch path via _tool_manager._tools[name].fn
# and would fail if the validate_tool_params() call were removed from the
# corresponding wrapper in build_server().
# ---------------------------------------------------------------------------


def _get_tool_fn(mcp: Any, name: str) -> Any:
    """Return the Python callable registered under ``name`` in the FastMCP tool manager."""
    return mcp._tool_manager._tools[name].fn


def test_agent_send_wrapper_rejects_oversize_message(tmp_path: Path) -> None:
    """build_server().agent_send must call validate_tool_params (65536-byte limit)."""
    from a2a_mcp_bridge.exceptions import MCPValidationError

    db = tmp_path / "bus.sqlite"
    mcp = server_module.build_server(agent_id="alice", db_path=str(db))
    agent_send = _get_tool_fn(mcp, "agent_send")

    with pytest.raises(MCPValidationError, match="65536"):
        agent_send(target="bob", message="x" * 65537)


def test_agent_send_wrapper_rejects_bad_target(tmp_path: Path) -> None:
    """build_server().agent_send must reject invalid agent_id before hitting the store."""
    from a2a_mcp_bridge.exceptions import MCPValidationError

    db = tmp_path / "bus.sqlite"
    mcp = server_module.build_server(agent_id="alice", db_path=str(db))
    agent_send = _get_tool_fn(mcp, "agent_send")

    with pytest.raises(MCPValidationError, match="target"):
        agent_send(target="BAD ID!", message="hi")


def test_agent_fetch_file_wrapper_rejects_empty_transfer_id(tmp_path: Path) -> None:
    """build_server().agent_fetch_file must call validate_tool_params."""
    from a2a_mcp_bridge.exceptions import MCPValidationError

    db = tmp_path / "bus.sqlite"
    mcp = server_module.build_server(agent_id="alice", db_path=str(db))
    agent_fetch_file = _get_tool_fn(mcp, "agent_fetch_file")

    with pytest.raises(MCPValidationError, match="transfer_id"):
        agent_fetch_file(transfer_id="")


def test_agent_delete_file_wrapper_rejects_empty_transfer_id(tmp_path: Path) -> None:
    """build_server().agent_delete_file must call validate_tool_params."""
    from a2a_mcp_bridge.exceptions import MCPValidationError

    db = tmp_path / "bus.sqlite"
    mcp = server_module.build_server(agent_id="alice", db_path=str(db))
    agent_delete_file = _get_tool_fn(mcp, "agent_delete_file")

    with pytest.raises(MCPValidationError, match="transfer_id"):
        agent_delete_file(transfer_id="")


def test_agent_send_file_wrapper_rejects_missing_file_path(tmp_path: Path) -> None:
    """build_server().agent_send_file must call validate_tool_params."""
    from a2a_mcp_bridge.exceptions import MCPValidationError

    db = tmp_path / "bus.sqlite"
    mcp = server_module.build_server(agent_id="alice", db_path=str(db))
    agent_send_file = _get_tool_fn(mcp, "agent_send_file")

    with pytest.raises(MCPValidationError, match="file_path"):
        agent_send_file(target="bob", file_path="")


# ---------------------------------------------------------------------------
# Capability Registry wrapper tests (v0.8 — pattern 0.7.7)
#
# Regression guard: 28 registry unit tests exercise CapabilityRegistry,
# RegistryQuery and HeartbeatManager in isolation. Nothing previously
# asserted that build_server() actually wires the 5 capability_* MCP tools
# into those components. A refactor that dropped cap_registry.announce(agent)
# or removed an @mcp.tool() wrapper would leave every unit test green while
# silently disabling capability registration.
# ---------------------------------------------------------------------------


def _valid_agent_payload(agent_id: str = "alice", skill: str = "code-review") -> str:
    """Return a minimal valid AgentInfo JSON payload for announce tests."""
    import json as _json

    return _json.dumps(
        {
            "agent_id": agent_id,
            "name": f"Agent {agent_id}",
            "status": "online",
            "capabilities": [
                {
                    "skill_id": skill,
                    "description": f"{skill} capability",
                    "domain": "software",
                    "cost": {
                        "tokens_per_call": 1000,
                        "latency_ms": 500,
                        "monetary_cost_usd": 0.01,
                        "type": "local",
                    },
                }
            ],
            "metadata": {},
        }
    )


def test_capability_announce_wrapper_registers_agent(tmp_path: Path) -> None:
    """build_server().capability_announce must persist the agent via cap_registry."""
    db = tmp_path / "bus.sqlite"
    mcp = server_module.build_server(agent_id="alice", db_path=str(db))
    capability_announce = _get_tool_fn(mcp, "capability_announce")

    result = capability_announce(payload=_valid_agent_payload("alice", "python"))

    assert result == {"status": "ok", "registered": 1}


def test_capability_announce_wrapper_rejects_bad_payload(tmp_path: Path) -> None:
    """capability_announce must return a structured error for invalid JSON."""
    db = tmp_path / "bus.sqlite"
    mcp = server_module.build_server(agent_id="alice", db_path=str(db))
    capability_announce = _get_tool_fn(mcp, "capability_announce")

    result = capability_announce(payload="not-valid-json")

    assert result["status"] == "error"
    assert "message" in result


def test_capability_query_wrapper_returns_matching_agents(tmp_path: Path) -> None:
    """capability_query must filter registered agents by keyword."""
    db = tmp_path / "bus.sqlite"
    mcp = server_module.build_server(agent_id="alice", db_path=str(db))
    announce = _get_tool_fn(mcp, "capability_announce")
    query = _get_tool_fn(mcp, "capability_query")

    announce(payload=_valid_agent_payload("alice", "python-review"))
    announce(payload=_valid_agent_payload("bob", "rust-review"))

    result = query(keyword="python")

    assert result["count"] == 1
    assert result["agents"][0]["agent_id"] == "alice"


def test_capability_discover_wrapper_lists_all_capabilities(tmp_path: Path) -> None:
    """capability_discover must return all capabilities across registered agents."""
    db = tmp_path / "bus.sqlite"
    mcp = server_module.build_server(agent_id="alice", db_path=str(db))
    announce = _get_tool_fn(mcp, "capability_announce")
    discover = _get_tool_fn(mcp, "capability_discover")

    announce(payload=_valid_agent_payload("alice", "skill-a"))
    announce(payload=_valid_agent_payload("bob", "skill-b"))

    result = discover()

    assert result["status"] == "success"
    assert result["total_agents"] == 2
    assert len(result["capabilities"]) == 2


def test_capability_find_best_wrapper_returns_matches(tmp_path: Path) -> None:
    """capability_find_best must return scored matches for the skill keyword."""
    db = tmp_path / "bus.sqlite"
    mcp = server_module.build_server(agent_id="alice", db_path=str(db))
    announce = _get_tool_fn(mcp, "capability_announce")
    find_best = _get_tool_fn(mcp, "capability_find_best")

    announce(payload=_valid_agent_payload("alice", "code-review-python"))

    result = find_best(skill="python")

    assert result["status"] == "success"
    assert result["count"] == 1


def test_capability_ping_wrapper_records_heartbeat(tmp_path: Path) -> None:
    """capability_ping must delegate to HeartbeatManager.ping."""
    db = tmp_path / "bus.sqlite"
    mcp = server_module.build_server(agent_id="alice", db_path=str(db))
    ping = _get_tool_fn(mcp, "capability_ping")

    result = ping(agent_id="alice")

    assert result == {"status": "ok", "agent_id": "alice"}
