#!/usr/bin/env python3
"""Cross-host propagation via HTTP facade (ADR-008 support)."""

import aiohttp
import logging
from dataclasses import dataclass, asdict
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class CapabilityAnnouncement:
    """Serialized capability announcement packet."""
    agent_id: str
    skill_id: str
    domain: Optional[str] = None
    description: Optional[str] = None
    monetary_cost_usd: Optional[float] = None
    tokens_per_call: Optional[int] = None


class AsyncHTTPBusClient:
    """Fire-and-forget HTTP client for capability propagation."""

    def __init__(
        self,
        remote_url: Optional[str] = None,
        timeout_ms: int = 200,  # Short timeout for fire-and-forget
    ):
        self.remote_url = remote_url or None
        self.timeout_ms = timeout_ms
        
    async def propagate_capability(
        self,
        announcement: CapabilityAnnouncement
    ) -> None:
        """Fire-and-forget propagation to remote facade.
        
        Logs errors but does not raise (fire-and-forget semantics).
        """
        if not self.remote_url:
            return  # No remote to propagate to
            
        try:
            payload = asdict(announcement)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.remote_url.strip('/')}/api/v1/capability-announce",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=self.timeout_ms/1000),
                ) as resp:
                    if resp.status not in (200, 201):
                        logger.debug(
                            "Remote capability propagation failed: %s (%s)",
                            self.remote_url, resp.status
                        )
        except Exception as e:
            # Fire-and-forget: log but don't fail local operation
            logger.debug("Capability propagate %s error: %s", self.remote_url, e)


# Singleton for local development testing
def get_propagator(remote_url: Optional[str] = None) -> AsyncHTTPBusClient:
    """Factory for propagation client."""
    return AsyncHTTPBusClient(remote_url=remote_url)
