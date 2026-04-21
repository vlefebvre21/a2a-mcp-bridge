"""SQLite-backed repository for a2a-mcp-bridge."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import AgentRecord, Message, SendResult

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

    def close(self) -> None:
        self._conn.close()

    # -- agents ------------------------------------------------------------

    def upsert_agent(self, agent_id: str, metadata: dict[str, Any] | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO agents (id, first_seen_at, last_seen_at, metadata)
            VALUES (?, ?, ?, NULL)
            ON CONFLICT(id) DO UPDATE SET last_seen_at = excluded.last_seen_at
            """,
            (agent_id, now, now),
        )

    def list_agents(self, active_within_days: int = 7) -> list[AgentRecord]:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=active_within_days)
        ).isoformat()
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
