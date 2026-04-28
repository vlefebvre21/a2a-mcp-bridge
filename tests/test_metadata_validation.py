"""Tests for metadata JSON validation and defensive parsing."""

import logging

import pytest

from a2a_mcp_bridge.store import Store


class TestMetadataValidationOnSend:
    """send_message must validate metadata before INSERT."""

    def test_valid_dict_metadata(self, store: Store) -> None:
        """A dict metadata is serialised and round-trips correctly."""
        store.upsert_agent("alice")
        store.upsert_agent("bob")

        meta = {"session_id": "abc123", "tag": "e2e"}
        result = store.send_message(
            sender="alice", recipient="bob", body="hi", metadata=meta,
        )
        assert result.message_id

        inbox = store.read_inbox("bob", limit=10, unread_only=True)
        assert len(inbox) == 1
        assert inbox[0].metadata == meta

    def test_invalid_json_string_metadata_raises(self, store: Store) -> None:
        """A string metadata that is not valid JSON raises METADATA_INVALID."""
        store.upsert_agent("alice")
        store.upsert_agent("bob")

        with pytest.raises(ValueError, match="METADATA_INVALID"):
            store.send_message(
                sender="alice",
                recipient="bob",
                body="hi",
                metadata="{'single': 'quotes'}",  # not valid JSON
            )

    def test_none_metadata_round_trips(self, store: Store) -> None:
        """metadata=None is stored as NULL and reads back as None."""
        store.upsert_agent("alice")
        store.upsert_agent("bob")

        result = store.send_message(
            sender="alice", recipient="bob", body="hi", metadata=None,
        )
        assert result.message_id

        inbox = store.read_inbox("bob", limit=10, unread_only=True)
        assert len(inbox) == 1
        assert inbox[0].metadata is None


class TestMetadataCorruptRecovery:
    """_row_to_message must not crash on corrupt JSON in the DB."""

    def test_corrupt_metadata_returns_none(self, store: Store, caplog: pytest.LogCaptureFixture) -> None:
        """If the DB holds invalid JSON, _row_to_message returns metadata=None."""
        store.upsert_agent("alice")
        store.upsert_agent("bob")

        # Insert a message with valid metadata first.
        store.send_message(
            sender="alice", recipient="bob", body="hi",
            metadata={"ok": True},
        )

        # Corrupt the metadata column directly in SQLite.
        store._conn.execute(
            "UPDATE messages SET metadata = ? WHERE recipient_id = ?",
            ("{'single': 'quotes'}", "bob"),
        )

        with caplog.at_level(logging.WARNING):
            inbox = store.read_inbox("bob", limit=10, unread_only=False)

        assert len(inbox) == 1
        assert inbox[0].metadata is None
        assert "corrupt" in caplog.text.lower()
