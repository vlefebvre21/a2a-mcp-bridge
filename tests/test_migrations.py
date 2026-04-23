"""Tests for idempotent schema migrations applied by :meth:`Store.init_schema`.

The v0.5 migration adds ``messages.sender_session_id`` to databases that
pre-date the column. We verify:

* a fresh DB already carries the column (via the updated ``schema.sql``);
* a DB created from the legacy v0.4 schema has the column added on init,
  without losing existing rows;
* a second ``init_schema`` call on a migrated DB is a no-op (idempotent).
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from a2a_mcp_bridge.store import Store

LEGACY_V04_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    metadata TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    sender_id TEXT NOT NULL,
    recipient_id TEXT NOT NULL,
    body TEXT NOT NULL,
    metadata TEXT,
    created_at TEXT NOT NULL,
    read_at TEXT,
    FOREIGN KEY (sender_id) REFERENCES agents(id),
    FOREIGN KEY (recipient_id) REFERENCES agents(id)
);
"""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _seed_legacy_db(db_path: Path) -> None:
    """Create a DB with the pre-v0.5 schema and one message in it."""
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        conn.executescript(LEGACY_V04_SCHEMA)
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO agents (id, first_seen_at, last_seen_at, metadata) VALUES (?, ?, ?, NULL)",
            ("alice", now, now),
        )
        conn.execute(
            "INSERT INTO agents (id, first_seen_at, last_seen_at, metadata) VALUES (?, ?, ?, NULL)",
            ("bob", now, now),
        )
        conn.execute(
            """
            INSERT INTO messages (id, sender_id, recipient_id, body, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (uuid.uuid4().hex, "alice", "bob", "legacy", None, now),
        )
    finally:
        conn.close()


class TestSenderSessionIdMigration:
    def test_fresh_db_has_sender_session_id_column(self, tmp_path: Path) -> None:
        db_path = tmp_path / "fresh.sqlite"
        store = Store(str(db_path))
        store.init_schema()
        try:
            assert "sender_session_id" in _columns(store._conn, "messages")
        finally:
            store.close()

    def test_legacy_db_gets_column_added_without_data_loss(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "legacy.sqlite"
        _seed_legacy_db(db_path)

        # Sanity check — pre-migration state
        conn = sqlite3.connect(str(db_path))
        assert "sender_session_id" not in _columns(conn, "messages")
        pre_rows = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        conn.close()
        assert pre_rows == 1

        # Migrate
        store = Store(str(db_path))
        store.init_schema()
        try:
            # Column exists, existing rows still present, sender_session_id NULL on old row
            assert "sender_session_id" in _columns(store._conn, "messages")
            rows = store._conn.execute(
                "SELECT body, sender_session_id FROM messages"
            ).fetchall()
            assert len(rows) == 1
            assert rows[0]["body"] == "legacy"
            assert rows[0]["sender_session_id"] is None
        finally:
            store.close()

    def test_migration_is_idempotent(self, tmp_path: Path) -> None:
        db_path = tmp_path / "repeat.sqlite"
        _seed_legacy_db(db_path)

        # First init — adds column
        store = Store(str(db_path))
        store.init_schema()
        store.close()

        # Second init on the same file — must not raise and must leave state intact
        store = Store(str(db_path))
        store.init_schema()
        try:
            assert "sender_session_id" in _columns(store._conn, "messages")
            rows = store._conn.execute("SELECT COUNT(*) FROM messages").fetchone()
            assert rows[0] == 1
        finally:
            store.close()

    def test_migration_does_not_break_existing_send_read_flow(
        self, tmp_path: Path
    ) -> None:
        """After migration the canonical send/read flow still behaves as before."""
        db_path = tmp_path / "flow.sqlite"
        _seed_legacy_db(db_path)

        store = Store(str(db_path))
        store.init_schema()
        try:
            store.send_message(sender="alice", recipient="bob", body="post-migration")
            unread = store.read_inbox(agent_id="bob", unread_only=True)
            bodies = [m.body for m in unread]
            # Both the legacy row and the post-migration row are deliverable
            assert "legacy" in bodies
            assert "post-migration" in bodies
        finally:
            store.close()


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(str(tmp_path / "bus.sqlite"))
    s.init_schema()
    return s
