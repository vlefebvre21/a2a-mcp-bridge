"""Tests for the HTTP façade server (facade.py).

Uses FastAPI's TestClient backed by an in-memory SQLite store so no
external services are required.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from a2a_mcp_bridge.facade import create_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _register(client: TestClient, agent_id: str) -> None:
    resp = client.post("/register", json={"agent_id": agent_id})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client() -> TestClient:
    app = create_app(db_path=":memory:")
    return TestClient(app)


@pytest.fixture
def authed_client() -> TestClient:
    app = create_app(db_path=":memory:", api_key="test-secret")
    return TestClient(app)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_returns_ok(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "version" in data
        assert data["agents"] == 0

    def test_health_counts_registered_agents(self, client: TestClient) -> None:
        _register(client, "alice")
        _register(client, "bob")
        resp = client.get("/health")
        assert resp.json()["agents"] == 2

    def test_ping_returns_server_info(self, client: TestClient) -> None:
        resp = client.get("/ping")
        assert resp.status_code == 200
        data = resp.json()
        assert data["server"] == "a2a-mcp-bridge"
        assert "version" in data


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

class TestRegister:
    def test_register_new_agent(self, client: TestClient) -> None:
        resp = client.post("/register", json={"agent_id": "alice"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_register_idempotent(self, client: TestClient) -> None:
        client.post("/register", json={"agent_id": "alice"})
        resp = client.post("/register", json={"agent_id": "alice"})
        assert resp.status_code == 200

    def test_register_with_metadata(self, client: TestClient) -> None:
        resp = client.post(
            "/register",
            json={"agent_id": "alice", "metadata": {"role": "worker"}},
        )
        assert resp.status_code == 200

    def test_register_empty_agent_id(self, client: TestClient) -> None:
        resp = client.post("/register", json={"agent_id": ""})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

class TestSend:
    def test_send_message(self, client: TestClient) -> None:
        _register(client, "alice")
        _register(client, "bob")
        resp = client.post(
            "/send",
            json={"sender": "alice", "recipient": "bob", "body": "hello"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "message_id" in data
        assert data["recipient"] == "bob"

    def test_send_with_intent(self, client: TestClient) -> None:
        _register(client, "alice")
        _register(client, "bob")
        resp = client.post(
            "/send",
            json={
                "sender": "alice",
                "recipient": "bob",
                "body": "do it",
                "intent": "execute",
            },
        )
        assert resp.status_code == 200

    def test_send_rejects_self(self, client: TestClient) -> None:
        _register(client, "alice")
        resp = client.post(
            "/send",
            json={"sender": "alice", "recipient": "alice", "body": "loop"},
        )
        assert resp.status_code == 400
        assert "TARGET_SELF" in resp.json()["error"]["message"]

    def test_send_rejects_unknown_target(self, client: TestClient) -> None:
        _register(client, "alice")
        resp = client.post(
            "/send",
            json={"sender": "alice", "recipient": "unknown", "body": "hi"},
        )
        assert resp.status_code == 400
        assert "TARGET_UNKNOWN" in resp.json()["error"]["message"]

    def test_send_missing_fields(self, client: TestClient) -> None:
        resp = client.post("/send", json={"sender": "alice"})
        # Pydantic validation → 400 with VALIDATION_ERROR
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------

class TestInbox:
    def test_inbox_returns_unread(self, client: TestClient) -> None:
        _register(client, "alice")
        _register(client, "bob")
        client.post(
            "/send",
            json={"sender": "alice", "recipient": "bob", "body": "hi bob"},
        )
        resp = client.post(
            "/inbox",
            json={"agent_id": "bob", "unread_only": True},
        )
        assert resp.status_code == 200
        messages = resp.json()["messages"]
        assert len(messages) == 1
        assert messages[0]["body"] == "hi bob"

    def test_inbox_marks_read(self, client: TestClient) -> None:
        _register(client, "alice")
        _register(client, "bob")
        client.post(
            "/send",
            json={"sender": "alice", "recipient": "bob", "body": "hi"},
        )
        client.post("/inbox", json={"agent_id": "bob"})
        resp = client.post(
            "/inbox",
            json={"agent_id": "bob", "unread_only": True},
        )
        assert resp.json()["messages"] == []

    def test_inbox_respects_limit(self, client: TestClient) -> None:
        _register(client, "alice")
        _register(client, "bob")
        for i in range(5):
            client.post(
                "/send",
                json={"sender": "alice", "recipient": "bob", "body": f"msg-{i}"},
            )
        resp = client.post(
            "/inbox",
            json={"agent_id": "bob", "limit": 2},
        )
        assert len(resp.json()["messages"]) == 2

    def test_inbox_empty_agent_id(self, client: TestClient) -> None:
        resp = client.post("/inbox", json={"agent_id": ""})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Inbox peek
# ---------------------------------------------------------------------------

class TestInboxPeek:
    def test_peek_does_not_mark_read(self, client: TestClient) -> None:
        _register(client, "alice")
        _register(client, "bob")
        client.post(
            "/send",
            json={"sender": "alice", "recipient": "bob", "body": "peek"},
        )
        resp = client.post(
            "/inbox_peek",
            json={"agent_id": "bob"},
        )
        assert len(resp.json()["messages"]) == 1
        resp2 = client.post(
            "/inbox",
            json={"agent_id": "bob", "unread_only": True},
        )
        assert len(resp2.json()["messages"]) == 1

    def test_peek_with_since_ts(self, client: TestClient) -> None:
        _register(client, "alice")
        _register(client, "bob")
        client.post(
            "/send",
            json={"sender": "alice", "recipient": "bob", "body": "old"},
        )
        resp = client.post(
            "/inbox_peek",
            json={"agent_id": "bob", "since_ts": "2099-01-01T00:00:00Z"},
        )
        assert resp.json()["messages"] == []

    def test_peek_empty_agent_id(self, client: TestClient) -> None:
        resp = client.post("/inbox_peek", json={"agent_id": ""})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# List agents
# ---------------------------------------------------------------------------

class TestListAgents:
    def test_list_returns_registered(self, client: TestClient) -> None:
        _register(client, "alice")
        resp = client.post("/list", json={})
        agents = resp.json()["agents"]
        assert any(a["agent_id"] == "alice" for a in agents)


# ---------------------------------------------------------------------------
# Subscribe
# ---------------------------------------------------------------------------

class TestSubscribe:
    def test_subscribe_fast_path_without_signal_dir(self, client: TestClient) -> None:
        """When messages are already pending, subscribe returns them
        immediately — even without a SignalDir (fast-path)."""
        _register(client, "alice")
        _register(client, "bob")
        client.post(
            "/send",
            json={"sender": "alice", "recipient": "bob", "body": "sub-test"},
        )
        resp = client.post(
            "/subscribe",
            json={"agent_id": "bob", "timeout_seconds": 0.1},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["timed_out"] is False
        assert len(data["messages"]) == 1
        assert data["messages"][0]["body"] == "sub-test"

    def test_subscribe_no_messages_no_signal_dir(self, client: TestClient) -> None:
        """When no messages are pending and no SignalDir is configured,
        subscribe returns CONFIG_ERROR 500."""
        _register(client, "alice")
        resp = client.post(
            "/subscribe",
            json={"agent_id": "alice", "timeout_seconds": 0.1},
        )
        assert resp.status_code == 500
        assert resp.json()["error"]["code"] == "CONFIG_ERROR"

    def test_subscribe_empty_agent_id(self, client: TestClient) -> None:
        resp = client.post("/subscribe", json={"agent_id": ""})
        assert resp.status_code == 400

    def test_concurrent_subscribers_both_receive(self, client: TestClient) -> None:
        """Two concurrent subscribe calls must both complete without
        blocking each other — proves the anyio.to_thread.run_sync
        offload works (C2 proof).

        Strategy: start a long subscribe (2 s) for agent A, then
        immediately check inbox for agent B. If subscribe blocks the
        ASGI event loop, the inbox call will stall until subscribe
        returns. With the offload, it completes instantly.
        """
        import threading
        import time

        _register(client, "alice")
        _register(client, "bob")

        # Long subscribe for alice (no messages → will block 2 s)
        def do_subscribe() -> None:
            client.post(
                "/subscribe",
                json={"agent_id": "alice", "timeout_seconds": 2.0},
            )

        sub_thread = threading.Thread(target=do_subscribe)
        sub_thread.start()

        # Give the subscribe a moment to start blocking
        time.sleep(0.2)

        # While subscribe is blocking, inbox for bob should be instant
        t0 = time.monotonic()
        resp = client.post("/inbox", json={"agent_id": "bob", "unread_only": True})
        elapsed = time.monotonic() - t0
        assert resp.status_code == 200

        # If the event loop was blocked by subscribe, this would take ~2 s.
        # With anyio.to_thread.run_sync, it should be < 0.5 s.
        assert elapsed < 0.5, f"Inbox call took {elapsed:.2f}s — event loop was blocked?"

        sub_thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class TestAuth:
    def test_no_auth_required_when_no_key(self, client: TestClient) -> None:
        _register(client, "alice")
        resp = client.post("/inbox", json={"agent_id": "alice"})
        assert resp.status_code == 200

    def test_auth_rejects_missing_key(self, authed_client: TestClient) -> None:
        resp = authed_client.post("/register", json={"agent_id": "alice"})
        assert resp.status_code == 401

    def test_auth_rejects_wrong_key(self, authed_client: TestClient) -> None:
        resp = authed_client.post(
            "/register",
            json={"agent_id": "alice"},
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert resp.status_code == 401

    def test_auth_accepts_correct_key(self, authed_client: TestClient) -> None:
        authed_client.post(
            "/register",
            json={"agent_id": "alice"},
            headers={"Authorization": "Bearer test-secret"},
        )
        resp = authed_client.post(
            "/send",
            json={"sender": "alice", "recipient": "alice", "body": "self"},
            headers={"Authorization": "Bearer test-secret"},
        )
        assert resp.status_code == 400  # TARGET_SELF, but auth passed

    def test_health_never_requires_auth(self, authed_client: TestClient) -> None:
        resp = authed_client.get("/health")
        assert resp.status_code == 200
