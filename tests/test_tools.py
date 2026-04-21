"""Unit tests for the tool-layer functions (no MCP transport, pure Python calls)."""

from pathlib import Path

import pytest

from a2a_mcp_bridge.store import Store
from a2a_mcp_bridge.tools import tool_agent_inbox, tool_agent_list, tool_agent_send


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(str(tmp_path / "bus.sqlite"))
    s.init_schema()
    return s


def test_send_then_inbox_roundtrip(store: Store) -> None:
    # Both agents register by calling any tool
    tool_agent_list(store, "alice")
    tool_agent_list(store, "bob")

    result = tool_agent_send(store, "alice", target="bob", message="hi")
    assert "error" not in result
    assert result["recipient"] == "bob"

    inbox = tool_agent_inbox(store, "bob")
    assert len(inbox["messages"]) == 1
    assert inbox["messages"][0]["body"] == "hi"


def test_send_to_unknown_returns_error(store: Store) -> None:
    tool_agent_list(store, "alice")
    result = tool_agent_send(store, "alice", target="ghost", message="x")
    assert result["error"]["code"] == "TARGET_UNKNOWN"


def test_send_self_returns_error(store: Store) -> None:
    tool_agent_list(store, "alice")
    result = tool_agent_send(store, "alice", target="alice", message="x")
    assert result["error"]["code"] == "TARGET_SELF"


def test_agent_list_includes_caller(store: Store) -> None:
    result = tool_agent_list(store, "alice")
    ids = [a["agent_id"] for a in result["agents"]]
    assert "alice" in ids
