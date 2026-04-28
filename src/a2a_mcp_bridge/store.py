"""SQLite-backed repository for a2a-mcp-bridge."""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .intents import DEFAULT_INTENT
from .models import (
    MAX_BODY_BYTES,
    MAX_METADATA_BYTES,
    MAX_SESSION_ID_BYTES,
    AgentRecord,
    Message,
    SendResult,
)

# Avoid circular import — signals is only needed for the type hint and
# the optional subscribe() implementation.  The module is lightweight
# (no heavy deps) so importing it at runtime is fine.
from .signals import SignalDir

SCHEMA_PATH: Path = Path(__file__).parent / "schema.sql"

logger = logging.getLogger(__name__)


class Store:
    """Thin SQLite repository. Not thread-safe; create one per process/thread.

    When *signal_dir* is provided, :meth:`subscribe` uses it for
    filesystem-based long-poll (the local mono-VPS path).  When
    *signal_dir* is ``None``, calling :meth:`subscribe` raises
    ``RuntimeError`` — the caller should use ``HttpBusStore`` instead.
    """

    def __init__(
        self, db_path: str, signal_dir: SignalDir | None = None,
        *, check_same_thread: bool = True,
    ) -> None:
        self.db_path = db_path
        self._signal_dir = signal_dir
        self._conn = sqlite3.connect(
            db_path, isolation_level=None,
            check_same_thread=check_same_thread,
        )
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

        Each migration step is delegated to :meth:`_add_column_if_missing`,
        which:
          * wraps its work in ``BEGIN IMMEDIATE`` / ``COMMIT`` so a concurrent
            writer (e.g. another Hermes profile spinning up) cannot interleave
            between the ``PRAGMA table_info`` probe and the ``ALTER TABLE``;
          * checks via ``PRAGMA table_info`` before attempting ``ALTER TABLE``,
            because SQLite has no ``ADD COLUMN IF NOT EXISTS`` and a second
            run would otherwise raise ``OperationalError``.

        The migration table of contents (keep in sync with CHANGELOG):

        * v0.5 — ``messages.sender_session_id TEXT NULL`` (A2A session
          correlation, see ADR-001).
        * v0.6 — ``messages.intent TEXT NOT NULL DEFAULT 'triage'`` (wake
          intent coupling, see ADR-002).
        """
        # v0.5 — messages.sender_session_id
        self._add_column_if_missing(
            table="messages",
            column="sender_session_id",
            column_type="TEXT",
        )
        # v0.6 — messages.intent (ADR-002). NOT NULL + DEFAULT 'triage' so
        # SQLite back-fills existing rows (every pre-ADR-002 message is
        # semantically a triage handoff, preserving backward-compat).
        self._add_column_if_missing(
            table="messages",
            column="intent",
            column_type="TEXT NOT NULL DEFAULT 'triage'",
        )

    _KNOWN_TABLES: frozenset[str] = frozenset({"agents", "messages"})

    def _add_column_if_missing(
        self,
        *,
        table: str,
        column: str,
        column_type: str,
    ) -> None:
        """Idempotently add ``column`` to ``table`` with the given type clause.

        Safe to call repeatedly: if the column already exists (fresh DB
        created from the current ``CREATE TABLE IF NOT EXISTS`` schema, or
        prior migration run), this is a no-op. The probe-then-ALTER race
        against a concurrent writer is closed by a ``BEGIN IMMEDIATE``
        transaction around the second probe + the ALTER itself.

        Args:
            table: target table name — must be a known table (whitelisted).
            column: column name to add.
            column_type: full SQL type clause minus ``ADD COLUMN``, e.g.
                ``TEXT`` or ``TEXT NOT NULL DEFAULT 'triage'``. NOT NULL +
                DEFAULT is required to back-fill existing rows; a bare
                ``TEXT`` produces NULL in old rows.
        """
        if table not in self._KNOWN_TABLES:
            raise ValueError(
                f"unknown table {table!r} — only {sorted(self._KNOWN_TABLES)} are allowed"
            )
        existing_cols = {
            row["name"]
            for row in self._conn.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()
        }
        if column in existing_cols:
            return

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-check inside the transaction in case a concurrent writer
            # (another Hermes profile starting up) raced us between the probe
            # above and here.
            cols = {
                row["name"]
                for row in self._conn.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }
            if column not in cols:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"
                )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def _row_to_message(self, r: sqlite3.Row) -> Message:
        """Build a :class:`Message` from a SQLite row.

        Used by :meth:`read_inbox` and :meth:`peek_inbox` to eliminate
        duplication.
        """
        metadata: dict[str, Any] | None = None
        if r["metadata"]:
            try:
                metadata = json.loads(r["metadata"])
            except json.JSONDecodeError:
                logger.warning(
                    "metadata JSON corrupt for message %s, returning None",
                    r["id"],
                )
                metadata = None

        return Message(
            id=r["id"],
            sender_id=r["sender_id"],
            recipient_id=r["recipient_id"],
            body=r["body"],
            metadata=metadata,
            created_at=datetime.fromisoformat(r["created_at"]),
            read_at=datetime.fromisoformat(r["read_at"]) if r["read_at"] else None,
            sender_session_id=r["sender_session_id"],
            intent=r["intent"],
        )

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
        intent: str = DEFAULT_INTENT,
    ) -> SendResult:
        if sender == recipient:
            raise ValueError("TARGET_SELF: cannot send to self")
        if len(body.encode("utf-8")) > MAX_BODY_BYTES:
            raise ValueError(f"MESSAGE_TOO_LARGE: body exceeds {MAX_BODY_BYTES} bytes")

        # ``intent`` is expected to already be normalised by the caller (the
        # tool layer in ``tools.py`` runs ``normalize_intent`` and logs the
        # downgrade warning). We defensively accept any value here but write
        # whatever string was provided — the NOT NULL constraint will reject
        # None, and the column accepts opaque strings so a future caller can
        # extend the enum without a schema change.
        if not isinstance(intent, str) or not intent:
            raise ValueError("INTENT_INVALID: intent must be a non-empty string")

        # Extract and validate the optional session_id convention (ADR-001 §4 #2).
        # The session_id travels inside the caller-supplied ``metadata`` dict so
        # that no new tool parameter is added to ``agent_send``; the bridge
        # simply recognises a well-known key, validates it, hoists it into a
        # dedicated column for query-ability, and leaves the rest of the dict
        # untouched for opaque forwarding.
        session_id: str | None = None
        if metadata is not None and "session_id" in metadata:
            raw = metadata["session_id"]
            if raw is not None:
                if not isinstance(raw, str):
                    raise ValueError(
                        "SESSION_ID_INVALID: session_id must be a string"
                    )
                # Enforce the limit in BYTES (UTF-8) to stay consistent with
                # MAX_BODY_BYTES / MAX_METADATA_BYTES. Using len() in chars
                # would silently let a 128-emoji session_id balloon to
                # ~512 bytes in the DB — see GLM review nit #1 on PR #12.
                if len(raw.encode("utf-8")) > MAX_SESSION_ID_BYTES:
                    raise ValueError(
                        f"SESSION_ID_TOO_LARGE: session_id exceeds {MAX_SESSION_ID_BYTES} bytes"
                    )
                session_id = raw

        metadata_json: str | None = None
        if metadata is not None:
            if isinstance(metadata, str):
                # Caller passed a raw JSON string — validate it parses.
                try:
                    json.loads(metadata)
                except json.JSONDecodeError as e:
                    raise ValueError(
                        "METADATA_INVALID: metadata string is not valid JSON"
                    ) from e
                metadata_json = metadata
            elif isinstance(metadata, dict):
                metadata_json = json.dumps(metadata, separators=(",", ":"))
            else:
                raise ValueError(
                    "METADATA_INVALID: metadata must be a dict, a JSON string, or None"
                )
            if len(metadata_json.encode("utf-8")) > MAX_METADATA_BYTES:
                raise ValueError(f"METADATA_TOO_LARGE: metadata exceeds {MAX_METADATA_BYTES} bytes")

        exists = self._conn.execute("SELECT 1 FROM agents WHERE id = ?", (recipient,)).fetchone()
        if not exists:
            raise ValueError(f"TARGET_UNKNOWN: {recipient}")

        message_id = uuid.uuid4().hex
        now = datetime.now(UTC)
        self._conn.execute(
            """
            INSERT INTO messages (id, sender_id, recipient_id, body, metadata, created_at, sender_session_id, intent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (message_id, sender, recipient, body, metadata_json, now.isoformat(), session_id, intent),
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
                    SELECT id, sender_id, recipient_id, body, metadata, created_at, read_at, sender_session_id, intent
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
                        SELECT id, sender_id, recipient_id, body, metadata, created_at, read_at, sender_session_id, intent
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
                SELECT id, sender_id, recipient_id, body, metadata, created_at, read_at, sender_session_id, intent
                FROM messages
                WHERE recipient_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (agent_id, limit),
            ).fetchall()

        return [self._row_to_message(r) for r in rows]

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
                SELECT id, sender_id, recipient_id, body, metadata, created_at, read_at, sender_session_id, intent
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
                SELECT id, sender_id, recipient_id, body, metadata, created_at, read_at, sender_session_id, intent
                FROM messages
                WHERE recipient_id = ? AND created_at >= ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (agent_id, since_ts, limit),
            ).fetchall()

        return [self._row_to_message(r) for r in rows]

    # -- real-time (long-poll) ---------------------------------------------

    _MAX_SUBSCRIBE_TIMEOUT: float = 55.0

    def subscribe(
        self,
        agent_id: str,
        timeout_seconds: float = 30.0,
        limit: int = 10,
    ) -> tuple[list[Message], bool]:
        """Long-poll for new messages using the filesystem signal.

        Returns ``(messages, timed_out)``.  Mirrors the behaviour of
        ``tool_agent_subscribe`` in ``tools.py`` — fast-path on pending
        messages, then block on ``SignalDir.wait()`` up to the capped
        timeout.

        Raises:
            RuntimeError: if no ``SignalDir`` was provided at
                construction **and** the fast-path found no pending
                messages (the local-filesystem subscribe path is
                unavailable).
        """
        timeout = max(0.0, min(timeout_seconds, self._MAX_SUBSCRIBE_TIMEOUT))
        limit = max(1, min(limit, 100))

        # Fast path: messages already waiting — no SignalDir needed.
        existing = self.read_inbox(agent_id, limit=limit, unread_only=True)
        if existing:
            return existing, False

        # Slow path: must block on filesystem signal.
        if self._signal_dir is None:
            raise RuntimeError(
                "Store.subscribe() requires a SignalDir — "
                "pass signal_dir= at construction, or use HttpBusStore"
            )

        fired = self._signal_dir.wait(agent_id, timeout_seconds=timeout)
        if not fired:
            return [], True

        messages = self.read_inbox(agent_id, limit=limit, unread_only=True)
        return messages, False
