"""Tests for ADR-002 intent field implementation."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from a2a_mcp_bridge.intents import normalize_intent, wakes
from a2a_mcp_bridge.store import Store
from a2a_mcp_bridge.tools import tool_agent_inbox, tool_agent_send
from a2a_mcp_bridge.wake import WebhookWaker

# ---------------------------------------------------------------------------
# Test 1: normalize_intent behaviour
# ---------------------------------------------------------------------------

def test_normalize_intent_none_returns_default() -> None:
    assert normalize_intent(None) == ("triage", False)
    assert normalize_intent("fyi") == ("fyi", False)
    assert normalize_intent("execute") == ("execute", False)
    assert normalize_intent("bogus-xyz") == ("triage", True)


# ---------------------------------------------------------------------------
# Test 2: wakes helper
# ---------------------------------------------------------------------------

def test_wakes_helper() -> None:
    assert wakes("triage") is True
    assert wakes("execute") is True
    assert wakes("review") is True
    assert wakes("question") is True
    assert wakes("fyi") is False


# ---------------------------------------------------------------------------
# Shared fixture for tool-level tests (#3, #4, #5)
# ---------------------------------------------------------------------------

@pytest.fixture
def _tool_store(tmp_path: Path) -> Store:
    s = Store(str(tmp_path / "bus.sqlite"))
    s.init_schema()
    s.upsert_agent("sender")
    s.upsert_agent("receiver")
    return s


# ---------------------------------------------------------------------------
# Test 3: default intent triggers wake
# ---------------------------------------------------------------------------

def test_send_default_intent_triggers_wake(_tool_store: Store) -> None:
    waker = MagicMock(spec=WebhookWaker)

    result = tool_agent_send(
        _tool_store, "sender", "receiver", "hello", waker=waker
    )

    assert result["intent"] == "triage"
    waker.wake.assert_called_once_with("receiver", sender_id="sender")


# ---------------------------------------------------------------------------
# Test 4: fyi intent skips wake
# ---------------------------------------------------------------------------

def test_send_intent_fyi_skips_wake(_tool_store: Store) -> None:
    waker = MagicMock(spec=WebhookWaker)

    result = tool_agent_send(
        _tool_store, "sender", "receiver", "hello", waker=waker, intent="fyi"
    )

    assert result["intent"] == "fyi"
    waker.wake.assert_not_called()
    assert "message_id" in result


# ---------------------------------------------------------------------------
# Test 5: unknown intent downgrades to triage with WARNING log
# ---------------------------------------------------------------------------

def test_send_unknown_intent_downgrades_to_triage_and_logs_warning(
    _tool_store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    waker = MagicMock(spec=WebhookWaker)

    result = tool_agent_send(
        _tool_store, "sender", "receiver", "hello",
        waker=waker, intent="bogus-xyz"
    )

    assert result["intent"] == "triage"
    waker.wake.assert_called_once()
    assert any("intent_downgraded" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Test 6: migration adds intent column to existing DB
# ---------------------------------------------------------------------------

def test_migration_adds_intent_column_to_existing_db(tmp_path: Path) -> None:
    import sqlite3

    db_path = tmp_path / "old_bus.sqlite"

    # Create OLD schema (without messages.intent column)
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE messages (
            id TEXT PRIMARY KEY, sender_id TEXT NOT NULL, recipient_id TEXT NOT NULL,
            body TEXT NOT NULL, metadata TEXT, created_at TEXT NOT NULL, read_at TEXT,
            sender_session_id TEXT
        );
        CREATE TABLE agents (
            id TEXT PRIMARY KEY, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
            metadata TEXT
        );
    """)
    # Insert some dummy rows
    conn.execute(
        """
        INSERT INTO messages (id, sender_id, recipient_id, body, metadata, created_at, read_at, sender_session_id)
        VALUES ('m1', 'a', 'b', 'old msg1', NULL, '2024-01-01T00:00:00+00:00', NULL, NULL);
        """
    )
    conn.execute(
        """
        INSERT INTO messages (id, sender_id, recipient_id, body, metadata, created_at, read_at, sender_session_id)
        VALUES ('m2', 'a', 'b', 'old msg2', NULL, '2024-01-02T00:00:00+00:00', NULL, NULL);
        """
    )
    conn.execute(
        """
        INSERT INTO agents (id, first_seen_at, last_seen_at, metadata)
        VALUES ('a', '2024-01-01T00:00:00+00:00', '2024-01-01T00:00:00+00:00', NULL);
        """
    )
    conn.execute(
        """
        INSERT INTO agents (id, first_seen_at, last_seen_at, metadata)
        VALUES ('b', '2024-01-01T00:00:00+00:00', '2024-01-01T00:00:00+00:00', NULL);
        """
    )
    conn.commit()
    conn.close()

    # Open with Store — triggers migration
    store = Store(str(db_path))
    store.init_schema()

    # Verify intent column exists (PRAGMA table_info returns tuples: (cid, name, type, notnull, dflt_value, pk))
    pragma_cols = {row[1] for row in store._conn.execute("PRAGMA table_info(messages)").fetchall()}
    assert "intent" in pragma_cols

    # Verify old rows have intent = 'triage'
    rows = store._conn.execute("SELECT id, intent FROM messages").fetchall()
    assert all(r[1] == "triage" for r in rows)

    # Verify new send persists custom intent
    store.send_message("a", "b", "new msg", intent="fyi")
    new_row = store._conn.execute(
        "SELECT intent FROM messages WHERE id = (SELECT id FROM messages ORDER BY created_at DESC LIMIT 1)"
    ).fetchone()
    assert new_row[0] == "fyi"


# ---------------------------------------------------------------------------
# Test 7: agent_inbox returns intent field
# ---------------------------------------------------------------------------

def test_agent_inbox_returns_intent_field(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "bus.sqlite"))
    store.init_schema()
    store.upsert_agent("sender")
    store.upsert_agent("receiver")

    store.send_message("sender", "receiver", "msg1", intent="triage")
    store.send_message("sender", "receiver", "msg2", intent="fyi")

    inbox = tool_agent_inbox(store, "receiver", limit=10, unread_only=False)

    assert len(inbox["messages"]) == 2
    # The inbox returns DESC by created_at, so msg2 is first
    intents = {m["intent"] for m in inbox["messages"]}
    assert intents == {"triage", "fyi"}
    # Verify each message dict actually has "intent" key
    for m in inbox["messages"]:
        assert "intent" in m


# ---------------------------------------------------------------------------
# Test 8: store enforces valid intent (empty / None rejected)
# ---------------------------------------------------------------------------

def test_store_send_message_rejects_empty_intent(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "bus.sqlite"))
    store.init_schema()
    store.upsert_agent("sender")
    store.upsert_agent("receiver")

    with pytest.raises(ValueError, match="INTENT_INVALID"):
        store.send_message("sender", "receiver", "body", intent="")

    with pytest.raises(ValueError, match="INTENT_INVALID"):
        store.send_message("sender", "receiver", "body", intent=None)  # type: ignore[arg-type]
