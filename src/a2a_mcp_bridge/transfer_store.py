"""SQLite-backed transfer store for ADR-007 Phase C — tracks file transfers staged on the facade server.

Source of truth split (ADR-007):
  - Phase A (same-machine): ``transfers.py`` uses ``meta.json`` files on disk.
    No TransferStore involved — the local Store + meta.json are sufficient.
  - Phase C (cross-host / façade): this module tracks uploads in SQLite.
    The façade server is the only host with access to both the staged file
    and this database; remote agents query via HTTP.

The two stores are intentionally separate: Phase A agents never touch
TransferStore, and Phase C agents never read local meta.json files.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS transfers (
    id TEXT PRIMARY KEY,
    sender_id TEXT NOT NULL,
    recipient_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    staged_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    fetched_at TEXT,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_transfers_recipient ON transfers(recipient_id);
CREATE INDEX IF NOT EXISTS idx_transfers_expires ON transfers(expires_at);
"""


class TransferStore:
    """Thin SQLite repository for staged file transfers.

    Each row represents a file that has been staged (copied into the
    transfer directory) and is awaiting collection by the recipient.
    Soft-deletes via ``deleted_at``; hard cleanup is left to an external
    reaper that reads :meth:`list_expired` and then removes both the
    on-disk file and the database row.

    Not thread-safe; create one per process/thread.
    """

    def __init__(self, db_path: str, *, check_same_thread: bool = True) -> None:
        """Open (or create) the transfer database.

        Args:
            db_path: filesystem path to the SQLite database file.
            check_same_thread: forwarded to :func:`sqlite3.connect`.
                Set to ``False`` when sharing a single store across
                threads (e.g. in ASGI apps).
        """
        self.db_path = db_path
        self._conn = sqlite3.connect(
            db_path,
            isolation_level=None,
            check_same_thread=check_same_thread,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA_SQL)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _row_to_dict(r: sqlite3.Row) -> dict[str, Any]:
        """Convert a :class:`sqlite3.Row` to a plain ``dict``."""
        return dict(r)

    # -- CRUD --------------------------------------------------------------

    def create(
        self,
        *,
        id: str,
        sender_id: str,
        recipient_id: str,
        filename: str,
        size_bytes: int,
        sha256: str,
        staged_path: str,
        expires_at: str,
    ) -> dict[str, Any]:
        """Insert a new transfer row and return it as a dict.

        Args:
            id: unique transfer identifier.
            sender_id: agent that staged the file.
            recipient_id: agent that may fetch the file.
            filename: **bare** filename (no path components). Must pass
                ``os.path.basename(filename) == filename`` to prevent
                path-traversal attacks.
            size_bytes: file size in bytes.
            sha256: hex-encoded SHA-256 digest of the staged file.
            staged_path: absolute path to the file on the facade server.
            expires_at: ISO-8601 UTC timestamp when the transfer expires.

        Returns:
            The newly inserted row as a dict.

        Raises:
            AssertionError: if *filename* contains path separators or
                ``..`` components.
        """
        assert os.path.basename(filename) == filename, (
            f"filename must be a bare name (no '/' or '..'), got: {filename!r}"
        )

        created_at = datetime.now(UTC).isoformat()

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                """
                INSERT INTO transfers
                    (id, sender_id, recipient_id, filename, size_bytes,
                     sha256, staged_path, created_at, expires_at,
                     fetched_at, deleted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    id,
                    sender_id,
                    recipient_id,
                    filename,
                    size_bytes,
                    sha256,
                    staged_path,
                    created_at,
                    expires_at,
                ),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        row = self._conn.execute(
            "SELECT * FROM transfers WHERE id = ?",
            (id,),
        ).fetchone()
        return self._row_to_dict(row)

    def get(self, transfer_id: str) -> dict[str, Any] | None:
        """Return a transfer row by id, or ``None`` if not found.

        Args:
            transfer_id: the ``id`` column value to look up.

        Returns:
            The matching row as a dict, or ``None``.
        """
        row = self._conn.execute(
            "SELECT * FROM transfers WHERE id = ?",
            (transfer_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def mark_fetched(self, transfer_id: str) -> bool:
        """Set ``fetched_at`` to the current UTC timestamp.

        Args:
            transfer_id: the transfer to mark as fetched.

        Returns:
            ``True`` if the row was found and updated, ``False`` otherwise.
        """
        now = datetime.now(UTC).isoformat()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._conn.execute(
                "UPDATE transfers SET fetched_at = ? WHERE id = ?",
                (now, transfer_id),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return cursor.rowcount > 0

    def delete(self, transfer_id: str) -> bool:
        """Soft-delete a transfer by setting ``deleted_at`` to now.

        The on-disk file is **not** removed — that is the reaper's job.

        Args:
            transfer_id: the transfer to soft-delete.

        Returns:
            ``True`` if the row was found and updated, ``False`` otherwise.
        """
        now = datetime.now(UTC).isoformat()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._conn.execute(
                "UPDATE transfers SET deleted_at = ? WHERE id = ?",
                (now, transfer_id),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return cursor.rowcount > 0

    def list_expired(self) -> list[dict[str, Any]]:
        """Return all transfers that have expired and are not yet soft-deleted.

        Used by the external reaper to identify files whose TTL has
        elapsed so they can be cleaned up from disk and the database.

        Returns:
            List of expired transfer rows as dicts.
        """
        now = datetime.now(UTC).isoformat()
        rows = self._conn.execute(
            """
            SELECT * FROM transfers
            WHERE expires_at < ? AND deleted_at IS NULL
            """,
            (now,),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def count_pending(self, sender_id: str) -> int:
        """Count non-expired, non-deleted transfers for a given sender.

        Useful for enforcing per-sender quotas or rate limits.

        Args:
            sender_id: the sender whose pending transfers to count.

        Returns:
            Number of pending (alive) transfers owned by *sender_id*.
        """
        now = datetime.now(UTC).isoformat()
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM transfers
            WHERE sender_id = ? AND deleted_at IS NULL AND expires_at > ?
            """,
            (sender_id, now),
        ).fetchone()
        return int(row["cnt"])

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()
