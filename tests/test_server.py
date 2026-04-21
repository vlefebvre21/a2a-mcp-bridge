"""Tests for the server module (env resolution + build_server)."""

from __future__ import annotations

from pathlib import Path

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
