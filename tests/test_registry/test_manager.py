"""Unit tests for CapabilityRegistry manager."""

from __future__ import annotations

from pathlib import Path

import pytest

from a2a_mcp_bridge.registry.manager import CapabilityRegistry
from a2a_mcp_bridge.registry.models import AgentInfo, Capability, CostModel


@pytest.fixture
def registry(tmp_path: Path) -> CapabilityRegistry:
    db_path = tmp_path / "registry.db"
    return CapabilityRegistry(str(db_path))


def _make_agent(agent_id: str = "test-agent", name: str = "Test Agent") -> AgentInfo:
    return AgentInfo(
        agent_id=agent_id,
        name=name,
        capabilities=[
            Capability(
                skill_id="code-review-python",
                description="Performs Python code reviews",
                domain="code",
                cost=CostModel(tokens_per_call=200, latency_ms=800),
            ),
            Capability(
                skill_id="diagram-generation",
                description="Generates architecture diagrams",
                domain="creative",
                cost=CostModel(tokens_per_call=80, latency_ms=400, monetary_cost_usd=0.002),
            ),
        ],
    )


def test_announce_and_get(registry: CapabilityRegistry):
    agent = _make_agent()
    registry.announce(agent)

    found = registry.get_agent("test-agent")
    assert found is not None
    assert found.name == "Test Agent"
    assert len(found.capabilities) == 2


def test_announce_updates_existing(registry: CapabilityRegistry):
    v1 = _make_agent(name="v1")
    registry.announce(v1)

    v2 = _make_agent(name="v2")
    v2.capabilities = [
        Capability(
            skill_id="new-skill",
            description="Brand new",
            domain="test",
            cost=CostModel(tokens_per_call=10, latency_ms=50),
        )
    ]
    registry.announce(v2)

    found = registry.get_agent("test-agent")
    assert found is not None
    assert found.name == "v2"
    assert len(found.capabilities) == 1
    assert found.capabilities[0].skill_id == "new-skill"


def test_get_all_agents(registry: CapabilityRegistry):
    registry.announce(_make_agent("a1", "Agent 1"))
    registry.announce(_make_agent("a2", "Agent 2"))

    all_agents = registry.get_all_agents()
    assert len(all_agents) == 2
    names = {a.name for a in all_agents}
    assert names == {"Agent 1", "Agent 2"}


def test_query_by_keyword(registry: CapabilityRegistry):
    registry.announce(_make_agent())

    # Match skill_id
    results = registry.query(keyword="python")
    assert len(results) == 1

    # Match domain
    results = registry.query(keyword="creative")
    assert len(results) == 1

    # No match
    results = registry.query(keyword="rust")
    assert len(results) == 0


def test_query_by_max_cost(registry: CapabilityRegistry):
    registry.announce(_make_agent())

    # Agent has a capability with monetary_cost_usd=0.002
    results = registry.query(max_cost=0.005)
    assert len(results) == 1

    # Too low — no match
    results = registry.query(max_cost=0.001)
    assert len(results) == 0


def test_query_filters_offline(registry: CapabilityRegistry):
    agent = _make_agent()
    agent.status = "offline"
    registry.announce(agent)

    results = registry.query()
    assert len(results) == 0


def test_get_agent_missing(registry: CapabilityRegistry):
    assert registry.get_agent("nonexistent") is None
