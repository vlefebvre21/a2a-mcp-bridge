"""Tests for Telegram wake-up integration into tool_agent_send + build_server."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from a2a_mcp_bridge import server as server_module
from a2a_mcp_bridge.signals import SignalDir
from a2a_mcp_bridge.store import Store
from a2a_mcp_bridge.tools import tool_agent_send
from a2a_mcp_bridge.wake import TelegramWaker, WakeEntry


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(str(tmp_path / "bus.sqlite"))
    s.init_schema()
    return s


@pytest.fixture
def signal_dir(tmp_path: Path) -> SignalDir:
    return SignalDir(str(tmp_path / "signals"))


class TestAgentSendCallsWaker:
    def test_waker_is_called_with_recipient_and_sender(
        self, store: Store, signal_dir: SignalDir
    ) -> None:
        store.upsert_agent("alice")
        store.upsert_agent("bob")

        fake_waker = MagicMock(spec=TelegramWaker)
        fake_waker.wake.return_value = True

        result = tool_agent_send(
            store,
            "alice",
            target="bob",
            message="hi",
            signal_dir=signal_dir,
            waker=fake_waker,
        )
        assert "error" not in result
        fake_waker.wake.assert_called_once_with("bob", sender_id="alice")

    def test_waker_not_called_on_error(self, store: Store, signal_dir: SignalDir) -> None:
        store.upsert_agent("alice")
        fake_waker = MagicMock(spec=TelegramWaker)

        result = tool_agent_send(
            store,
            "alice",
            target="ghost",
            message="x",
            signal_dir=signal_dir,
            waker=fake_waker,
        )
        assert "error" in result
        fake_waker.wake.assert_not_called()

    def test_waker_exception_does_not_break_send(self, store: Store, signal_dir: SignalDir) -> None:
        """Even if the waker raises (it shouldn't, but defensively), send succeeds."""
        store.upsert_agent("alice")
        store.upsert_agent("bob")
        fake_waker = MagicMock(spec=TelegramWaker)
        fake_waker.wake.side_effect = RuntimeError("boom")

        result = tool_agent_send(
            store,
            "alice",
            target="bob",
            message="hi",
            signal_dir=signal_dir,
            waker=fake_waker,
        )
        assert result["recipient"] == "bob"

    def test_waker_none_preserves_v02_behaviour(self, store: Store) -> None:
        """Backwards compat: calling without waker behaves like v0.2."""
        store.upsert_agent("alice")
        store.upsert_agent("bob")
        result = tool_agent_send(store, "alice", target="bob", message="hi")
        assert result["recipient"] == "bob"


class TestBuildServerLoadsRegistry:
    def test_build_server_without_registry_sets_waker_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If A2A_WAKE_REGISTRY is unset and default path does not exist, waker is None."""
        monkeypatch.delenv("A2A_WAKE_REGISTRY", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))  # ~ resolves here, no registry file
        db = tmp_path / "bus.sqlite"
        mcp = server_module.build_server(
            agent_id="alice",
            db_path=str(db),
            signal_dir_path=str(tmp_path / "signals"),
        )
        assert mcp is not None

    def test_build_server_loads_registry_when_env_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reg_path = tmp_path / "wake.json"
        reg_path.write_text(json.dumps({"alice": {"bot_token": "T:TOKEN", "chat_id": "111"}}))
        monkeypatch.setenv("A2A_WAKE_REGISTRY", str(reg_path))
        db = tmp_path / "bus.sqlite"

        # Patch the internal loader so we observe it was actually consulted
        with patch(
            "a2a_mcp_bridge.server.load_registry",
            wraps=__import__("a2a_mcp_bridge.wake", fromlist=["load_registry"]).load_registry,
        ) as spy:
            server_module.build_server(
                agent_id="alice",
                db_path=str(db),
                signal_dir_path=str(tmp_path / "signals"),
            )
            spy.assert_called_once_with(str(reg_path))

    def test_build_server_swallows_malformed_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A broken registry must log + continue, never crash the server boot."""
        reg_path = tmp_path / "wake.json"
        reg_path.write_text("not json")
        monkeypatch.setenv("A2A_WAKE_REGISTRY", str(reg_path))
        db = tmp_path / "bus.sqlite"
        # Should NOT raise
        mcp = server_module.build_server(
            agent_id="alice",
            db_path=str(db),
            signal_dir_path=str(tmp_path / "signals"),
        )
        assert mcp is not None


class TestWakerEntryShape:
    """Sanity: WakeEntry keys are what the CLI writes."""

    def test_cli_output_loads_back_into_waker(self, tmp_path: Path) -> None:
        from a2a_mcp_bridge.wake import load_registry

        reg = {"alice": {"bot_token": "T:TOKEN", "chat_id": "111"}}
        reg_path = tmp_path / "wake.json"
        reg_path.write_text(json.dumps(reg))

        loaded = load_registry(str(reg_path))
        waker = TelegramWaker(loaded)
        assert waker.has("alice")
        assert loaded["alice"] == WakeEntry(bot_token="T:TOKEN", chat_id="111")
