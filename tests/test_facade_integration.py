"""Integration test: real uvicorn subprocess + httpx client.

Verifies that the HTTP façade server and HttpBusStore client can
actually talk to each other — catching URL-prefix mismatches and
other wiring bugs that TestClient alone cannot detect.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import httpx
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FACADE_PORT = 18766  # high port to avoid collisions


@pytest.fixture(scope="module")
def facade_server():
    """Start a real uvicorn subprocess for the duration of the module."""

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "a2a_mcp_bridge.facade:create_app",
            "--host", "127.0.0.1",
            "--port", str(_FACADE_PORT),
            "--factory",
        ],
        env={**os.environ, "PYTHONPATH": "src"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Wait for the server to become ready (up to 8 s).
    base_url = f"http://127.0.0.1:{_FACADE_PORT}"
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{base_url}/health", timeout=1.0)
            if resp.status_code == 200:
                break
        except (httpx.ConnectError, httpx.ReadTimeout):
            pass
        time.sleep(0.15)
    else:
        proc.kill()
        _out, err = proc.communicate(timeout=5)
        pytest.fail(
            f"Façade server did not start on port {_FACADE_PORT}:\n"
            f"stderr: {err.decode()}"
        )

    yield base_url

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestIntegration:
    """End-to-end tests hitting a real uvicorn server with httpx."""

    def test_health(self, facade_server):
        resp = httpx.get(f"{facade_server}/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_register_and_send(self, facade_server):
        httpx.post(f"{facade_server}/register", json={"agent_id": "alice"})
        httpx.post(f"{facade_server}/register", json={"agent_id": "bob"})
        resp = httpx.post(
            f"{facade_server}/send",
            json={"sender": "alice", "recipient": "bob", "body": "real http!"},
        )
        assert resp.status_code == 200
        assert "message_id" in resp.json()

    def test_inbox_over_http(self, facade_server):
        resp = httpx.post(
            f"{facade_server}/inbox",
            json={"agent_id": "bob", "unread_only": True},
        )
        assert resp.status_code == 200
        messages = resp.json()["messages"]
        assert any(m["body"] == "real http!" for m in messages)

    def test_httpbusstore_client_compatibility(self, facade_server):
        """HttpBusStore must be able to talk to the façade.

        This is the integration test that catches C1-class bugs
        (URL prefix mismatches between client and server).
        """
        from a2a_mcp_bridge.bus_store import HttpBusStore

        client = HttpBusStore(base_url=facade_server, agent_id="carol")
        client.upsert_agent("carol")
        client.send_message("carol", "bob", "via HttpBusStore")
        messages = client.read_inbox("bob", unread_only=True)
        assert any(m.body == "via HttpBusStore" for m in messages)


# ---------------------------------------------------------------------------
# Event-loop blocking test (C2 proof)
# ---------------------------------------------------------------------------

_SIGNAL_DIR_PORT = 18767  # separate from _FACADE_PORT to keep fixtures independent


@pytest.fixture(scope="module")
def facade_server_with_signal_dir(tmp_path_factory):
    """A second uvicorn subprocess wired with a SignalDir.

    Uses the ``tests/_facade_with_signal_dir.py`` factory module to read the
    signal-directory path from ``A2A_FACADE_SIGNAL_DIR`` at startup. This
    gives ``/subscribe`` a real filesystem signal to block on so the C2
    event-loop test can observe the offload (or its absence).
    """
    sig_dir = tmp_path_factory.mktemp("fd_signal")

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "tests._facade_with_signal_dir:make_app",
            "--host", "127.0.0.1",
            "--port", str(_SIGNAL_DIR_PORT),
            "--factory",
        ],
        env={
            **os.environ,
            "PYTHONPATH": "src:.",  # so ``tests.*`` is importable
            "A2A_FACADE_SIGNAL_DIR": str(sig_dir),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    base_url = f"http://127.0.0.1:{_SIGNAL_DIR_PORT}"
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{base_url}/health", timeout=1.0)
            if resp.status_code == 200:
                break
        except (httpx.ConnectError, httpx.ReadTimeout):
            pass
        time.sleep(0.15)
    else:
        proc.kill()
        _out, err = proc.communicate(timeout=5)
        pytest.fail(
            f"Signal-dir façade did not start on port {_SIGNAL_DIR_PORT}:\n"
            f"stderr: {err.decode()}"
        )

    yield base_url

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


class TestSubscribeLoopBlocking:
    """Real-uvicorn test that catches C2-class bugs (blocking event loop)."""

    @pytest.mark.asyncio
    async def test_subscribe_does_not_block_inbox(
        self, facade_server_with_signal_dir
    ):
        """/subscribe must not block /inbox on the shared uvicorn event loop.

        Without ``anyio.to_thread.run_sync`` in the subscribe handler,
        ``store.subscribe()`` → ``signal_dir.wait()`` runs ``time.sleep``
        polling directly on the ASGI event loop. A concurrent ``/inbox``
        request then stalls until the subscribe times out. With the
        offload, the blocking ``wait()`` runs in a threadpool so the event
        loop stays responsive.
        """
        import asyncio

        base_url = facade_server_with_signal_dir

        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as ac:
            # Register both agents.
            for agent_id in ("alice", "bob"):
                r = await ac.post("/register", json={"agent_id": agent_id})
                assert r.status_code == 200

            # Fire a long subscribe for alice (no messages → blocks 2 s).
            sub_task = asyncio.create_task(
                ac.post(
                    "/subscribe",
                    json={"agent_id": "alice", "timeout_seconds": 2.0},
                )
            )
            # Give uvicorn a tick to pick up the subscribe request.
            await asyncio.sleep(0.3)

            # Now hit /inbox on the SAME uvicorn server (shared event loop).
            # If subscribe is blocking the loop, this stalls until the
            # 2 s subscribe timeout. With the offload, it returns instantly.
            t0 = asyncio.get_event_loop().time()
            inbox_resp = await ac.post(
                "/inbox",
                json={"agent_id": "bob", "unread_only": True},
            )
            elapsed = asyncio.get_event_loop().time() - t0

            assert inbox_resp.status_code == 200
            assert elapsed < 0.5, (
                f"/inbox waited {elapsed:.2f}s while /subscribe was blocking — "
                "event loop stalled. The anyio.to_thread offload in "
                "facade.py::subscribe is missing or broken."
            )

            # Clean up the subscribe so the fixture tears down cleanly.
            sub_resp = await sub_task
            assert sub_resp.status_code == 200
            assert sub_resp.json()["timed_out"] is True
