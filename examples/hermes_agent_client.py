"""Example: Hermes Agent Client with Announcement + Heartbeat.

This demonstrates how a specialized Hermes agent announces its capabilities
to the a2a-mcp-bridge and sends periodic heartbeat pings so the registry
knows it is still alive.

Usage:
    python -m examples.hermes_agent_client

Adapt ``bridge_client.send_announce()`` and ``bridge_client.send_heartbeat()``
to your actual A2A/MCP client implementation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from a2a_mcp_bridge.registry.models import AgentInfo, Capability, CostModel

logger = logging.getLogger(__name__)


class HermesAgentClient:
    """Example client for a specialized Hermes agent."""

    def __init__(self, bridge_client: Any, agent_id: str, name: str) -> None:
        self.bridge_client = bridge_client  # your A2A/MCP client
        self.agent_info = AgentInfo(
            agent_id=agent_id,
            name=name,
            capabilities=self._define_capabilities(),
            status="online",
        )
        self._heartbeat_task: asyncio.Task | None = None

    def _define_capabilities(self) -> list[Capability]:
        """Define what this specialized agent can do."""
        return [
            Capability(
                skill_id="code-review-python",
                description="Performs high-quality Python code reviews with security and performance focus",
                domain="code",
                cost=CostModel(tokens_per_call=120.0, latency_ms=650, type="local"),
                supports_streaming=True,
                max_context_tokens=128000,
                permissions=["read", "analyze"],
            ),
            Capability(
                skill_id="diagram-generation",
                description="Generates architecture and flow diagrams",
                domain="creative",
                cost=CostModel(tokens_per_call=80.0, latency_ms=400, type="local"),
                supports_streaming=False,
            ),
        ]

    async def start(self) -> None:
        """Announce capabilities and start heartbeat."""
        # 1. Announce to the bridge
        await self.bridge_client.send_announce(self.agent_info.model_dump())
        logger.info("%s: announced capabilities to bridge", self.agent_info.name)

        # 2. Start periodic heartbeat
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info("%s: heartbeat started", self.agent_info.name)

    async def _heartbeat_loop(self) -> None:
        """Send heartbeat every 20 seconds."""
        while True:
            try:
                self.bridge_client.send_heartbeat(self.agent_info.agent_id)
                await asyncio.sleep(20)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("%s: heartbeat error", self.agent_info.name)
                await asyncio.sleep(5)

    async def stop(self) -> None:
        """Graceful shutdown."""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        logger.info("%s: shutdown complete", self.agent_info.name)


# ---------------------------------------------------------------------------
# Usage example
# ---------------------------------------------------------------------------

async def main() -> None:
    """Run the demo agent for 5 minutes."""
    # Assume you have a bridge_client already connected
    # bridge_client = your_a2a_mcp_client(...)

    # agent = HermesAgentClient(
    #     bridge_client=bridge_client,
    #     agent_id="hermes-python-specialist-01",
    #     name="Python Specialist",
    # )
    # await agent.start()
    # try:
    #     await asyncio.sleep(300)  # demo: run for 5 minutes
    # finally:
    #     await agent.stop()
    print("See docstring at the top of this file for usage instructions.")


if __name__ == "__main__":
    asyncio.run(main())
