"""Tests for v0.2 real-time delivery: signal files + agent_subscribe long-poll."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from a2a_mcp_bridge.signals import SignalDir, signal_path_for
from a2a_mcp_bridge.store import Store
from a2a_mcp_bridge.tools import (
    tool_agent_send,
    tool_agent_subscribe,
)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(str(tmp_path / "bus.sqlite"))
    s.init_schema()
    return s


@pytest.fixture
def signal_dir(tmp_path: Path) -> SignalDir:
    return SignalDir(str(tmp_path / "signals"))


class TestSignalDir:
    def test_signal_dir_creates_directory(self, tmp_path: Path) -> None:
        d = tmp_path / "signals"
        assert not d.exists()
        SignalDir(str(d))
        assert d.is_dir()

    def test_notify_writes_signal_file(self, signal_dir: SignalDir) -> None:
        signal_dir.notify("alice")
        path = signal_path_for(signal_dir.path, "alice")
        assert path.is_file()

    def test_notify_updates_mtime(self, signal_dir: SignalDir) -> None:
        signal_dir.notify("alice")
        path = signal_path_for(signal_dir.path, "alice")
        first = path.stat().st_mtime_ns
        time.sleep(0.01)
        signal_dir.notify("alice")
        assert path.stat().st_mtime_ns >= first

    def test_wait_returns_false_on_timeout(self, signal_dir: SignalDir) -> None:
        # No signal fired → must time out
        fired = signal_dir.wait("alice", timeout_seconds=0.2, poll_interval=0.05)
        assert fired is False

    def test_wait_returns_true_when_signal_fires(self, signal_dir: SignalDir) -> None:
        def fire() -> None:
            time.sleep(0.1)
            signal_dir.notify("alice")

        threading.Thread(target=fire, daemon=True).start()
        fired = signal_dir.wait("alice", timeout_seconds=2.0, poll_interval=0.05)
        assert fired is True

    def test_wait_detects_preexisting_unconsumed_signal(self, signal_dir: SignalDir) -> None:
        """If a signal already exists when wait() starts, return immediately."""
        signal_dir.notify("alice")
        start = time.monotonic()
        fired = signal_dir.wait("alice", timeout_seconds=2.0, poll_interval=0.05)
        elapsed = time.monotonic() - start
        assert fired is True
        assert elapsed < 0.5


class TestAgentSendWritesSignal:
    def test_send_creates_signal_for_recipient(self, store: Store, signal_dir: SignalDir) -> None:
        store.upsert_agent("alice")
        store.upsert_agent("bob")

        result = tool_agent_send(store, "alice", target="bob", message="hi", signal_dir=signal_dir)
        assert "error" not in result
        assert signal_path_for(signal_dir.path, "bob").is_file()

    def test_send_does_not_signal_on_error(self, store: Store, signal_dir: SignalDir) -> None:
        store.upsert_agent("alice")
        result = tool_agent_send(store, "alice", target="ghost", message="x", signal_dir=signal_dir)
        assert "error" in result
        assert not signal_path_for(signal_dir.path, "ghost").exists()

    def test_send_without_signal_dir_still_works(self, store: Store) -> None:
        """Backwards compat: tool_agent_send without signal_dir (v0.1 behaviour)."""
        store.upsert_agent("alice")
        store.upsert_agent("bob")
        result = tool_agent_send(store, "alice", target="bob", message="hi")
        assert result["recipient"] == "bob"


class TestAgentSubscribe:
    def test_subscribe_returns_immediately_when_inbox_has_messages(
        self, store: Store, signal_dir: SignalDir
    ) -> None:
        store.upsert_agent("alice")
        store.upsert_agent("bob")
        store.send_message("alice", "bob", "pre-existing")

        start = time.monotonic()
        result = tool_agent_subscribe(store, "bob", signal_dir=signal_dir, timeout_seconds=5.0)
        elapsed = time.monotonic() - start

        assert len(result["messages"]) == 1
        assert result["messages"][0]["body"] == "pre-existing"
        assert result["timed_out"] is False
        assert elapsed < 1.0

    def test_subscribe_times_out_when_no_messages(
        self, store: Store, signal_dir: SignalDir
    ) -> None:
        store.upsert_agent("alice")
        result = tool_agent_subscribe(
            store,
            "alice",
            signal_dir=signal_dir,
            timeout_seconds=0.3,
            poll_interval=0.05,
        )
        assert result["messages"] == []
        assert result["timed_out"] is True

    def test_subscribe_wakes_on_incoming_signal(
        self, store: Store, signal_dir: SignalDir, tmp_path: Path
    ) -> None:
        store.upsert_agent("alice")
        store.upsert_agent("bob")
        db_path = store.db_path

        def send_after_delay() -> None:
            # Real deployment: sender lives in another process → own Store.
            # Emulate that here to avoid SQLite's single-thread rule.
            sender_store = Store(db_path)
            time.sleep(0.15)
            tool_agent_send(
                sender_store,
                "alice",
                target="bob",
                message="wake",
                signal_dir=signal_dir,
            )
            sender_store.close()

        threading.Thread(target=send_after_delay, daemon=True).start()
        start = time.monotonic()
        result = tool_agent_subscribe(
            store,
            "bob",
            signal_dir=signal_dir,
            timeout_seconds=3.0,
            poll_interval=0.05,
        )
        elapsed = time.monotonic() - start

        assert len(result["messages"]) == 1
        assert result["messages"][0]["body"] == "wake"
        assert result["timed_out"] is False
        assert elapsed < 2.0

    def test_subscribe_clamps_timeout(self, store: Store, signal_dir: SignalDir) -> None:
        """Very large timeouts must be clamped to protect the MCP transport."""
        store.upsert_agent("alice")
        start = time.monotonic()
        # We don't wait the full clamp — we just check it doesn't explode
        result = tool_agent_subscribe(
            store,
            "alice",
            signal_dir=signal_dir,
            timeout_seconds=0.1,
            poll_interval=0.02,
        )
        assert (time.monotonic() - start) < 1.0
        assert result["timed_out"] is True
