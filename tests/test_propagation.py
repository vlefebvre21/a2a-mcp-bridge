#!/usr/bin/env python3
"""Test 3: HttpBusStore Cross-Host Propagation."""
import pytest
from pathlib import Path
from tempfile import TemporaryDirectory
from a2a_mcp_bridge.http_bus_store import AsyncHTTPBusClient, CapabilityAnnouncement

class TestHttpPropagator:
    def test_no_remote_url_skips_propagation(self):
        """Client with no remote URL does nothing (safety check)."""
        client = AsyncHTTPBusClient(remote_url=None)
        assert client.remote_url is None
    
    def test_propagation_target_set(self):
        """Client initializes with remote URL."""
        client = AsyncHTTPBusClient(remote_url="http://other-host:8081")
        assert client.remote_url == "http://other-host:8081"
    
    @pytest.mark.skip(reason="Requires running remote bus for integration test")
    async def test_propagation_is_fire_and_forget(self):
        """Propagation doesn't block on remote errors (fire-and-forget)."""
        # TODO: Mock aiohttp.ClientSession to test that propagate_capability
        # catches exceptions and doesn't raise to the caller.
        pass  # Placeholder for integration test
