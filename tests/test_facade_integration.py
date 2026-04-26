"""Integration test: real uvicorn subprocess + httpx client.

Verifies that the HTTP façade server and HttpBusStore client can
actually talk to each other — catching URL-prefix mismatches and
other wiring bugs that TestClient alone cannot detect.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
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
    tmpdir = tempfile.mkdtemp()
    os.path.join(tmpdir, "facade_test.db")

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
