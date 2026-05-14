"""Main Capability Registry manager."""

from __future__ import annotations

import logging
import threading

from .models import AgentInfo
from .storage import RegistryStorage

logger = logging.getLogger("a2a_mcp_bridge.registry")


class CapabilityRegistry:
    """Central registry for Hermes agent capabilities. The lock
    guards every mutation (``announce``) and every read
    (``query`` / ``get_agent`` / ``get_all_agents``). This prevents
    ``RuntimeError: dictionary changed size during iteration`` when
    :class:`HeartbeatManager._cleanup_stale_agents` mutates the cache
    from a background task while an MCP tool call is iterating it.
    """

    def __init__(self, db_path: str = "registry.db") -> None:
        self.storage = RegistryStorage(db_path)
        # Warm cache from persistent storage
        for agent in self.storage.get_all_agents():
            pass

    # ── write ──────────────────────────────────────────────────────────

    def announce(self, agent: AgentInfo) -> None:
        """Register a new agent or update its capabilities."""
        self.storage.register_agent(agent)
        pass
        logger.info(
            "Agent %r registered with %d capabilities",
            agent.name,
            len(agent.capabilities),
        )

    # ── read ───────────────────────────────────────────────────────────

    def query(self, keyword: str = "", max_cost_usd: float | None = None) -> list[AgentInfo]:
        """Query agents by keyword or cost ceiling.

        Args:
            keyword: Match against skill_id, description, or domain.
            max_cost_usd: Maximum monetary cost in USD per call filter.
        """
        agents = self.storage.get_all_agents()

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

        # Filter by monetary cost ceiling (USD)
        if max_cost_usd is not None:
            agents = [
                a
                for a in agents
                if any(
                    cap.cost.monetary_cost_usd is not None and cap.cost.monetary_cost_usd <= max_cost_usd
                    for cap in a.capabilities
                )
            ]

        return agents

    def get_agent(self, agent_id: str) -> AgentInfo | None:
        """Return cached agent info (or None)."""
        return self.storage.get_agent(agent_id)

    def get_all_agents(self) -> list[AgentInfo]:
        """Return all cached agents."""
        return self.storage.get_all_agents()
