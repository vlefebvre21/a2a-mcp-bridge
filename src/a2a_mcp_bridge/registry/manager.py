"""Main Capability Registry manager."""

from __future__ import annotations

import logging
import os
import threading

from .models import AgentInfo
from .storage import RegistryStorage

logger = logging.getLogger("a2a_mcp_bridge.registry")


class CapabilityRegistry:
    """Central registry for Hermes agent capabilities.

    Thread-safety: ``_cache`` is protected by an ``RLock``. The lock
    guards every mutation (``announce``) and every read
    (``query`` / ``get_agent`` / ``get_all_agents``). This prevents
    ``RuntimeError: dictionary changed size during iteration`` when
    :class:`HeartbeatManager._cleanup_stale_agents` mutates the cache
    from a background task while an MCP tool call is iterating it.

    Bounded cache: the in-memory ``_cache`` has an upper bound
    (``MAX_CACHED_AGENTS``, default 10 000, configurable via
    ``A2A_CAPABILITY_MAX_AGENTS``).  When the cache is at capacity,
    ``announce()`` for a *new* ``agent_id`` raises
    :class:`MCPValidationError`; updates to an already-cached agent_id
    are always allowed.  This prevents a hostile or buggy agent from
    blowing the bridge's RAM by announcing a million unique agents.
    """

    #: Hard upper bound on the number of distinct agents kept in the
    #: in-memory cache.  Override with the ``A2A_CAPABILITY_MAX_AGENTS``
    #: environment variable (parsed as int).
    MAX_CACHED_AGENTS: int = 10_000

    def __init__(self, db_path: str = "registry.db") -> None:
        self.storage = RegistryStorage(db_path)
        self._max_cached_agents = int(
            os.environ.get("A2A_CAPABILITY_MAX_AGENTS", self.MAX_CACHED_AGENTS)
        )
        self._cache: dict[str, AgentInfo] = {}
        self._lock = threading.RLock()
        # Warm cache from persistent storage
        for agent in self.storage.get_all_agents():
            self._cache[agent.agent_id] = agent

    # ── write ──────────────────────────────────────────────────────────

    def announce(self, agent: AgentInfo) -> None:
        """Register a new agent or update its capabilities.

        Raises:
            MCPValidationError: if the cache is at capacity and
                ``agent.agent_id`` is not already present (i.e. this
                would be a *new* entry rather than an update).
        """
        from ..exceptions import MCPValidationError

        with self._lock:
            if (
                agent.agent_id not in self._cache
                and len(self._cache) >= self._max_cached_agents
            ):
                raise MCPValidationError(
                    f"capability registry cache full "
                    f"({len(self._cache)}/{self._max_cached_agents}); "
                    f"cannot register new agent {agent.agent_id!r}"
                )
            # Persist to SQLite first (outside lock would be better for
            # throughput, but we keep the original ordering for safety).
            self.storage.register_agent(agent)
            self._cache[agent.agent_id] = agent

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
        with self._lock:
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

        # Filter by monetary cost ceiling (USD)
        if max_cost_usd is not None:
            agents = [
                a
                for a in agents
                if any(
                    cap.cost.monetary_cost_usd is not None
                    and cap.cost.monetary_cost_usd <= max_cost_usd
                    for cap in a.capabilities
                )
            ]

        return agents

    def get_agent(self, agent_id: str) -> AgentInfo | None:
        """Return cached agent info (or None)."""
        with self._lock:
            return self._cache.get(agent_id)

    def get_all_agents(self) -> list[AgentInfo]:
        """Return all cached agents."""
        with self._lock:
            return list(self._cache.values())
