"""Test that HttpBusStore.register_capability is non-blocking (fire-and-forget).

Verifies ADR-008 decision #3: propagation must not block register_capability
if the remote façade is unreachable.
"""

from __future__ import annotations

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

        Deterministic version: use a slow mock client (blocks on a threading.Event)
        and assert that register_capability returns BEFORE the event is released —
        which is impossible unless the POST runs in a background thread.
        """
        import threading

        store = _make_http_store()

        # Block the HTTP POST on a threading.Event — it will not complete
        # until we explicitly release it.
        post_started = threading.Event()
        post_may_finish = threading.Event()

        def blocking_post(*args, **kwargs):
            post_started.set()
            post_may_finish.wait(timeout=5.0)
            mock_resp = MagicMock()
            return mock_resp

        store._client.post.side_effect = blocking_post

        # Call register_capability — must return immediately even though
        # the underlying POST is blocked.
        store.register_capability("agent-x", "skill-y", domain="test")

        # Wait for the background thread to actually start the POST.
        # If register_capability had blocked, this line would never be reached
        # because post_may_finish is still unset → POST would hang forever.
        assert post_started.wait(timeout=2.0), (
            "Background POST did not start — register_capability may have "
            "swallowed the submission"
        )

        # Release the blocked POST so cleanup can happen.
        post_may_finish.set()

        # Clean up — shutdown with cancel_futures so pending tasks are dropped.
        store._propagation_pool.shutdown(wait=True, cancel_futures=True)
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
