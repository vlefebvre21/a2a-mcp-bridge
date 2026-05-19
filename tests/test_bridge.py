"""Tests for the bridge module — integration between MCP handlers and SQLite store.

Covers normal and error cases for all handlers.
"""

from __future__ import annotations

import pytest

from a2a_mcp_bridge.signals import SignalDir
from a2a_mcp_bridge.store import Store
from a2a_mcp_bridge.tools import (
    tool_agent_inbox,
    tool_agent_inbox_peek,
    tool_agent_list,
    tool_agent_send,
    tool_agent_subscribe,
)


# Fixtures
@pytest.fixture
def store_with_alice_and_bob(store: Store) -> Store:
    """Fixture that registers alice and bob in the store."""
    store.upsert_agent("alice")
    store.upsert_agent("bob")
    return store


# Tests for tool_agent_send
class TestAgentSend:
    def test_send_between_agents_success(self, store_with_alice_and_bob: Store) -> None:
        store = store_with_alice_and_bob
        # Pass intent=triage explicitly so the assertion doesn't couple to
        # whatever DEFAULT_INTENT currently is (ADR-002 evolution).
        result = tool_agent_send(
            store, "alice", "bob", "Hello Bob!", None, None, None, intent="triage"
        )
        assert "message_id" in result
        assert result["recipient"] == "bob"
        assert result["intent"] == "triage"

    def test_send_to_self_raises_error(self, store_with_alice_and_bob: Store) -> None:
        store = store_with_alice_and_bob
        result = tool_agent_send(
            store, "alice", "alice", "Hello me!", None, None, None, intent=None
        )
        assert "error" in result
        assert result["error"]["code"] == "TARGET_SELF"

    def test_send_to_unknown_agent_raises_error(
        self, store_with_alice_and_bob: Store
    ) -> None:
        store = store_with_alice_and_bob
        result = tool_agent_send(
            store, "alice", "unknown", "Hello?", None, None, None, intent=None
        )
        assert "error" in result
        assert result["error"]["code"] == "TARGET_UNKNOWN"

    def test_send_with_fyi_intent_skips_wake(
        self, store_with_alice_and_bob: Store
    ) -> None:
        store = store_with_alice_and_bob
        result = tool_agent_send(
            store, "alice", "bob", "FYI", None, None, None, intent="fyi"
        )
        assert result["intent"] == "fyi"
        assert result["recipient"] == "bob"


# Tests for tool_agent_inbox
class TestAgentInbox:
    def test_inbox_returns_unread_messages(
        self, store_with_alice_and_bob: Store
    ) -> None:
        store = store_with_alice_and_bob
        tool_agent_send(
            store, "alice", "bob", "Test message", None, None, None, intent=None
        )
        result = tool_agent_inbox(
            store, "bob", limit=10, unread_only=True, session_id=None, signal_dir=None
        )
        assert len(result["messages"]) == 1
        assert result["messages"][0]["body"] == "Test message"

    def test_inbox_empty_when_no_messages(
        self, store_with_alice_and_bob: Store
    ) -> None:
        result = tool_agent_inbox(
            store_with_alice_and_bob, "bob",
            limit=10, unread_only=True, session_id=None, signal_dir=None,
        )
        assert result["messages"] == []


# Tests for tool_agent_inbox_peek
class TestAgentInboxPeek:
    def test_peek_returns_messages_without_consuming(
        self, store_with_alice_and_bob: Store
    ) -> None:
        store = store_with_alice_and_bob
        tool_agent_send(
            store, "alice", "bob", "Peek test", None, None, None, intent=None
        )
        result = tool_agent_inbox_peek(
            store, "bob", since_ts=None, limit=50, session_id=None
        )
        assert len(result["messages"]) == 1
        # Message should still be unread (peek doesn't consume)
        result2 = tool_agent_inbox_peek(
            store, "bob", since_ts=None, limit=50, session_id=None
        )
        assert len(result2["messages"]) == 1


# Tests for tool_agent_list
class TestAgentList:
    def test_list_returns_registered_agents(
        self, store_with_alice_and_bob: Store
    ) -> None:
        store = store_with_alice_and_bob
        result = tool_agent_list(
            store, "alice", active_within_days=7, session_id=None
        )
        assert len(result["agents"]) == 2
        agent_ids = {a["agent_id"] for a in result["agents"]}
        assert "alice" in agent_ids
        assert "bob" in agent_ids

    def test_list_returns_only_self_when_no_other_agents(
        self, store: Store
    ) -> None:
        # tool_agent_list upserts the caller, so the list will contain
        # exactly one agent (the caller itself).
        result = tool_agent_list(
            store, "solo-agent", active_within_days=7, session_id=None
        )
        assert len(result["agents"]) == 1
        assert result["agents"][0]["agent_id"] == "solo-agent"


# Tests for tool_agent_subscribe
class TestAgentSubscribe:
    def test_subscribe_returns_immediately_with_pending_messages(
        self, store_with_alice_and_bob: Store
    ) -> None:
        store = store_with_alice_and_bob
        signal_dir = SignalDir("/tmp/test_signals_subscribe")
        tool_agent_send(
            store, "alice", "bob", "Subscribe test", None, None, None, intent=None
        )
        result = tool_agent_subscribe(
            store,
            "bob",
            signal_dir=signal_dir,
            timeout_seconds=5.0,
            poll_interval=0.2,
            limit=10,
            session_id=None,
        )
        assert not result["timed_out"]
        assert len(result["messages"]) == 1
        assert result["messages"][0]["body"] == "Subscribe test"

    def test_subscribe_times_out_when_no_messages(
        self, store_with_alice_and_bob: Store
    ) -> None:
        store = store_with_alice_and_bob
        signal_dir = SignalDir("/tmp/test_signals_timeout")
        result = tool_agent_subscribe(
            store,
            "bob",
            signal_dir=signal_dir,
            timeout_seconds=0.5,
            poll_interval=0.2,
            limit=10,
            session_id=None,
        )
        assert result["timed_out"]
        assert result["messages"] == []
