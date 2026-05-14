#!/usr/bin/env python3
"""Test 1: Capability Registry Centralization (Storage layer)."""
import pytest
from pathlib import Path
from tempfile import TemporaryDirectory
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
        assert caps[0]["agent_id"] == "agent-a"
    
    def test_update_capability_replaces_row(self):
        """UPDATE works via INSERT OR REPLACE."""
        self.store.register_capability("agent-1", "skill-1")
        self.store.register_capability("agent-1", "skill-1", description="Updated desc")
        caps = self.store.get_capabilities(keyword="skill-1")
        assert len(caps) == 1
        assert caps[0]["description"] == "Updated desc"
    
    def test_query_by_keyword(self):
        """Filter by keyword in skill_id, description, domain."""
        self.store.register_capability("agent-1", "a2a-file-transfer")
        self.store.register_capability("agent-2", "video-summarization")
        
        caps = self.store.get_capabilities(keyword="file")
        assert len(caps) == 1
        assert caps[0]["skill_id"] == "a2a-file-transfer"
    
    def test_query_by_max_cost(self):
        """Filter by monetary_cost_usd ceiling."""
        self.store.register_capability("cheap", "skill", monetary_cost_usd=0.1)
        self.store.register_capability("expensive", "skill2", monetary_cost_usd=10.0)
        
        caps = self.store.get_capabilities(max_cost_usd=1.0)
        assert len(caps) == 1
        assert caps[0]["agent_id"] == "cheap"
    
    def test_unique_constraint_enforced(self):
        """Same agent+skill combination overwritten, not duplicated."""
        self.store.register_capability("agent", "skill")
        self.store.register_capability("agent", "skill")  # Should replace
        
        caps = self.store.get_capabilities()
        assert len(caps) == 1
