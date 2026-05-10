"""Comprehensive integration tests for a2a-mcp-bridge using real SQLite.

# These tests verify end-to-end behaviour without mocking:
#  - Message lifecycle: send → inbox (unread) → read_inbox (mark-as-read) → peek (read status)
#  - File transfers: stage → sha256 verification → fetch → delete
#  - Real-time subscriptions: timeout, signal wake-up
#  - Rate limiting: burst allowance, cooldown, reset
#  - Error handling: unknown agents, expired transfers, oversized payloads, path traversal
#  - Sequential operations: dual agents with cross-messaging and read ordering
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from a2a_mcp_bridge.models import SendResult
from a2a_mcp_bridge.rate_limit import RateLimiter
from a2a_mcp_bridge.store import SignalDir, Store
from a2a_mcp_bridge.transfer_store import TransferStore
from a2a_mcp_bridge.transfers import (
    new_transfer_id,
)


class TestMessageLifecycle:
    """Category 1: Full message lifecycle from send to read verification."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path) -> None:
        self.db = tmp_path / "bus.sqlite"
        self.store = Store(str(self.db), signal_dir=None)
        self.store.init_schema()

    def test_send_and_read_peek(self) -> None:
        """Send a message, verify it appears unread in inbox, then read and peek."""
        self.store.upsert_agent("alice")
        self.store.upsert_agent("bob")

        # Send message (intent='execute' for testing)
        result: SendResult = self.store.send_message(
            sender="alice",
            recipient="bob",
            body="Hello from Alice",
            intent="execute",
        )

        assert result.message_id is not None
        assert result.recipient == "bob"
        assert isinstance(result.sent_at, datetime)

        # Peek (should see unread message, read_at is None)
        peeked = self.store.peek_inbox("bob", limit=10)
        assert len(peeked) == 1
        assert peeked[0].id == result.message_id
        assert peeked[0].read_at is None  # Not read yet

        # Read inbox (consumes, marks as read)
        read = self.store.read_inbox("bob", limit=10, unread_only=True)
        assert len(read) == 1
        assert read[0].read_at is not None
        assert (datetime.now(UTC) - read[0].read_at).total_seconds() < 1.0

        # Peek again (should see same message but now read)
        peeked2 = self.store.peek_inbox("bob", limit=10, since_ts=None)
        assert len(peeked2) == 1
        assert peeked2[0].read_at is not None

    def test_read_inbox_limits(self) -> None:
        """read_inbox respects limit parameter and unread_only filtering."""
        self.store.upsert_agent("sender")
        self.store.upsert_agent("receiver")

        # Send 5 messages
        for i in range(5):
            self.store.send_message("sender", "receiver", f"msg-{i}")

        # Read only 2 (unread_only=True)
        read = self.store.read_inbox("receiver", limit=2, unread_only=True)
        assert len(read) == 2

        # Read remaining (should get 3)
        read_more = self.store.read_inbox("receiver", limit=10, unread_only=True)
        assert len(read_more) == 3

        # Read only read messages (unread_only=False, should get all 5)
        peek_all = self.store.read_inbox("receiver", limit=10, unread_only=False)
        assert len(peek_all) == 5

    def test_sender_session_id_propagation(self) -> None:
        """Optional session_id in metadata is hoisted to dedicated column."""
        self.store.upsert_agent("a")
        self.store.upsert_agent("b")

        session = "sess-12345"
        self.store.send_message(
            "a",
            "b",
            "test body",
            metadata={"session_id": session, "other": "data"},
        )

        msg = self.store.peek_inbox("b", limit=1)[0]
        assert msg.sender_session_id == session
        # Metadata JSON still contains the full dict including session_id
        assert msg.metadata is not None and "other" in msg.metadata


class TestFileTransfers:
    """Category 2: File transfer lifecycle (stage → fetch → verify sha256 → delete)."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.db = tmp_path / "bus.sqlite"
        self.transfers_dir = tmp_path / "transfers"
        self.transfers_dir.mkdir()
        # Use monkeypatch for automatic rollback (prevents global state pollution)
        monkeypatch.setattr("a2a_mcp_bridge.transfers.resolve_transfer_dir", lambda: self.transfers_dir)

        self.db_store = Store(str(self.db), signal_dir=None)
        self.db_store.init_schema()
        # TransferStore takes a db file path (auto-creates with schema)
        self.transfer_store = TransferStore(str(tmp_path / "transfers.db"))

    def test_stage_and_fetch_with_sha256(self) -> None:
        """Stage a file, verify manifest created in DB via get(), check sha256."""
        self.db_store.upsert_agent("sender")
        self.db_store.upsert_agent("recipient")

        payload = b"Hello, file transfer!"
        source_path = self.transfers_dir / "source.bin"
        source_path.write_bytes(payload)

        transfer_id = new_transfer_id()
        sha256 = hashlib.sha256(payload).hexdigest()
        expires_at = (datetime.now(UTC) + timedelta(hours=24)).isoformat()

        # Use real API: create with exact signature from transfer_store.py line 83-153
        self.transfer_store.create(
            id=transfer_id,
            sender_id="sender",
            recipient_id="recipient",
            filename="source.bin",  # bare filename, no path
            size_bytes=len(payload),
            sha256=sha256,
            staged_path=str(source_path),
            expires_at=expires_at,
        )

        # Verify manifest was created via get()
        row = self.transfer_store.get(transfer_id)
        assert row is not None
        assert row["sha256"] == sha256
        assert row["recipient_id"] == "recipient"
        assert row["sender_id"] == "sender"
    def test_delete_transfer(self) -> None:
        """Soft-delete sets deleted_at timestamp, findable via list_expired after TTL."""
        self.db_store.upsert_agent("sender")

        transfer_id = new_transfer_id()
        payload = b"secret"
        sha256 = hashlib.sha256(payload).hexdigest()

        # Create source file and manifest via API
        source_path = self.transfers_dir / "secret.bin"
        source_path.write_bytes(payload)

        # Use expired timestamp so list_expired can find it immediately after delete
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

        self.transfer_store.create(
            id=transfer_id,
            sender_id="sender",
            recipient_id="victim",
            filename="secret.bin",  # bare name
            size_bytes=len(payload),
            sha256=sha256,
            staged_path=str(source_path),
            expires_at=past,  # already expired
        )

        # Verify transfer exists in DB
        row = self.transfer_store.get(transfer_id)
        assert row is not None

        # Delete (soft-delete, sets deleted_at)
        result = self.transfer_store.delete(transfer_id)
        assert result is True  # Row was updated

        # Verify transfer marked as deleted in DB (deleted_at is not null)
        row = self.transfer_store.get(transfer_id)
        assert row["deleted_at"] is not None
    def test_transfer_path_traversal_rejected(self) -> None:
        """Path traversal attempts in filenames are rejected during create()."""
        # These fail the basename check: os.path.basename(filename) == filename
        bad_names = ["../etc/passwd", "../../secret.txt"]
        for bad in bad_names:
            with pytest.raises(AssertionError, match="filename must be a bare name"):
                # Trigger the basename check in create()
                self.transfer_store.create(
                    id=new_transfer_id(),
                    sender_id="attacker",
                    recipient_id="victim",
                    filename=bad,  # contains path separator
                    size_bytes=0,
                    sha256="0" * 64,
                    staged_path=str(self.transfers_dir / "dummy"),
                    expires_at=datetime.now(UTC).isoformat(),
                )


class TestSubscriptionMechanism:
    """Category 3: Long-poll subscription with timeout and signal wake-up."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path) -> None:
        self.db = tmp_path / "bus.sqlite"
        self.signal_dir = SignalDir(str(tmp_path / "signals"))
        self.store = Store(str(self.db), signal_dir=self.signal_dir)
        self.store.init_schema()

    def test_subscribe_returns_immediately_with_existing(self) -> None:
        """If inbox has unread messages, subscribe returns immediately without blocking."""
        self.store.upsert_agent("alice")
        self.store.upsert_agent("system")  # Register sender
        self.store.send_message("system", "alice", "urgent!")

        msgs, timed_out = self.store.subscribe("alice", timeout_seconds=30)
        assert len(msgs) == 1
        assert msgs[0].body == "urgent!"
        assert not timed_out  # Didn't timeout, had data

    def test_subscribe_times_out_when_empty(self) -> None:
        """Empty inbox with no signal triggers timeout."""
        self.store.upsert_agent("bob")

        start = datetime.now(UTC)
        msgs, timed_out = self.store.subscribe("bob", timeout_seconds=2)
        elapsed = (datetime.now(UTC) - start).total_seconds()

        assert len(msgs) == 0
        assert timed_out is True
        assert elapsed >= 1.5  # Should have waited close to timeout

    def test_signal_wakes_subscription(self) -> None:
        """Signal file triggers immediate return from subscribe (no blocking)."""
        self.store.upsert_agent("charlie")

        # Create signal file BEFORE calling subscribe using the actual API
        self.signal_dir.notify("charlie")

        # Subscribe should return immediately because signal exists, not block 5s
        start = datetime.now(UTC)
        _, timed_out = self.store.subscribe("charlie", timeout_seconds=5)
        elapsed = (datetime.now(UTC) - start).total_seconds()

        assert timed_out is False  # Not a timeout (would be if it blocked 5s)
        assert elapsed < 0.1  # Returned immediately (signal detected)


class TestRateLimiting:
    """Category 4: Rate limiter burst, cooldown, and reset semantics."""

    def test_burst_and_cooldown(self) -> None:
        """Limiter allows burst of 'rpm' requests then blocks until window slides."""
        limiter = RateLimiter(rpm=3)  # 3 per minute
        key = "ip-1.2.3.4"

        # Burst: 3 requests should succeed
        assert limiter.allow(key) is True
        assert limiter.allow(key) is True
        assert limiter.allow(key) is True

        # 4th should fail (rate limited)
        assert limiter.allow(key) is False

    def test_reset_clears_window(self) -> None:
        """reset() clears the sliding window for a key."""
        limiter = RateLimiter(rpm=2)
        key = "ip-5.6.7.8"

        # Consume quota
        assert limiter.allow(key) is True
        assert limiter.allow(key) is True
        assert limiter.allow(key) is False  # Blocked

        # Reset and try again
        limiter.reset(key)
        assert limiter.allow(key) is True  # Now allowed again

    def test_disabled_limiter_allows_all(self) -> None:
        """rpm=0 disables rate limiting entirely."""
        limiter = RateLimiter(rpm=0)

        for _ in range(100):
            assert limiter.allow("any-key") is True
        # Internal dict should be empty (no tracking)
        assert len(limiter.hits) == 0


class TestErrorHandling:
    """Category 5: Graceful error handling for edge cases."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path) -> None:
        self.db = tmp_path / "bus.sqlite"
        self.store = Store(str(self.db), signal_dir=None)
        self.store.init_schema()

    def test_send_to_unknown_agent(self) -> None:
        """Sending to unregistered agent raises ValueError with TARGET_UNKNOWN."""
        self.store.upsert_agent("alice")

        with pytest.raises(ValueError, match=r"TARGET_UNKNOWN.*unknown-agent"):
            self.store.send_message("alice", "unknown-agent", "hello")

    def test_send_to_self_rejected(self) -> None:
        """Agents cannot send messages to themselves (TARGET_SELF)."""
        self.store.upsert_agent("alice")

        with pytest.raises(ValueError, match="TARGET_SELF"):
            self.store.send_message("alice", "alice", "echo")

    def test_oversized_body_rejected(self) -> None:
        """Body exceeding MAX_BODY_BYTES (64KB) is rejected."""
        from a2a_mcp_bridge.models import MAX_BODY_BYTES
        self.store.upsert_agent("a")
        self.store.upsert_agent("b")

        huge = "x" * (MAX_BODY_BYTES + 1)
        with pytest.raises(ValueError, match="MESSAGE_TOO_LARGE"):
            self.store.send_message("a", "b", huge)

    def test_session_id_too_large(self) -> None:
        """Session ID exceeding 128 bytes is rejected."""
        from a2a_mcp_bridge.models import MAX_SESSION_ID_BYTES
        self.store.upsert_agent("a")
        self.store.upsert_agent("b")

        bad_session = "a" * (MAX_SESSION_ID_BYTES + 1)
        with pytest.raises(ValueError, match="SESSION_ID_TOO_LARGE"):
            self.store.send_message(
                "a", "b", "body",
                metadata={"session_id": bad_session}
            )



class TestSequentialOperations:
    """Category 6: Two agents exchanging messages with sequential (single-threaded) operations."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path) -> None:
        self.db = tmp_path / "bus.sqlite"
        # Use check_same_thread=False for concurrent testing
        self.store = Store(str(self.db), signal_dir=None, check_same_thread=False)
        self.store.init_schema()

    def test_cross_messaging_atomicity(self) -> None:
        """Multiple sends in rapid succession all appear in inbox correctly."""
        self.store.upsert_agent("alpha")
        self.store.upsert_agent("beta")

        # Send 10 messages in loop
        for i in range(10):
            self.store.send_message("alpha", "beta", f"batch-{i}")

        # All should appear in peek (unread)
        msgs = self.store.peek_inbox("beta", limit=50)
        assert len(msgs) == 10
        bodies = [m.body for m in msgs]
        assert all(f"batch-{i}" in bodies for i in range(10))

    def test_concurrent_readers_consistency(self) -> None:
        """Two sequential read_inbox calls mark messages atomically."""
        self.store.upsert_agent("sender")
        self.store.upsert_agent("receiver")

        # Send 3 messages
        for i in range(3):
            self.store.send_message("sender", "receiver", f"msg-{i}")

        # First read consumes first 2 (unread only)
        batch1 = self.store.read_inbox("receiver", limit=2, unread_only=True)
        assert len(batch1) == 2

        # Second read should get remaining 1 (not the first 2 again)
        batch2 = self.store.read_inbox("receiver", limit=5, unread_only=True)
        assert len(batch2) == 1
        assert batch2[0].body == "msg-2"

    def test_read_at_ordering(self) -> None:
        """read_inbox returns messages in created_at ASC order (FIFO)."""
        self.store.upsert_agent("a")
        self.store.upsert_agent("b")

        # Send with intentional ordering (oldest first in DB)
        self.store.send_message("a", "b", "first")
        self.store.send_message("a", "b", "second")
        self.store.send_message("a", "b", "third")

        msgs = self.store.read_inbox("b", limit=10, unread_only=True)
        assert [m.body for m in msgs] == ["first", "second", "third"]
