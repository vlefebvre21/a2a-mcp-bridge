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
