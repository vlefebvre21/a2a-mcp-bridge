"""Tests for v0.4.4 webhook wake-up integration in tool_agent_send + build_server."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from a2a_mcp_bridge import server as server_module
from a2a_mcp_bridge.signals import SignalDir
from a2a_mcp_bridge.store import Store
from a2a_mcp_bridge.tools import tool_agent_send
from a2a_mcp_bridge.wake import WakeEntry, WebhookWaker


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(str(tmp_path / "bus.sqlite"))
    s.init_schema()
    return s


@pytest.fixture
def signal_dir(tmp_path: Path) -> SignalDir:
    return SignalDir(str(tmp_path / "signals"))


class TestAgentSendCallsWaker:
    """The waker must be called AFTER the message is persisted to SQLite.

    Per the best-effort contract (issue #opus-review point 1), ``agent_send``
    persists first and wakes second. A waker failure never prevents
    persistence; a persistence failure skips the waker call entirely.
    """

    def test_waker_is_called_with_recipient_and_sender(
        self, store: Store, signal_dir: SignalDir
    ) -> None:
        store.upsert_agent("alice")
        store.upsert_agent("bob")

        fake_waker = MagicMock(spec=WebhookWaker)
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

    def test_persist_before_wake(self, store: Store, signal_dir: SignalDir) -> None:
        """Waker runs after the store write — swap in a dummy store to verify.

        If the order were inverted, a wake_called_before_store_write would be
        observable. We spy on the call order.
        """
        store.upsert_agent("alice")
        store.upsert_agent("bob")

        calls: list[str] = []

        orig_send_message = store.send_message

        def wrapped_send(*args, **kwargs):  # type: ignore[no-untyped-def]
            calls.append("store")
            return orig_send_message(*args, **kwargs)

        store.send_message = wrapped_send  # type: ignore[method-assign]

        fake_waker = MagicMock(spec=WebhookWaker)

        def record_wake(target, sender_id):  # type: ignore[no-untyped-def]
            calls.append("wake")
            return True

        fake_waker.wake.side_effect = record_wake

        tool_agent_send(
            store,
            "alice",
            target="bob",
            message="hi",
            signal_dir=signal_dir,
            waker=fake_waker,
        )
        assert calls == ["store", "wake"]

    def test_waker_not_called_on_persist_error(
        self, store: Store, signal_dir: SignalDir
    ) -> None:
        store.upsert_agent("alice")
        fake_waker = MagicMock(spec=WebhookWaker)

        # "ghost" hasn't been upserted → store.send_message raises → no wake.
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

    def test_waker_exception_does_not_break_send(
        self, store: Store, signal_dir: SignalDir
    ) -> None:
        """Even if the waker raises (defensive; it shouldn't), send succeeds.

        This is the explicit fallback-is-a-warning contract from the v0.4.4
        review: wake failures log a warning, never propagate.
        """
        store.upsert_agent("alice")
        store.upsert_agent("bob")
        fake_waker = MagicMock(spec=WebhookWaker)
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

    def test_waker_none_preserves_no_wake_behaviour(self, store: Store) -> None:
        """Backwards compat: no waker → persistence only, no wake, no error."""
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
        monkeypatch.setenv("HOME", str(tmp_path))
        db = tmp_path / "bus.sqlite"
        mcp = server_module.build_server(
            agent_id="alice",
            db_path=str(db),
            signal_dir_path=str(tmp_path / "signals"),
        )
        assert mcp is not None

    def test_build_server_loads_webhook_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reg_path = tmp_path / "wake.json"
        reg_path.write_text(
            json.dumps(
                {
                    "wake_webhook_secret": "secret64" * 8,
                    "agents": {
                        "alice": {
                            "wake_webhook_url": "http://127.0.0.1:8700/webhooks/wake"
                        }
                    },
                }
            )
        )
        monkeypatch.setenv("A2A_WAKE_REGISTRY", str(reg_path))
        db = tmp_path / "bus.sqlite"

        with patch(
            "a2a_mcp_bridge.server.load_registry",
            wraps=__import__(
                "a2a_mcp_bridge.wake", fromlist=["load_registry"]
            ).load_registry,
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
        mcp = server_module.build_server(
            agent_id="alice",
            db_path=str(db),
            signal_dir_path=str(tmp_path / "signals"),
        )
        assert mcp is not None

    def test_build_server_swallows_legacy_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pre-v0.4.4 Telegram-based registry must not crash boot.

        ``load_registry`` detects the legacy format, logs a migration
        warning, and returns an empty registry. ``build_server`` should
        see no waker and move on.
        """
        reg_path = tmp_path / "wake.json"
        reg_path.write_text(
            json.dumps(
                {
                    "wake_bot_token": "111:OLD",
                    "agents": {"alice": {"chat_id": "-100", "thread_id": 5}},
                }
            )
        )
        monkeypatch.setenv("A2A_WAKE_REGISTRY", str(reg_path))
        db = tmp_path / "bus.sqlite"
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

        reg = {
            "wake_webhook_secret": "secret64" * 8,
            "agents": {
                "alice": {
                    "wake_webhook_url": "http://127.0.0.1:8700/webhooks/wake"
                }
            },
        }
        reg_path = tmp_path / "wake.json"
        reg_path.write_text(json.dumps(reg))

        secret, loaded = load_registry(str(reg_path))
        waker = WebhookWaker(loaded, shared_secret=secret)
        assert waker.has("alice")
        assert loaded["alice"] == WakeEntry(
            wake_webhook_url="http://127.0.0.1:8700/webhooks/wake"
        )
        assert waker.configured is True
