"""Unit tests for Capability Registry models."""

from __future__ import annotations

from datetime import datetime

import pytest

from a2a_mcp_bridge.registry.models import AgentInfo, Capability, CostModel


def test_cost_model():
    cost = CostModel(tokens_per_call=150.0, latency_ms=450, type="local")
    assert cost.tokens_per_call == 150.0
    assert cost.type == "local"
    assert cost.monetary_cost_usd is None


def test_capability():
    cap = Capability(
        skill_id="code-review-python",
        description="Performs Python code reviews",
        domain="code",
        cost=CostModel(tokens_per_call=200, latency_ms=800),
    )
    assert cap.skill_id == "code-review-python"
    assert cap.supports_streaming is False
    assert cap.permissions == ["read"]
    assert cap.version == "1.0.0"


def test_agent_info():
    agent = AgentInfo(
        agent_id="hermes-code-01",
        name="Python Specialist",
        capabilities=[
            Capability(
                skill_id="code-review",
                description="...",
                domain="code",
                cost=CostModel(tokens_per_call=100, latency_ms=300),
            )
        ],
    )
    assert agent.name == "Python Specialist"
    assert len(agent.capabilities) == 1
    assert agent.status == "online"
    assert isinstance(agent.last_heartbeat, datetime)
