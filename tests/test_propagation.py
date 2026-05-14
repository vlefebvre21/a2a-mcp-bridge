#!/usr/bin/env python3
"""Test 3: HttpBusStore Cross-Host Propagation."""
import pytest
from a2a_mcp_bridge.http_bus_store import AsyncHTTPBusClient, CapabilityAnnouncement

class TestHttpPropagator:
    def test_no_remote_url_skips_propagation(self):
        """Client with no remote URL does nothing."""
        client = AsyncHTTPBusClient(remote_url=None)
        # Should not raise
        assert client.remote_url is None
    
    def test_propagation_is_fire_and_forget(self):
        """Propagation doesn't block on remote errors."""
        # TODO: Mock aiohttp.ClientSession to test fire-and-forget semantics
        pass  # Placeholder for integration test
