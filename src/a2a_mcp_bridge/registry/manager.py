"""Main Capability Registry manager."""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from .models import AgentInfo
from .storage import RegistryStorage

logger = logging.getLogger("a2a_mcp_bridge.registry")


class CapabilityRegistry:
    """Central registry for Hermes agent capabilities."""

    def __init__(self, db_path: str = "registry.db") -> None:
        self.storage = RegistryStorage(db_path)
        self._cache: Dict[str, AgentInfo] = {}
        # Warm cache from persistent storage
        for agent in self.storage.get_all_agents():
            self._cache[agent.agent_id] = agent

    # ── write ──────────────────────────────────────────────────────────

    def announce(self, agent: AgentInfo) -> None:
        """Register a new agent or update its capabilities."""
        self.storage.register_agent(agent)
        self._cache[agent.agent_id] = agent
        logger.info(
            "Agent %r registered with %d capabilities",
            agent.name,
            len(agent.capabilities),
        )

    # ── read ───────────────────────────────────────────────────────────

    def query(self, keyword: str = "", max_cost: float | None = None) -> List[AgentInfo]:
        """Query agents by keyword or cost ceiling.

        For now this is a simple filter — can be enhanced with scoring later.
        """
        agents = list(self._cache.values())

        # Filter by status
        agents = [a for a in agents if a.status == "online"]

        # Filter by keyword (match against skill_id, description, domain)
        if keyword:
            kw = keyword.lower()
            agents = [
                a
                for a in agents
                if any(
                    kw in cap.skill_id.lower()
                    or kw in cap.description.lower()
                    or kw in cap.domain.lower()
                    for cap in a.capabilities
                )
            ]

        # Filter by monetary cost ceiling
        if max_cost is not None:
            agents = [
                a
                for a in agents
                if any(
                    cap.cost.monetary_cost_usd is not None and cap.cost.monetary_cost_usd <= max_cost
                    for cap in a.capabilities
                )
            ]

        return agents

    def get_agent(self, agent_id: str) -> Optional[AgentInfo]:
        """Return cached agent info (or None)."""
        return self._cache.get(agent_id)

    def get_all_agents(self) -> List[AgentInfo]:
        """Return all cached agents."""
        return list(self._cache.values())
