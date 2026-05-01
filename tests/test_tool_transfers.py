"""Tests for tool_agent_send_file / fetch_file / delete_file."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from a2a_mcp_bridge.store import Store


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    s = Store(str(tmp_path / "bus.db"))
    s.init_schema()
    s.upsert_agent("alice")
    s.upsert_agent("bob")
    return s


def test_tool_agent_send_file_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: Store) -> None:
    monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path / "xfer"))
    src = tmp_path / "report.md"
    src.write_text("# Report\n" * 100)

    from a2a_mcp_bridge.tools import tool_agent_send_file

    result = tool_agent_send_file(
        store, caller_id="alice", target="bob",
        file_path=str(src), description="weekly report",
    )
    assert "error" not in result
    assert result["transfer_id"]
    assert result["size"] > 0
    assert result["sha256"]

    # Inbox message body is the ADR-007 JSON
    msgs = store.read_inbox("bob")
    assert len(msgs) == 1
    body = json.loads(msgs[0].body)
    assert body["kind"] == "file_transfer"
    assert body["version"] == 1
    assert body["transfer_id"] == result["transfer_id"]
    assert body["locator"]["scheme"] == "file"


def test_tool_agent_fetch_file_returns_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: Store) -> None:
    monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path / "xfer"))
    src = tmp_path / "r.md"
    src.write_text("hello")

    from a2a_mcp_bridge.tools import tool_agent_fetch_file, tool_agent_send_file

    sent = tool_agent_send_file(store, caller_id="alice", target="bob", file_path=str(src))
    got = tool_agent_fetch_file(store, caller_id="bob", transfer_id=sent["transfer_id"])
    assert got["filename"] == "r.md"
    assert Path(got["path"]).read_text() == "hello"


def test_tool_agent_delete_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: Store) -> None:
    monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path / "xfer"))
    src = tmp_path / "r.md"
    src.write_text("hi")

    from a2a_mcp_bridge.tools import (
        tool_agent_delete_file,
        tool_agent_fetch_file,
        tool_agent_send_file,
    )

    sent = tool_agent_send_file(store, caller_id="alice", target="bob", file_path=str(src))
    tool_agent_delete_file(store, caller_id="bob", transfer_id=sent["transfer_id"])

    res = tool_agent_fetch_file(store, caller_id="bob", transfer_id=sent["transfer_id"])
    assert "error" in res
