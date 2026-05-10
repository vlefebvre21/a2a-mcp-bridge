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


@pytest.fixture(autouse=True)
def _allow_internal_webhooks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests use 127.0.0.1 / localhost in mock wake registries.

    Our SSRF hardening rejects those by default; enable the override so
    unit / integration tests that never open a real socket can still
    exercise the parser.
    """
    monkeypatch.setenv("A2A_ALLOW_INTERNAL_WEBHOOKS", "1")
