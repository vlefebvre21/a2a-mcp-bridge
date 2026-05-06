"""SQLite persistence layer for Capability Registry."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .models import AgentInfo, Capability

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    last_heartbeat TEXT NOT NULL,
    metadata TEXT
);
CREATE TABLE IF NOT EXISTS capabilities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    capability_json TEXT NOT NULL,
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
);
"""


class RegistryStorage:
    """SQLite-based persistent storage for the Capability Registry."""

    def __init__(self, db_path: str = "registry.db") -> None:
        self.db_path = Path(db_path)
        self._init_db()

    def _init_db(self) -> None:
        conn = sqlite3.connect(str(self.db_path))
        conn.executescript(_SCHEMA)
        conn.commit()
        conn.close()

    # ── write ──────────────────────────────────────────────────────────

    def register_agent(self, agent: AgentInfo) -> None:
        """Save or update agent and its capabilities."""
        conn = sqlite3.connect(str(self.db_path))

        conn.execute(
            """
            INSERT OR REPLACE INTO agents
            (agent_id, name, status, last_heartbeat, metadata)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                agent.agent_id,
                agent.name,
                agent.status,
                agent.last_heartbeat.isoformat(),
                json.dumps(agent.metadata),
            ),
        )

        # Replace capabilities for this agent
        conn.execute("DELETE FROM capabilities WHERE agent_id = ?", (agent.agent_id,))
        for cap in agent.capabilities:
            conn.execute(
                "INSERT INTO capabilities (agent_id, skill_id, capability_json) VALUES (?, ?, ?)",
                (agent.agent_id, cap.skill_id, cap.model_dump_json()),
            )

        conn.commit()
        conn.close()

    # ── read ───────────────────────────────────────────────────────────

    def get_all_agents(self) -> list[AgentInfo]:
        """Load all agents with their capabilities."""
        conn = sqlite3.connect(str(self.db_path))
        agents: list[AgentInfo] = []

        for row in conn.execute("SELECT * FROM agents"):
            agent_id, name, status, heartbeat_str, metadata_json = row
            capabilities: list[Capability] = []
            for cap_row in conn.execute(
                "SELECT capability_json FROM capabilities WHERE agent_id = ?",
                (agent_id,),
            ):
                capabilities.append(Capability.model_validate_json(cap_row[0]))

            agents.append(
                AgentInfo(
                    agent_id=agent_id,
                    name=name,
                    status=status,
                    last_heartbeat=datetime.fromisoformat(heartbeat_str),
                    metadata=json.loads(metadata_json) if metadata_json else {},
                    capabilities=capabilities,
                )
            )

        conn.close()
        return agents

    def get_agent(self, agent_id: str) -> AgentInfo | None:
        """Load a single agent by ID (or None if not found)."""
        for agent in self.get_all_agents():
            if agent.agent_id == agent_id:
                return agent
        return None
