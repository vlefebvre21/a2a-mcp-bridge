"""Tests for TransferStore — SQLite-backed transfer tracking (ADR-007 Phase C)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from a2a_mcp_bridge.transfer_store import TransferStore


# -- fixture ---------------------------------------------------------------


@pytest.fixture
def ts(tmp_path: Path) -> TransferStore:
    """Return a fresh TransferStore backed by a temp SQLite file."""
    db_path = tmp_path / "transfers.sqlite"
    return TransferStore(str(db_path), check_same_thread=False)


# -- helpers ---------------------------------------------------------------

def _past_ts() -> str:
    """ISO-8601 timestamp well in the past (expired)."""
    return (datetime.now(UTC) - timedelta(days=1)).isoformat()


def _future_ts() -> str:
    """ISO-8601 timestamp well in the future (active)."""
    return (datetime.now(UTC) + timedelta(days=1)).isoformat()


def _make_transfer(
    ts: TransferStore,
    *,
    id: str = "xfer-001",
    sender_id: str = "alice",
    recipient_id: str = "bob",
    filename: str = "data.csv",
    size_bytes: int = 1024,
    sha256: str = "a" * 64,
    staged_path: str = "/tmp/staged/data.csv",
    expires_at: str | None = None,
) -> dict:
    """Convenience wrapper to create a transfer with sensible defaults."""
    return ts.create(
        id=id,
        sender_id=sender_id,
        recipient_id=recipient_id,
        filename=filename,
        size_bytes=size_bytes,
        sha256=sha256,
        staged_path=staged_path,
        expires_at=expires_at or _future_ts(),
    )


# -- tests -----------------------------------------------------------------


class TestCreateAndGet:
    def test_create_and_get(self, ts: TransferStore) -> None:
        """Create a transfer, get it back, verify all fields."""
        expires = _future_ts()
        row = ts.create(
            id="xfer-1",
            sender_id="alice",
            recipient_id="bob",
            filename="report.pdf",
            size_bytes=2048,
            sha256="b" * 64,
            staged_path="/tmp/staged/report.pdf",
            expires_at=expires,
        )

        # Verify returned dict has expected fields
        assert row["id"] == "xfer-1"
        assert row["sender_id"] == "alice"
        assert row["recipient_id"] == "bob"
        assert row["filename"] == "report.pdf"
        assert row["size_bytes"] == 2048
        assert row["sha256"] == "b" * 64
        assert row["staged_path"] == "/tmp/staged/report.pdf"
        assert row["expires_at"] == expires
        assert row["fetched_at"] is None
        assert row["deleted_at"] is None
        assert row["created_at"] is not None

        # get() returns identical data
        fetched = ts.get("xfer-1")
        assert fetched is not None
        assert fetched == row


class TestGetNotFound:
    def test_get_not_found(self, ts: TransferStore) -> None:
        """get() returns None for an unknown id."""
        assert ts.get("nonexistent") is None


class TestMarkFetched:
    def test_mark_fetched(self, ts: TransferStore) -> None:
        """mark_fetched sets fetched_at, returns True; False for unknown id."""
        _make_transfer(ts, id="xfer-f1")
        assert ts.get("xfer-f1")["fetched_at"] is None

        result = ts.mark_fetched("xfer-f1")
        assert result is True
        assert ts.get("xfer-f1")["fetched_at"] is not None

        # Unknown id returns False
        assert ts.mark_fetched("no-such-id") is False


class TestDeleteSoft:
    def test_delete_soft(self, ts: TransferStore) -> None:
        """delete() sets deleted_at, get() still returns the row, returns True."""
        _make_transfer(ts, id="xfer-del1")
        assert ts.get("xfer-del1")["deleted_at"] is None

        result = ts.delete("xfer-del1")
        assert result is True

        row = ts.get("xfer-del1")
        assert row is not None
        assert row["deleted_at"] is not None


class TestDeleteNotFound:
    def test_delete_not_found(self, ts: TransferStore) -> None:
        """delete() returns False for an unknown id."""
        assert ts.delete("nonexistent") is False


class TestListExpired:
    def test_list_expired(self, ts: TransferStore) -> None:
        """list_expired returns only expired (and not deleted) transfers."""
        _make_transfer(ts, id="xfer-exp", expires_at=_past_ts())
        _make_transfer(ts, id="xfer-act", expires_at=_future_ts())

        expired = ts.list_expired()
        ids = [r["id"] for r in expired]
        assert "xfer-exp" in ids
        assert "xfer-act" not in ids


class TestListExpiredExcludesDeleted:
    def test_list_expired_excludes_deleted(self, ts: TransferStore) -> None:
        """An expired + soft-deleted transfer should NOT appear in list_expired."""
        _make_transfer(ts, id="xfer-exp-del", expires_at=_past_ts())
        ts.delete("xfer-exp-del")

        expired = ts.list_expired()
        ids = [r["id"] for r in expired]
        assert "xfer-exp-del" not in ids


class TestCountPending:
    def test_count_pending(self, ts: TransferStore) -> None:
        """count_pending returns only non-expired, non-deleted transfers for the sender."""
        # 2 active transfers for alice
        _make_transfer(ts, id="xfer-p1", sender_id="alice", expires_at=_future_ts())
        _make_transfer(ts, id="xfer-p2", sender_id="alice", expires_at=_future_ts())
        # 1 expired transfer for alice
        _make_transfer(ts, id="xfer-p3", sender_id="alice", expires_at=_past_ts())

        assert ts.count_pending("alice") == 2


class TestCountPendingExcludesDeleted:
    def test_count_pending_excludes_deleted(self, ts: TransferStore) -> None:
        """Soft-deleted transfers should not count as pending."""
        _make_transfer(ts, id="xfer-cd1", sender_id="alice", expires_at=_future_ts())
        _make_transfer(ts, id="xfer-cd2", sender_id="alice", expires_at=_future_ts())
        ts.delete("xfer-cd1")

        assert ts.count_pending("alice") == 1


class TestPathTraversalProtection:
    def test_path_traversal_protection(self, ts: TransferStore) -> None:
        """create() with a path-traversal filename raises AssertionError."""
        with pytest.raises(AssertionError, match="filename"):
            ts.create(
                id="xfer-bad",
                sender_id="alice",
                recipient_id="bob",
                filename="../../etc/passwd",
                size_bytes=42,
                sha256="c" * 64,
                staged_path="/tmp/staged/evil",
                expires_at=_future_ts(),
            )
