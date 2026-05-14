#!/usr/bin/env python3
"""Test 4: Concurrency and Thread Safety."""
import pytest, threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from a2a_mcp_bridge.store import Store

class TestCapabilityConcurrency:
    @pytest.fixture(autouse=True)
    def clean_store(self, tmp_path):
        self.db = tmp_path / "concurrent.sqlite"
        yield
    
    def test_concurrent_capability_registration(self):
        """Multiple threads can register capabilities simultaneously."""
        from a2a_mcp_bridge.store import Store
        store = Store(self.db, check_same_thread=False)
        errors = []
        
        def register_one(i):
            try:
                store.register_capability(f"agent-{i}", f"skill-{i % 3}")
            except Exception as e:
                errors.append(e)
        
        with ThreadPoolExecutor(max_workers=5) as ex:
            list(ex.map(register_one, range(10)))
        
        store.close()
        assert len(errors) == 0, f"Concurrent registration failed: {errors}"
        
    def test_concurrent_capability_query(self):
        """Multiple threads can query capabilities simultaneously."""
        from a2a_mcp_bridge.store import Store
        store = Store(self.db, check_same_thread=False)
        
        # First register some data
        for i in range(10):
            store.register_capability(f"agent-{i}", "skill-read")
        
        errors = []
        def query_one():
            try:
                caps = store.get_capabilities()
                assert len(caps) == 10
            except Exception as e:
                errors.append(e)
        
        with ThreadPoolExecutor(max_workers=5) as ex:
            list(ex.map(lambda x: query_one(), range(10)))
        
        store.close()
        assert len(errors) == 0, f"Concurrent query failed: {errors}"
