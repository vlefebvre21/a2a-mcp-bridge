"""Pytest fixtures shared across the test suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from a2a_mcp_bridge.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    """Returns a fresh Store with an initialised schema in a temp SQLite file."""
    db_path = tmp_path / "test.sqlite"
    s = Store(str(db_path))
    s.init_schema()
    return s
