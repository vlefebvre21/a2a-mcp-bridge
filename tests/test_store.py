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


import pytest


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
