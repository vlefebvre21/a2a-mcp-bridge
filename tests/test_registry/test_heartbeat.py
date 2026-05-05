"""Unit tests for HeartbeatManager — ping, stale cleanup, lifecycle."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from a2a_mcp_bridge.registry.heartbeat import HeartbeatManager
from a2a_mcp_bridge.registry.manager import CapabilityRegistry
from a2a_mcp_bridge.registry.models import AgentInfo, Capability, CostModel


@pytest.fixture
def registry(tmp_path: Path) -> CapabilityRegistry:
    db_path = tmp_path / "registry.db"
    return CapabilityRegistry(str(db_path))


@pytest.fixture
def heartbeat(registry: CapabilityRegistry) -> HeartbeatManager:
    return HeartbeatManager(registry, interval_seconds=5)


def _make_agent(agent_id: str = "hb-agent") -> AgentInfo:
    return AgentInfo(
        agent_id=agent_id,
        name="HB Agent",
        capabilities=[
            Capability(
                skill_id="test-skill",
                description="test",
                domain="test",
                cost=CostModel(tokens_per_call=10, latency_ms=50),
            )
        ],
    )


def test_ping_records_timestamp(heartbeat: HeartbeatManager):
    heartbeat.ping("agent-1")
    assert "agent-1" in heartbeat._last_heartbeat
    assert heartbeat._last_heartbeat["agent-1"] > 0


def test_cleanup_stale_marks_offline(registry: CapabilityRegistry, heartbeat: HeartbeatManager):
    agent = _make_agent()
    registry.announce(agent)

    # Simulate a stale ping (long ago)
    import time
    heartbeat._last_heartbeat["hb-agent"] = time.time() - (heartbeat.interval * 3)

    heartbeat._cleanup_stale_agents()

    updated = registry.get_agent("hb-agent")
    assert updated is not None
    assert updated.status == "offline"


def test_cleanup_stale_leaves_fresh_alone(registry: CapabilityRegistry, heartbeat: HeartbeatManager):
    agent = _make_agent()
    registry.announce(agent)

    # Fresh ping
    heartbeat.ping("hb-agent")

    heartbeat._cleanup_stale_agents()

    updated = registry.get_agent("hb-agent")
    assert updated is not None
    assert updated.status == "online"


def test_cleanup_stale_skips_already_offline(registry: CapabilityRegistry, heartbeat: HeartbeatManager):
    agent = _make_agent()
    agent.status = "offline"
    registry.announce(agent)

    import time
    heartbeat._last_heartbeat["hb-agent"] = time.time() - 999

    # Should not crash or double-process
    heartbeat._cleanup_stale_agents()

    updated = registry.get_agent("hb-agent")
    assert updated is not None
    assert updated.status == "offline"


@pytest.mark.asyncio
async def test_start_and_stop(heartbeat: HeartbeatManager):
    await heartbeat.start()
    assert heartbeat._running is True
    assert heartbeat._task is not None

    await heartbeat.stop()
    assert heartbeat._running is False
    assert heartbeat._task is None


@pytest.mark.asyncio
async def test_start_idempotent(heartbeat: HeartbeatManager):
    await heartbeat.start()
    await heartbeat.start()  # should not create a second task
    assert heartbeat._running is True

    await heartbeat.stop()


@pytest.mark.asyncio
async def test_stop_when_not_started(heartbeat: HeartbeatManager):
    # Should not raise
    await heartbeat.stop()
    assert heartbeat._running is False
