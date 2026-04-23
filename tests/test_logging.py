"""Tests for session-tagged structured logging (ADR-001 §4 #3, v0.5).

The bridge must emit one log record per tool call with a minimum schema:
``{ts, level, event, agent_id}``, plus ``session_id`` when provided. Bodies
and caller metadata are never logged verbatim — only a short
:func:`hash_body` digest goes into ``body_hash``.

Toggling is done via ``A2A_LOG_JSON=1`` but is evaluated at import time, so
we don't flip it live in tests; we test the two code paths independently:

* the ``log_event`` helper's output under both modes (pure unit)
* the integration flow (tool handlers emit the expected event names and
  carry the session_id when provided)
"""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path

import pytest

from a2a_mcp_bridge import logging_ext
from a2a_mcp_bridge.logging_ext import hash_body
from a2a_mcp_bridge.store import Store
from a2a_mcp_bridge.tools import (
    tool_agent_inbox,
    tool_agent_inbox_peek,
    tool_agent_list,
    tool_agent_send,
)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(str(tmp_path / "bus.sqlite"))
    s.init_schema()
    return s


def _register(store: Store, *agents: str) -> None:
    for a in agents:
        store.upsert_agent(a)


class TestHashBody:
    def test_hash_body_is_stable_and_short(self) -> None:
        assert hash_body("hello") == hash_body("hello")
        assert len(hash_body("hello")) == 16  # 8-byte blake2b → 16 hex chars

    def test_hash_body_handles_none_and_bytes(self) -> None:
        assert hash_body(None) is None
        assert hash_body(b"raw") == hash_body("raw")

    def test_hash_body_distinguishes_bodies(self) -> None:
        assert hash_body("one") != hash_body("two")


class TestLogEventPlainText:
    def test_plain_text_emits_event_and_kv_pairs(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Force plain-text mode (default) — reimport to re-read env
        monkeypatch.setenv("A2A_LOG_JSON", "")
        importlib.reload(logging_ext)

        caplog.set_level(logging.INFO, logger="a2a_mcp_bridge.tests")
        test_logger = logging.getLogger("a2a_mcp_bridge.tests")
        logging_ext.log_event(
            test_logger,
            event="tool.test",
            agent_id="alice",
            session_id="s1",
            target="bob",
            count=3,
        )
        records = [r for r in caplog.records if r.name == "a2a_mcp_bridge.tests"]
        assert len(records) == 1
        msg = records[0].getMessage()
        assert msg.startswith("tool.test ")
        assert "[session=s1]" in msg
        assert "target=bob" in msg
        assert "count=3" in msg

    def test_plain_text_drops_none_fields(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("A2A_LOG_JSON", "")
        importlib.reload(logging_ext)
        caplog.set_level(logging.INFO, logger="a2a_mcp_bridge.tests")
        test_logger = logging.getLogger("a2a_mcp_bridge.tests")
        logging_ext.log_event(
            test_logger,
            event="tool.test",
            agent_id="alice",
            session_id=None,  # absent from output
            count=0,  # present — 0 is not None
            body_hash=None,  # absent
        )
        msg = [r for r in caplog.records if r.name == "a2a_mcp_bridge.tests"][
            -1
        ].getMessage()
        assert "session=" not in msg
        assert "body_hash" not in msg
        assert "count=0" in msg


class TestLogEventJson:
    def test_json_mode_emits_object_per_line(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("A2A_LOG_JSON", "1")
        importlib.reload(logging_ext)

        caplog.set_level(logging.INFO, logger="a2a_mcp_bridge.tests")
        test_logger = logging.getLogger("a2a_mcp_bridge.tests")
        logging_ext.log_event(
            test_logger,
            event="tool.test",
            agent_id="alice",
            session_id="s1",
            message_id="abcd",
            duration_ms=4.2,
        )
        records = [r for r in caplog.records if r.name == "a2a_mcp_bridge.tests"]
        assert len(records) == 1
        payload = json.loads(records[0].getMessage())

        # Minimum schema always present
        for k in ("ts", "level", "event", "agent_id"):
            assert k in payload
        assert payload["event"] == "tool.test"
        assert payload["agent_id"] == "alice"
        assert payload["session_id"] == "s1"
        assert payload["message_id"] == "abcd"
        assert payload["duration_ms"] == 4.2

        # Reset back to default for other tests
        monkeypatch.setenv("A2A_LOG_JSON", "")
        importlib.reload(logging_ext)


class TestToolIntegration:
    def test_agent_send_logs_event_with_session_id(
        self, store: Store, caplog: pytest.LogCaptureFixture
    ) -> None:
        _register(store, "alice", "bob")
        caplog.set_level(logging.INFO, logger="a2a_mcp_bridge.tools")
        result = tool_agent_send(
            store,
            "alice",
            target="bob",
            message="hello",
            metadata={"session_id": "sess-42"},
        )
        assert "error" not in result

        msgs = [
            r.getMessage()
            for r in caplog.records
            if r.name == "a2a_mcp_bridge.tools"
        ]
        # At least one tool.agent_send line carrying the session_id
        assert any(
            "tool.agent_send" in m and "session=sess-42" in m for m in msgs
        )
        # body_hash appears (and is 16 hex chars), body content does not
        assert any(f"body_hash={hash_body('hello')}" in m for m in msgs)
        assert not any("hello" in m for m in msgs)  # body never leaks in a log line

    def test_agent_send_error_logs_warn(
        self, store: Store, caplog: pytest.LogCaptureFixture
    ) -> None:
        _register(store, "alice")
        caplog.set_level(logging.INFO, logger="a2a_mcp_bridge.tools")
        result = tool_agent_send(store, "alice", target="ghost", message="x")
        assert result["error"]["code"] == "TARGET_UNKNOWN"

        records = [r for r in caplog.records if r.name == "a2a_mcp_bridge.tools"]
        err_records = [r for r in records if r.levelno == logging.WARNING]
        assert err_records, "expected a WARNING log record on agent_send error"
        msg = err_records[-1].getMessage()
        assert "tool.agent_send" in msg
        assert "error_code=TARGET_UNKNOWN" in msg

    def test_read_tools_pick_up_session_id_param(
        self, store: Store, caplog: pytest.LogCaptureFixture
    ) -> None:
        _register(store, "alice", "bob")
        caplog.set_level(logging.INFO, logger="a2a_mcp_bridge.tools")

        # exercise each read tool with session_id
        tool_agent_inbox(store, "bob", session_id="s-inbox")
        tool_agent_inbox_peek(store, "bob", session_id="s-peek")
        tool_agent_list(store, "bob", session_id="s-list")

        records = [
            r.getMessage()
            for r in caplog.records
            if r.name == "a2a_mcp_bridge.tools"
        ]
        assert any("tool.agent_inbox " in m and "session=s-inbox" in m for m in records)
        assert any(
            "tool.agent_inbox_peek" in m and "session=s-peek" in m for m in records
        )
        assert any("tool.agent_list" in m and "session=s-list" in m for m in records)

    def test_tool_logs_always_include_duration(
        self, store: Store, caplog: pytest.LogCaptureFixture
    ) -> None:
        _register(store, "alice", "bob")
        caplog.set_level(logging.INFO, logger="a2a_mcp_bridge.tools")
        tool_agent_list(store, "alice")
        msgs = [
            r.getMessage()
            for r in caplog.records
            if r.name == "a2a_mcp_bridge.tools" and "tool.agent_list" in r.getMessage()
        ]
        assert any("duration_ms=" in m for m in msgs)
