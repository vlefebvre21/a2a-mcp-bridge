"""Tests for the read-only ``agent_inbox_peek`` primitive (ADR-001 §4 #1).

Contract (keep in sync with the tool docstring in server.py):
  * peek NEVER mutates ``read_at``
  * peek returns already-read messages alongside unread ones
  * when ``since_ts`` is provided, only messages with ``created_at >= since_ts``
    are returned, sorted ASC by ``created_at``
  * when ``since_ts`` is omitted, returns the ``limit`` most recent messages
    sorted DESC by ``created_at`` (newest first)
  * ``limit`` is clamped to ``[1, 200]``
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from a2a_mcp_bridge.store import Store
from a2a_mcp_bridge.tools import (
    tool_agent_inbox,
    tool_agent_inbox_peek,
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


class TestAgentInboxPeekIsReadOnly:
    def test_peek_does_not_mark_read(self, store: Store) -> None:
        _register(store, "alice", "bob")
        tool_agent_send(store, "alice", target="bob", message="hi-1")
        tool_agent_send(store, "alice", target="bob", message="hi-2")

        # Peek twice — nothing should change in read_at
        peek1 = tool_agent_inbox_peek(store, "bob")
        peek2 = tool_agent_inbox_peek(store, "bob")

        assert len(peek1["messages"]) == 2
        assert len(peek2["messages"]) == 2
        assert all(m["read_at"] is None for m in peek1["messages"])
        assert all(m["read_at"] is None for m in peek2["messages"])

        # And a subsequent unread-only inbox call still sees both messages
        inbox = tool_agent_inbox(store, "bob", unread_only=True)
        assert len(inbox["messages"]) == 2

    def test_peek_includes_already_read_messages_with_read_at(
        self, store: Store
    ) -> None:
        _register(store, "alice", "bob")
        tool_agent_send(store, "alice", target="bob", message="early")
        tool_agent_send(store, "alice", target="bob", message="late")

        # Consume both via the real inbox
        inbox = tool_agent_inbox(store, "bob", unread_only=True)
        assert len(inbox["messages"]) == 2

        # Peek must still see both, with read_at populated
        peek = tool_agent_inbox_peek(store, "bob")
        assert len(peek["messages"]) == 2
        assert all(m["read_at"] is not None for m in peek["messages"])


class TestAgentInboxPeekSinceFilter:
    def test_since_ts_filters_messages_asc(self, store: Store) -> None:
        _register(store, "alice", "bob")
        tool_agent_send(store, "alice", target="bob", message="old-1")
        tool_agent_send(store, "alice", target="bob", message="old-2")

        # Sleep enough that created_at is strictly later than the boundary
        # (ISO string compare is lexicographic but also chronological for
        # fixed-offset UTC isoformat — sleeping 10ms is plenty for SQLite
        # to record a later timestamp).
        time.sleep(0.02)
        boundary = datetime.now(UTC).isoformat()
        time.sleep(0.02)

        tool_agent_send(store, "alice", target="bob", message="new-1")
        tool_agent_send(store, "alice", target="bob", message="new-2")

        peek = tool_agent_inbox_peek(store, "bob", since_ts=boundary)
        bodies = [m["body"] for m in peek["messages"]]
        # Only post-boundary messages, in ASC order
        assert bodies == ["new-1", "new-2"]

    def test_since_ts_in_future_returns_empty(self, store: Store) -> None:
        _register(store, "alice", "bob")
        tool_agent_send(store, "alice", target="bob", message="now")
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        peek = tool_agent_inbox_peek(store, "bob", since_ts=future)
        assert peek["messages"] == []

    def test_since_ts_none_returns_latest_desc(self, store: Store) -> None:
        _register(store, "alice", "bob")
        for i in range(5):
            tool_agent_send(store, "alice", target="bob", message=f"m{i}")
            time.sleep(0.005)

        peek = tool_agent_inbox_peek(store, "bob", limit=3)
        # Newest first, only the 3 most recent
        bodies = [m["body"] for m in peek["messages"]]
        assert bodies == ["m4", "m3", "m2"]


class TestAgentInboxPeekIsolation:
    def test_peek_only_shows_messages_addressed_to_caller(self, store: Store) -> None:
        _register(store, "alice", "bob", "carol")
        tool_agent_send(store, "alice", target="bob", message="for-bob")
        tool_agent_send(store, "alice", target="carol", message="for-carol")

        peek_bob = tool_agent_inbox_peek(store, "bob")
        peek_carol = tool_agent_inbox_peek(store, "carol")

        assert [m["body"] for m in peek_bob["messages"]] == ["for-bob"]
        assert [m["body"] for m in peek_carol["messages"]] == ["for-carol"]

    def test_peek_respects_limit_clamp(self, store: Store) -> None:
        _register(store, "alice", "bob")
        tool_agent_send(store, "alice", target="bob", message="only-one")

        # limit=0 → clamped to 1 (at least one message returned)
        peek_zero = tool_agent_inbox_peek(store, "bob", limit=0)
        assert len(peek_zero["messages"]) == 1

        # limit=9999 → accepted (clamped to 200 internally, but we have 1 msg)
        peek_huge = tool_agent_inbox_peek(store, "bob", limit=9999)
        assert len(peek_huge["messages"]) == 1


class TestAgentInboxPeekPayloadShape:
    def test_payload_shape_matches_inbox(self, store: Store) -> None:
        _register(store, "alice", "bob")
        tool_agent_send(
            store,
            "alice",
            target="bob",
            message="shape",
            metadata={"topic": "ping"},
        )

        peek = tool_agent_inbox_peek(store, "bob")
        assert len(peek["messages"]) == 1
        m = peek["messages"][0]
        # Same keys as agent_inbox's serialisation (now includes
        # sender_session_id + intent — ADR-001/002).
        assert set(m.keys()) == {
            "message_id",
            "sender",
            "body",
            "metadata",
            "sent_at",
            "read_at",
            "sender_session_id",
            "intent",
        }
        assert m["sender"] == "alice"
        assert m["metadata"] == {"topic": "ping"}
        assert m["read_at"] is None  # peek doesn't mark read
        assert m["sender_session_id"] is None  # none provided on send
