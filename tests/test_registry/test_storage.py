"""Unit tests for RegistryStorage with SQLite."""

from __future__ import annotations

from pathlib import Path

import pytest

from a2a_mcp_bridge.registry.models import AgentInfo, Capability, CostModel
from a2a_mcp_bridge.registry.storage import RegistryStorage


@pytest.fixture
def storage(tmp_path: Path) -> RegistryStorage:
    """Fresh RegistryStorage backed by a temp SQLite file."""
    db_path = tmp_path / "registry.db"
    return RegistryStorage(str(db_path))


def test_register_and_get_all(storage: RegistryStorage):
    agent = AgentInfo(
        agent_id="test-agent-1",
        name="Test Agent",
        capabilities=[
            Capability(
                skill_id="test-skill",
                description="A test skill",
                domain="test",
                cost=CostModel(tokens_per_call=50, latency_ms=100),
            )
        ],
    )

    storage.register_agent(agent)
    agents = storage.get_all_agents()

    assert len(agents) == 1
    assert agents[0].name == "Test Agent"
    assert agents[0].capabilities[0].skill_id == "test-skill"


def test_register_updates_existing(storage: RegistryStorage):
    """Registering the same agent_id replaces capabilities."""
    agent_v1 = AgentInfo(
        agent_id="dup-agent",
        name="Dup Agent v1",
        capabilities=[
            Capability(
                skill_id="skill-a",
                description="A",
                domain="test",
                cost=CostModel(tokens_per_call=10, latency_ms=50),
            )
        ],
    )
    agent_v2 = AgentInfo(
        agent_id="dup-agent",
        name="Dup Agent v2",
        capabilities=[
            Capability(
                skill_id="skill-b",
                description="B",
                domain="test",
                cost=CostModel(tokens_per_call=20, latency_ms=100),
            ),
            Capability(
                skill_id="skill-c",
                description="C",
                domain="test",
                cost=CostModel(tokens_per_call=30, latency_ms=150),
            ),
        ],
    )

    storage.register_agent(agent_v1)
    storage.register_agent(agent_v2)

    agents = storage.get_all_agents()
    assert len(agents) == 1
    assert agents[0].name == "Dup Agent v2"
    assert len(agents[0].capabilities) == 2
    skill_ids = {c.skill_id for c in agents[0].capabilities}
    assert skill_ids == {"skill-b", "skill-c"}


def test_get_agent_by_id(storage: RegistryStorage):
    agent = AgentInfo(
        agent_id="lookup-agent",
        name="Lookup Agent",
        capabilities=[],
    )
    storage.register_agent(agent)

    found = storage.get_agent("lookup-agent")
    assert found is not None
    assert found.name == "Lookup Agent"

    missing = storage.get_agent("nonexistent")
    assert missing is None
