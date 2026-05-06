"""Unit tests for RegistryQuery — discover_all + find_best."""

from __future__ import annotations

from pathlib import Path

import pytest

from a2a_mcp_bridge.registry.manager import CapabilityRegistry
from a2a_mcp_bridge.registry.models import AgentInfo, Capability, CostModel
from a2a_mcp_bridge.registry.query import RegistryQuery


@pytest.fixture
def query(tmp_path: Path) -> RegistryQuery:
    db_path = tmp_path / "registry.db"
    registry = CapabilityRegistry(str(db_path))
    return RegistryQuery(registry)


def _make_agent(
    agent_id: str = "agent-1",
    name: str = "Agent One",
    skills: list[tuple[str, str, float]] | None = None,
) -> AgentInfo:
    if skills is None:
        skills = [
            ("code-review-python", "Python code reviews", 200),
            ("arxiv-search", "Search arXiv papers", 500),
        ]
    return AgentInfo(
        agent_id=agent_id,
        name=name,
        capabilities=[
            Capability(
                skill_id=sid,
                description=desc,
                domain="code" if "code" in sid else "research",
                cost=CostModel(tokens_per_call=tokens, latency_ms=800, monetary_cost_usd=tokens * 0.00001),
            )
            for sid, desc, tokens in skills
        ],
    )


def test_discover_all_empty(query: RegistryQuery):
    result = query.discover_all()
    assert result == []


def test_discover_all(query: RegistryQuery):
    query.registry.announce(_make_agent())

    result = query.discover_all()
    assert len(result) == 2

    # Check structure
    entry = result[0]
    assert "agent_id" in entry
    assert "agent_name" in entry
    assert "skill_id" in entry
    assert "description" in entry
    assert "domain" in entry
    assert "cost" in entry
    assert "max_context_tokens" in entry
    assert "version" in entry
    assert "supports_streaming" in entry
    assert "permissions" in entry


def test_discover_all_multiple_agents(query: RegistryQuery):
    query.registry.announce(_make_agent("a1", "Agent 1"))
    query.registry.announce(_make_agent("a2", "Agent 2"))

    result = query.discover_all()
    assert len(result) == 4  # 2 capabilities x 2 agents


def test_find_best_matching(query: RegistryQuery):
    query.registry.announce(_make_agent())

    results = query.find_best("python")
    assert len(results) == 1
    assert results[0]["skill_id"] == "code-review-python"
    assert results[0]["score"] == 1.0


def test_find_best_no_match(query: RegistryQuery):
    query.registry.announce(_make_agent())

    results = query.find_best("rust")
    assert len(results) == 0


def test_find_best_cost_penalty(query: RegistryQuery):
    # Agent with expensive skill
    agent = _make_agent(skills=[("expensive-skill", "Very expensive", 5000)])
    query.registry.announce(agent)

    # Within budget → score 1.0
    results = query.find_best("expensive", max_tokens=10000)
    assert len(results) == 1
    assert results[0]["score"] == 1.0

    # Over budget → score 0.5
    results = query.find_best("expensive", max_tokens=100)
    assert len(results) == 1
    assert results[0]["score"] == 0.5


def test_find_best_sorted_by_score_then_cost(query: RegistryQuery):
    # Two agents with same skill keyword but different costs
    cheap = _make_agent(
        "cheap-agent",
        "Cheap",
        [("code-review", "Code review cheap", 100)],
    )
    expensive = _make_agent(
        "expensive-agent",
        "Expensive",
        [("code-review", "Code review expensive", 500)],
    )
    query.registry.announce(cheap)
    query.registry.announce(expensive)

    results = query.find_best("code-review")
    assert len(results) == 2
    # Both score 1.0, but cheap first (lower cost)
    assert results[0]["agent_id"] == "cheap-agent"
    assert results[1]["agent_id"] == "expensive-agent"


def test_find_best_includes_agent_name(query: RegistryQuery):
    query.registry.announce(_make_agent())

    results = query.find_best("python")
    assert len(results) == 1
    assert results[0]["agent_name"] == "Agent One"
    assert results[0]["description"] == "Python code reviews"
