import pytest

from a2a_mcp_bridge.store import Store


class TestAgentLifecycle:
    def test_upsert_new_agent_creates_record(self, store: Store) -> None:
        store.upsert_agent("alice")
        agents = store.list_agents(active_within_days=7)
        assert len(agents) == 1
        assert agents[0].agent_id == "alice"

    def test_upsert_existing_agent_updates_last_seen(self, store: Store) -> None:
        store.upsert_agent("alice")
        first = store.list_agents(7)[0].last_seen_at

        # Touch again — must update last_seen_at, leave first_seen_at unchanged
        store.upsert_agent("alice")
        after = store.list_agents(7)[0]
        assert after.last_seen_at >= first
        assert after.first_seen_at <= after.last_seen_at

    def test_list_excludes_inactive_agents(self, store: Store) -> None:
        """An agent whose last_seen_at is older than the window is excluded."""
        # Directly inject an old row
        store._conn.execute(
            """
            INSERT INTO agents (id, first_seen_at, last_seen_at, metadata)
            VALUES (?, ?, ?, NULL)
            """,
            ("stale", "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00"),
        )
        active = store.list_agents(active_within_days=7)
        assert "stale" not in [a.agent_id for a in active]


class TestMessaging:
    def test_send_and_inbox_marks_read(self, store: Store) -> None:
        store.upsert_agent("alice")
        store.upsert_agent("bob")

        result = store.send_message(sender="alice", recipient="bob", body="hello")
        assert result.recipient == "bob"
        assert result.message_id

        inbox = store.read_inbox(agent_id="bob", limit=10, unread_only=True)
        assert len(inbox) == 1
        assert inbox[0].body == "hello"
        assert inbox[0].read_at is not None

        # Second call returns nothing (already marked read)
        assert store.read_inbox(agent_id="bob", limit=10, unread_only=True) == []

    def test_read_inbox_non_unread_returns_all(self, store: Store) -> None:
        store.upsert_agent("alice")
        store.upsert_agent("bob")
        store.send_message("alice", "bob", "1")
        store.send_message("alice", "bob", "2")
        store.read_inbox("bob", unread_only=True)  # mark as read

        all_msgs = store.read_inbox("bob", unread_only=False, limit=10)
        assert len(all_msgs) == 2

    def test_send_rejects_unknown_target(self, store: Store) -> None:
        store.upsert_agent("alice")
        with pytest.raises(ValueError, match="TARGET_UNKNOWN"):
            store.send_message(sender="alice", recipient="nobody", body="hi")

    def test_send_rejects_self(self, store: Store) -> None:
        store.upsert_agent("alice")
        with pytest.raises(ValueError, match="TARGET_SELF"):
            store.send_message(sender="alice", recipient="alice", body="hi")

    def test_send_rejects_oversized_body(self, store: Store) -> None:
        from a2a_mcp_bridge.models import MAX_BODY_BYTES

        store.upsert_agent("alice")
        store.upsert_agent("bob")
        with pytest.raises(ValueError, match="MESSAGE_TOO_LARGE"):
            store.send_message("alice", "bob", "x" * (MAX_BODY_BYTES + 1))

    def test_inbox_respects_limit(self, store: Store) -> None:
        store.upsert_agent("alice")
        store.upsert_agent("bob")
        for i in range(5):
            store.send_message("alice", "bob", f"msg {i}")
        inbox = store.read_inbox("bob", limit=3, unread_only=True)
        assert len(inbox) == 3


class TestPurgeOldMessages:
    """Tests for Store.purge_old_messages."""

    def _insert_old_msg(
        self,
        store: Store,
        msg_id: str,
        *,
        created_at: str = "2024-01-01T00:00:00+00:00",
        read_at: str | None = "2024-01-01T01:00:00+00:00",
    ) -> None:
        """Insert a message with an explicit (old) timestamp via raw SQL."""
        store._conn.execute(
            "INSERT INTO messages (id, sender_id, recipient_id, body, "
            "metadata, created_at, read_at, sender_session_id, intent) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (msg_id, "alice", "bob", "old msg", "{}", created_at, read_at, None, "triage"),
        )

    def test_purge_old_messages_deletes_by_age(self, store: Store) -> None:
        store.upsert_agent("alice")
        store.upsert_agent("bob")

        # Old message (2024) — should be deleted
        self._insert_old_msg(store, "m_old")
        # Recent message (now) — should survive
        store.send_message("alice", "bob", "recent msg")

        deleted = store.purge_old_messages(older_than_days=30)
        assert deleted == 1

        # Verify the old one is gone and the recent one survives
        remaining = store._conn.execute("SELECT id FROM messages").fetchall()
        assert len(remaining) == 1  # only the recent message

    def test_purge_old_messages_unread_only_preserves_unread(self, store: Store) -> None:
        store.upsert_agent("alice")
        store.upsert_agent("bob")

        # Old + read (read_at set) — should be deleted
        self._insert_old_msg(store, "m_read", read_at="2024-01-01T01:00:00+00:00")
        # Old + unread (read_at NULL) — should survive even though old
        self._insert_old_msg(store, "m_unread", read_at=None)

        deleted = store.purge_old_messages(older_than_days=30, unread_only=True)
        assert deleted == 1

        remaining = store._conn.execute("SELECT id FROM messages").fetchall()
        ids = {r["id"] for r in remaining}
        assert "m_unread" in ids
        assert "m_read" not in ids

    def test_purge_old_messages_rejects_invalid_days(self, store: Store) -> None:
        with pytest.raises(ValueError, match="older_than_days must be >= 1"):
            store.purge_old_messages(older_than_days=0)

    def test_purge_old_messages_returns_count(self, store: Store) -> None:
        store.upsert_agent("alice")
        store.upsert_agent("bob")

        # Insert 3 old messages
        self._insert_old_msg(store, "m1")
        self._insert_old_msg(store, "m2")
        self._insert_old_msg(store, "m3")

        deleted = store.purge_old_messages(older_than_days=30)
        assert deleted == 3
