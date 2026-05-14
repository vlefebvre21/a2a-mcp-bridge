#!/usr/bin/env python3
"""Test 1: Capability Registry Centralization (Storage layer)."""
import pytest
from pathlib import Path, TemporaryDirectory
from a2a_mcp_bridge.store import Store

class TestCapabilityCentralization:
    @pytest.fixture(autouse=True)
    def clean_store(self, tmp_path):
        self.db = tmp_path / "test.sqlite"
        self.store = Store(self.db)
        yield
        self.store.close()
    
    def test_register_capability_creates_row(self):
        """Basic insert works."""
        self.store.register_capability("agent-a", "skill-x")
        caps = self.store.get_capabilities()
        assert len(caps) == 1
