"""Tests for _load_waker_if_stale — reload waker on mtime change."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from a2a_mcp_bridge import server as server_module
from a2a_mcp_bridge.wake import WebhookWaker


def _write_registry(path: Path, agents: dict[str, str], secret: str = "secret64" * 8) -> None:
    """Write a valid v0.4.4 webhook registry to *path*."""
    path.write_text(
        json.dumps(
            {
                "wake_webhook_secret": secret,
                "agents": {
                    aid: {"wake_webhook_url": url} for aid, url in agents.items()
                },
            }
        )
    )


@pytest.fixture(autouse=True)
def _reset_cache():
    """Ensure the module-level cache is cleared before and after every test."""
    server_module._reset_waker_cache()
    yield
    server_module._reset_waker_cache()


@pytest.fixture
def reg_path(tmp_path: Path) -> Path:
    return tmp_path / "wake.json"


class TestLoadWakerIfStale:
    def test_first_call_loads_waker(self, reg_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """On the first call, the waker is loaded from disk."""
        monkeypatch.setenv("A2A_WAKE_REGISTRY", str(reg_path))
        _write_registry(reg_path, {"alice": "http://127.0.0.1:8700/webhooks/wake"})

        waker = server_module._load_waker_if_stale()
        assert waker is not None
        assert isinstance(waker, WebhookWaker)
        assert waker.has("alice")

    def test_cached_waker_returned_when_mtime_unchanged(
        self, reg_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the file mtime has not changed, the same waker object is returned."""
        monkeypatch.setenv("A2A_WAKE_REGISTRY", str(reg_path))
        _write_registry(reg_path, {"alice": "http://127.0.0.1:8700/webhooks/wake"})

        waker1 = server_module._load_waker_if_stale()
        waker2 = server_module._load_waker_if_stale()
        assert waker1 is waker2

    def test_waker_reloaded_on_mtime_change(
        self, reg_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the file mtime changes (new content), the waker is reloaded."""
        monkeypatch.setenv("A2A_WAKE_REGISTRY", str(reg_path))
        _write_registry(reg_path, {"alice": "http://127.0.0.1:8700/webhooks/wake"})

        waker1 = server_module._load_waker_if_stale()
        assert waker1 is not None
        assert waker1.has("alice")
        assert not waker1.has("bob")

        # Ensure a different mtime by writing new content and guaranteeing
        # the filesystem mtime ticks forward.
        time.sleep(0.05)
        _write_registry(
            reg_path,
            {
                "alice": "http://127.0.0.1:8700/webhooks/wake",
                "bob": "http://127.0.0.1:8701/webhooks/wake",
            },
        )
        # Force a different mtime on filesystems with low resolution.
        # On most Linux this is unnecessary but makes the test robust on
        # platforms with 1s mtime granularity.
        import os
        os.utime(str(reg_path))

        waker2 = server_module._load_waker_if_stale()
        assert waker2 is not None
        # Must be a new object (reloaded).
        assert waker2 is not waker1
        # And must contain the new agent.
        assert waker2.has("bob")
