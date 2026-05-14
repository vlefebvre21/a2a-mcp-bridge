"""Test that HttpBusStore.register_capability is non-blocking (fire-and-forget).

Verifies ADR-008 decision #3: propagation must not block register_capability
if the remote façade is unreachable.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from a2a_mcp_bridge.bus_store import HttpBusStore


def _make_http_store() -> HttpBusStore:
    """Create an HttpBusStore with a fully mocked _client and _propagation_pool."""
    fake_httpx = MagicMock()
    fake_httpx.HTTPError = Exception
    fake_httpx.HTTPStatusError = Exception

    import a2a_mcp_bridge.bus_store as mod
    mod.httpx = fake_httpx

    store = HttpBusStore.__new__(HttpBusStore)
    store._base_url = "http://localhost:8443/bus"
    store._agent_id = "test-agent"
    store._timeout = 65.0
    store._httpx = fake_httpx
    store._client = MagicMock()

    # Create a real ThreadPoolExecutor for the propagation pool
    from concurrent.futures import ThreadPoolExecutor
    store._propagation_pool = ThreadPoolExecutor(
        max_workers=2, thread_name_prefix="cap-propagate"
    )

    return store


class TestRegisterCapabilityNonBlocking:
    """Tests for non-blocking register_capability (ADR-008 decision #3)."""

    def test_register_capability_does_not_block_on_dead_facade(self) -> None:
        """Propagation must not block register_capability if facade is unreachable.

        Uses a real ThreadPoolExecutor: the HTTP POST will fail quickly because
        _client is a MagicMock that returns a response with raise_for_status
        raising an error. The key assertion is that register_capability returns
        in <200ms, proving fire-and-forget semantics.
        """
        store = _make_http_store()

        # Make the mock client.post raise an error (simulating unreachable facade)
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("connection refused")
        store._client.post.return_value = mock_resp

        start = time.monotonic()
        store.register_capability("agent-x", "skill-y", domain="test")
        elapsed = time.monotonic() - start

        # register_capability should return almost immediately (<200ms)
        # because the HTTP POST is submitted to the pool, not awaited.
        assert elapsed < 0.2, (
            f"register_capability blocked for {elapsed:.2f}s — "
            "propagation is not fire-and-forget"
        )

        # Clean up
        store._propagation_pool.shutdown(wait=True)
        del store

    def test_register_capability_submits_to_pool(self) -> None:
        """register_capability should submit to the propagation pool, not call _client.post directly."""
        store = _make_http_store()

        # Replace the real pool with a mock that tracks submissions but doesn't execute
        mock_pool = MagicMock()
        submissions = []

        def tracking_submit(fn, *args, **kwargs):
            submissions.append((fn, args, kwargs))

        mock_pool.submit = tracking_submit
        store._propagation_pool = mock_pool

        store.register_capability("agent-x", "skill-y", domain="test")

        # Should have submitted _sync_propagate to the pool
        assert len(submissions) == 1
        assert submissions[0][0].__name__ == "_sync_propagate"
        assert submissions[0][1][0]["agent_id"] == "agent-x"
        assert submissions[0][1][0]["skill_id"] == "skill-y"

        # _client.post should NOT have been called since the pool mock doesn't execute
        assert not store._client.post.called

        # Clean up
        del store
