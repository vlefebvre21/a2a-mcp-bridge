"""Tests for the optional ``session_id`` metadata convention (ADR-001 §4 #2).

Contract:
  * ``agent_send`` recognises a ``session_id`` key inside the opaque
    ``metadata`` dict. No new tool parameter.
  * The value must be a string whose UTF-8 encoding is ≤ 128 bytes —
    otherwise the send is rejected with ``SESSION_ID_INVALID`` or
    ``SESSION_ID_TOO_LARGE``. The limit is enforced in BYTES (not chars)
    so UTF-8 multi-byte characters (emoji, CJK) cannot sneak past.
  * On success, ``sender_session_id`` is stored in the dedicated SQLite
    column AND surfaced at the top level of the payload returned by
    ``agent_inbox`` / ``agent_inbox_peek``.
  * Absence of the key (or ``session_id=None``) leaves the column NULL and
    the payload key at ``None`` — nothing breaks for pre-v0.5 callers.
  * The rest of the metadata dict is preserved for opaque forwarding.
"""

from __future__ import annotations

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


class TestSessionIdPropagation:
    def test_session_id_propagates_to_inbox_payload(self, store: Store) -> None:
        _register(store, "alice", "bob")
        result = tool_agent_send(
            store,
            "alice",
            target="bob",
            message="hi",
            metadata={"session_id": "sess-abc-123", "topic": "hello"},
        )
        assert "error" not in result

        inbox = tool_agent_inbox(store, "bob")
        assert len(inbox["messages"]) == 1
        msg = inbox["messages"][0]
        assert msg["sender_session_id"] == "sess-abc-123"
        # The session_id is preserved in metadata too (opaque forwarding)
        assert msg["metadata"] == {"session_id": "sess-abc-123", "topic": "hello"}

    def test_session_id_propagates_to_peek_payload(self, store: Store) -> None:
        _register(store, "alice", "bob")
        tool_agent_send(
            store,
            "alice",
            target="bob",
            message="hi",
            metadata={"session_id": "peek-session-42"},
        )
        peek = tool_agent_inbox_peek(store, "bob")
        assert len(peek["messages"]) == 1
        assert peek["messages"][0]["sender_session_id"] == "peek-session-42"

    def test_no_session_id_leaves_column_and_payload_null(self, store: Store) -> None:
        _register(store, "alice", "bob")
        # No metadata at all
        tool_agent_send(store, "alice", target="bob", message="no-meta")
        # Metadata without session_id
        tool_agent_send(
            store,
            "alice",
            target="bob",
            message="no-session",
            metadata={"topic": "other"},
        )
        # Explicit session_id=None
        tool_agent_send(
            store,
            "alice",
            target="bob",
            message="null-session",
            metadata={"session_id": None, "other": "k"},
        )

        inbox = tool_agent_inbox(store, "bob", unread_only=True, limit=10)
        assert len(inbox["messages"]) == 3
        assert all(m["sender_session_id"] is None for m in inbox["messages"])


class TestSessionIdValidation:
    def test_rejects_non_string_session_id(self, store: Store) -> None:
        _register(store, "alice", "bob")
        result = tool_agent_send(
            store,
            "alice",
            target="bob",
            message="x",
            metadata={"session_id": 12345},
        )
        assert result.get("error", {}).get("code") == "SESSION_ID_INVALID"

    def test_rejects_too_long_session_id(self, store: Store) -> None:
        _register(store, "alice", "bob")
        too_long = "a" * 129  # 129 ASCII bytes
        result = tool_agent_send(
            store,
            "alice",
            target="bob",
            message="x",
            metadata={"session_id": too_long},
        )
        assert result.get("error", {}).get("code") == "SESSION_ID_TOO_LARGE"

    def test_rejects_utf8_that_exceeds_bytes_limit(self, store: Store) -> None:
        """GLM review nit #1 regression guard.

        The limit is enforced in BYTES (len(raw.encode("utf-8"))), not
        characters. A string of 33 4-byte emoji is 33 chars but 132 bytes,
        so it must be rejected even though len() = 33 ≤ 128.
        """
        _register(store, "alice", "bob")
        emoji_33 = "🎯" * 33  # 33 chars, 132 bytes in UTF-8
        assert len(emoji_33) == 33
        assert len(emoji_33.encode("utf-8")) == 132
        result = tool_agent_send(
            store,
            "alice",
            target="bob",
            message="x",
            metadata={"session_id": emoji_33},
        )
        assert result.get("error", {}).get("code") == "SESSION_ID_TOO_LARGE"

    def test_accepts_exactly_max_length(self, store: Store) -> None:
        _register(store, "alice", "bob")
        max_id = "a" * 128  # 128 ASCII bytes = 128 bytes
        result = tool_agent_send(
            store,
            "alice",
            target="bob",
            message="x",
            metadata={"session_id": max_id},
        )
        assert "error" not in result

        inbox = tool_agent_inbox(store, "bob")
        assert inbox["messages"][0]["sender_session_id"] == max_id

    def test_accepts_empty_string(self, store: Store) -> None:
        """Empty string is a valid (but pointless) session_id — don't reject it."""
        _register(store, "alice", "bob")
        result = tool_agent_send(
            store,
            "alice",
            target="bob",
            message="x",
            metadata={"session_id": ""},
        )
        assert "error" not in result
        inbox = tool_agent_inbox(store, "bob")
        assert inbox["messages"][0]["sender_session_id"] == ""


class TestSessionIdBackwardCompat:
    def test_existing_inbox_shape_unchanged_without_session_id(
        self, store: Store
    ) -> None:
        """Pre-v0.5 callers see ``sender_session_id: None`` and that's it."""
        _register(store, "alice", "bob")
        tool_agent_send(store, "alice", target="bob", message="hi")

        inbox = tool_agent_inbox(store, "bob")
        m = inbox["messages"][0]
        expected_keys = {
            "message_id",
            "sender",
            "body",
            "metadata",
            "sent_at",
            "read_at",
            "sender_session_id",
        }
        assert set(m.keys()) == expected_keys
        assert m["sender_session_id"] is None
