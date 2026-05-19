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


def test_query_by_max_cost_usd(registry: CapabilityRegistry):
    registry.announce(_make_agent())

    # Agent has a capability with monetary_cost_usd=0.002
    results = registry.query(max_cost_usd=0.005)
    assert len(results) == 1

    # Too low — no match
    results = registry.query(max_cost_usd=0.001)
    assert len(results) == 0


def test_query_filters_offline(registry: CapabilityRegistry):
    agent = _make_agent()
    agent.status = "offline"
    registry.announce(agent)

    results = registry.query()
    assert len(results) == 0


def test_get_agent_missing(registry: CapabilityRegistry):
    assert registry.get_agent("nonexistent") is None


def test_announce_at_cap_allows_update_for_existing_agent(registry: CapabilityRegistry):
    """Announcing an already-cached agent_id is always allowed, even at cap."""

    # Set a tiny cap for testing
    registry._max_cached_agents = 2
    registry.announce(_make_agent("a1", "Agent 1"))
    registry.announce(_make_agent("a2", "Agent 2"))

    # Updating an existing agent at cap must succeed
    v2 = _make_agent("a1", "Agent 1 v2")
    v2.capabilities = [
        Capability(
            skill_id="updated-skill",
            description="Updated",
            domain="test",
            cost=CostModel(tokens_per_call=5, latency_ms=10),
        )
    ]
    registry.announce(v2)  # should NOT raise

    found = registry.get_agent("a1")
    assert found is not None
    assert found.name == "Agent 1 v2"
    assert found.capabilities[0].skill_id == "updated-skill"


def test_announce_at_cap_raises_for_new_agent(registry: CapabilityRegistry):
    """Announcing a new agent_id when the cache is at capacity raises MCPValidationError."""
    from a2a_mcp_bridge.exceptions import MCPValidationError

    # Set a tiny cap for testing
    registry._max_cached_agents = 2
    registry.announce(_make_agent("a1", "Agent 1"))
    registry.announce(_make_agent("a2", "Agent 2"))

    # A brand-new agent_id at cap must raise
    with pytest.raises(MCPValidationError, match="capability registry cache full"):
        registry.announce(_make_agent("a3", "Agent 3"))

    # Cache size must not have grown
    assert len(registry.get_all_agents()) == 2


def test_announce_max_agents_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A2A_CAPABILITY_MAX_AGENTS env var overrides the default cap."""
    from a2a_mcp_bridge.exceptions import MCPValidationError

    monkeypatch.setenv("A2A_CAPABILITY_MAX_AGENTS", "1")
    db_path = tmp_path / "registry.db"
    reg = CapabilityRegistry(str(db_path))

    reg.announce(_make_agent("a1", "Agent 1"))

    with pytest.raises(MCPValidationError, match="capability registry cache full"):
        reg.announce(_make_agent("a2", "Agent 2"))


def test_concurrent_announce_and_query_no_race(registry: CapabilityRegistry):
    """Regression guard: announce() and query() must be safe under concurrency.

    Before the RLock on _cache, ``HeartbeatManager._cleanup_stale_agents``
    mutating the cache from a background task while an MCP tool was
    iterating ``self._cache.values()`` could raise
    ``RuntimeError: dictionary changed size during iteration``.

    This test exercises that pattern directly by hammering announce() and
    query()/get_all_agents() from multiple threads for a short burst.
    Any concurrent-mutation bug will raise; a clean run proves the lock
    serialises access correctly.
    """
    import threading as _threading

    errors: list[BaseException] = []
    stop = _threading.Event()

    def writer() -> None:
        i = 0
        while not stop.is_set():
            try:
                registry.announce(_make_agent(f"a{i % 50}", f"Agent {i}"))
                i += 1
            except BaseException as exc:
                errors.append(exc)
                return

    def reader() -> None:
        while not stop.is_set():
            try:
                registry.query(keyword="python")
                registry.get_all_agents()
            except BaseException as exc:
                errors.append(exc)
                return

    threads = [
        _threading.Thread(target=writer),
        _threading.Thread(target=writer),
        _threading.Thread(target=reader),
        _threading.Thread(target=reader),
    ]
    for t in threads:
        t.start()

    # Hammer for 200 ms — enough to expose a missing lock.
    stop.wait(0.2)
    stop.set()
    for t in threads:
        t.join(timeout=2.0)

    assert errors == [], f"concurrent access raised: {errors!r}"
