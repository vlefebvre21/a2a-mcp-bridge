"""Heartbeat and live status management for registered agents."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime

from .manager import CapabilityRegistry

logger = logging.getLogger("a2a_mcp_bridge.registry.heartbeat")


class HeartbeatManager:
    """Manages periodic heartbeats and agent status updates."""

    def __init__(self, registry: CapabilityRegistry, interval_seconds: int = 30) -> None:
        self.registry = registry
        self.interval = interval_seconds
        self._last_heartbeat: dict[str, float] = {}
        self._running = False
        self._task: asyncio.Task | None = None

    # ── lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the heartbeat monitoring task."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._heartbeat_loop())
        logger.info("Heartbeat monitor started (interval=%ds)", self.interval)

    async def stop(self) -> None:
        """Stop the heartbeat task."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("Heartbeat monitor stopped")

    # ── heartbeat API ──────────────────────────────────────────────────

    def ping(self, agent_id: str) -> None:
        """Called by agents to signal they are still alive."""
        self._last_heartbeat[agent_id] = time.time()
        logger.debug("Agent %s pinged at %s", agent_id, datetime.utcnow().isoformat())

    # ── internal ───────────────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        """Background loop that cleans up stale agents."""
        while self._running:
            try:
                await asyncio.sleep(self.interval)
                self._cleanup_stale_agents()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in heartbeat loop")
                await asyncio.sleep(5)

    def _cleanup_stale_agents(self) -> None:
        """Mark agents as offline if no heartbeat for > 2x interval."""
        now = time.time()
        threshold = now - (self.interval * 2)

        for agent_id, last_time in list(self._last_heartbeat.items()):
            if last_time < threshold:
                agent = self.registry.get_agent(agent_id)
                if agent and agent.status != "offline":
                    logger.warning("Agent %s marked as offline (stale heartbeat)", agent_id)
                    agent.status = "offline"
                    self.registry.announce(agent)  # persist to DB + cache
