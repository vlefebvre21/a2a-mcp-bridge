"""SQLite-backed repository for a2a-mcp-bridge."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import MAX_BODY_BYTES, MAX_METADATA_BYTES, AgentRecord, Message, SendResult

SCHEMA_PATH: Path = Path(__file__).parent / "schema.sql"


class Store:
    """Thin SQLite repository. Not thread-safe; create one per process/thread."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")

    def init_schema(self) -> None:
        self._conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        """Apply idempotent schema migrations to pre-existing databases.

        Runs after :meth:`init_schema` so fresh DBs (which already include the
        current columns via ``CREATE TABLE IF NOT EXISTS``) skip the ALTER
        branches naturally. For pre-v0.5 DBs on disk we need to add columns
        introduced later without dropping data.

        Each migration step:
          * wraps its work in ``BEGIN IMMEDIATE`` / ``COMMIT`` so a concurrent
            writer (e.g. another Hermes profile spinning up) cannot interleave
            between the ``PRAGMA table_info`` probe and the ``ALTER TABLE``;
          * checks via ``PRAGMA table_info`` before attempting ``ALTER TABLE``,
            because SQLite has no ``ADD COLUMN IF NOT EXISTS`` and a second
            run would otherwise raise ``OperationalError``.

        The migration table of contents (keep in sync with CHANGELOG):

        * v0.5 — ``messages.sender_session_id TEXT NULL`` (A2A session
          correlation, see ADR-001).
        """
        # v0.5 — messages.sender_session_id
        existing_cols = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(messages)").fetchall()
        }
        if "sender_session_id" not in existing_cols:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                # Re-check inside the transaction in case a concurrent writer
                # raced us between the probe above and here.
                cols = {
                    row["name"]
                    for row in self._conn.execute(
                        "PRAGMA table_info(messages)"
                    ).fetchall()
                }
                if "sender_session_id" not in cols:
                    self._conn.execute(
                        "ALTER TABLE messages ADD COLUMN sender_session_id TEXT"
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        self._conn.close()

    # -- agents ------------------------------------------------------------

    def upsert_agent(self, agent_id: str, metadata: dict[str, Any] | None = None) -> None:
        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            """
            INSERT INTO agents (id, first_seen_at, last_seen_at, metadata)
            VALUES (?, ?, ?, NULL)
            ON CONFLICT(id) DO UPDATE SET last_seen_at = excluded.last_seen_at
            """,
            (agent_id, now, now),
        )

    def list_agents(self, active_within_days: int = 7) -> list[AgentRecord]:
        cutoff = (datetime.now(UTC) - timedelta(days=active_within_days)).isoformat()
        rows = self._conn.execute(
            """
            SELECT id, first_seen_at, last_seen_at
            FROM agents
            WHERE last_seen_at >= ?
            ORDER BY last_seen_at DESC
            """,
            (cutoff,),
        ).fetchall()
        return [
            AgentRecord(
                agent_id=r["id"],
                first_seen_at=datetime.fromisoformat(r["first_seen_at"]),
                last_seen_at=datetime.fromisoformat(r["last_seen_at"]),
                online=False,  # liveness is decided at the server layer
                metadata=None,
            )
            for r in rows
        ]

    # -- messaging ---------------------------------------------------------

    def send_message(
        self,
        sender: str,
        recipient: str,
        body: str,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        if sender == recipient:
            raise ValueError("TARGET_SELF: cannot send to self")
        if len(body.encode("utf-8")) > MAX_BODY_BYTES:
            raise ValueError(f"MESSAGE_TOO_LARGE: body exceeds {MAX_BODY_BYTES} bytes")
        metadata_json: str | None = None
        if metadata is not None:
            metadata_json = json.dumps(metadata, separators=(",", ":"))
            if len(metadata_json.encode("utf-8")) > MAX_METADATA_BYTES:
                raise ValueError(f"METADATA_TOO_LARGE: metadata exceeds {MAX_METADATA_BYTES} bytes")

        exists = self._conn.execute("SELECT 1 FROM agents WHERE id = ?", (recipient,)).fetchone()
        if not exists:
            raise ValueError(f"TARGET_UNKNOWN: {recipient}")

        message_id = uuid.uuid4().hex
        now = datetime.now(UTC)
        self._conn.execute(
            """
            INSERT INTO messages (id, sender_id, recipient_id, body, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (message_id, sender, recipient, body, metadata_json, now.isoformat()),
        )
        return SendResult(message_id=message_id, sent_at=now, recipient=recipient)

    def read_inbox(
        self,
        agent_id: str,
        limit: int = 10,
        unread_only: bool = True,
    ) -> list[Message]:
        limit = max(1, min(limit, 100))
        if unread_only:
            # Atomic select-and-mark: BEGIN, SELECT, UPDATE, COMMIT.
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    """
                    SELECT id, sender_id, recipient_id, body, metadata, created_at, read_at
                    FROM messages
                    WHERE recipient_id = ? AND read_at IS NULL
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (agent_id, limit),
                ).fetchall()
                if rows:
                    now = datetime.now(UTC).isoformat()
                    placeholders = ",".join("?" * len(rows))
                    self._conn.execute(
                        f"UPDATE messages SET read_at = ? WHERE id IN ({placeholders})",
                        (now, *[r["id"] for r in rows]),
                    )
                    # Re-read to include read_at values in returned objects
                    rows = self._conn.execute(
                        f"""
                        SELECT id, sender_id, recipient_id, body, metadata, created_at, read_at
                        FROM messages
                        WHERE id IN ({placeholders})
                        ORDER BY created_at ASC
                        """,
                        tuple(r["id"] for r in rows),
                    ).fetchall()
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        else:
            rows = self._conn.execute(
                """
                SELECT id, sender_id, recipient_id, body, metadata, created_at, read_at
                FROM messages
                WHERE recipient_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (agent_id, limit),
            ).fetchall()

        return [
            Message(
                id=r["id"],
                sender_id=r["sender_id"],
                recipient_id=r["recipient_id"],
                body=r["body"],
                metadata=json.loads(r["metadata"]) if r["metadata"] else None,
                created_at=datetime.fromisoformat(r["created_at"]),
                read_at=datetime.fromisoformat(r["read_at"]) if r["read_at"] else None,
            )
            for r in rows
        ]

    def peek_inbox(
        self,
        agent_id: str,
        since_ts: str | None = None,
        limit: int = 50,
    ) -> list[Message]:
        """Read-only view of the caller's inbox.

        Returns messages addressed to ``agent_id`` without any mark-as-read
        side-effect. The tuple ``(read_at, read status)`` of every row is left
        untouched — this is the crucial property that distinguishes
        :meth:`peek_inbox` from :meth:`read_inbox`.

        Semantics:
          * ``since_ts`` is an ISO-8601 UTC timestamp string. When provided,
            only messages with ``created_at >= since_ts`` are returned, sorted
            **ASC** by ``created_at`` — the replay-in-order use case (gateway
            cache recovery).
          * When ``since_ts`` is ``None``, returns the ``limit`` most recent
            messages sorted by ``created_at DESC``. This gives the "show me
            my latest inbox without consuming it" use case without forcing
            the caller to derive a timestamp.
          * Already-read messages ARE included, with their ``read_at``
            populated so the caller can tell who consumed them and when.

        The limit is clamped to ``[1, 200]`` to protect the caller from
        accidentally loading the entire history into a single payload.

        See ADR-001 §4 (bridge-side primitive #1) for the rationale.
        """
        limit = max(1, min(limit, 200))
        if since_ts is None:
            rows = self._conn.execute(
                """
                SELECT id, sender_id, recipient_id, body, metadata, created_at, read_at
                FROM messages
                WHERE recipient_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (agent_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT id, sender_id, recipient_id, body, metadata, created_at, read_at
                FROM messages
                WHERE recipient_id = ? AND created_at >= ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (agent_id, since_ts, limit),
            ).fetchall()

        return [
            Message(
                id=r["id"],
                sender_id=r["sender_id"],
                recipient_id=r["recipient_id"],
                body=r["body"],
                metadata=json.loads(r["metadata"]) if r["metadata"] else None,
                created_at=datetime.fromisoformat(r["created_at"]),
                read_at=datetime.fromisoformat(r["read_at"]) if r["read_at"] else None,
            )
            for r in rows
        ]
