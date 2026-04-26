"""Tests for the HTTP façade server (facade.py).

Uses FastAPI's TestClient backed by an in-memory SQLite store so no
external services are required.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from a2a_mcp_bridge.facade import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    """TestClient wired to a fresh in-memory app (no auth)."""
    app = create_app(db_path=":memory:", api_key=None)
    return TestClient(app)


@pytest.fixture
def authed_client() -> TestClient:
    """TestClient with API-key auth enabled."""
    app = create_app(db_path=":memory:", api_key="secret123")
    return TestClient(app)


def _register(client: TestClient, agent_id: str) -> None:
    """Helper: register an agent via the /bus/register endpoint."""
    resp = client.post("/bus/register", json={"agent_id": agent_id})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class TestHealth:
    def test_health_returns_ok(self, client: TestClient) -> None:
        resp = client.get("/bus/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "version" in data
        assert data["agents"] == 0

    def test_health_counts_registered_agents(self, client: TestClient) -> None:
        _register(client, "alice")
        resp = client.get("/bus/health")
        assert resp.json()["agents"] == 1


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------


class TestRegister:
    def test_register_new_agent(self, client: TestClient) -> None:
        resp = client.post("/bus/register", json={"agent_id": "alice"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_register_idempotent(self, client: TestClient) -> None:
        client.post("/bus/register", json={"agent_id": "alice"})
        resp = client.post("/bus/register", json={"agent_id": "alice"})
        assert resp.status_code == 200

    def test_register_with_metadata(self, client: TestClient) -> None:
        resp = client.post(
            "/bus/register",
            json={"agent_id": "bob", "metadata": {"role": "worker"}},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


class TestSend:
    def test_send_message(self, client: TestClient) -> None:
        _register(client, "alice")
        _register(client, "bob")
        resp = client.post(
            "/bus/send",
            json={"sender": "alice", "recipient": "bob", "body": "hello"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "message_id" in data
        assert data["recipient"] == "bob"
        assert "sent_at" in data

    def test_send_with_intent(self, client: TestClient) -> None:
        _register(client, "alice")
        _register(client, "bob")
        resp = client.post(
            "/bus/send",
            json={
                "sender": "alice",
                "recipient": "bob",
                "body": "fyi msg",
                "intent": "fyi",
            },
        )
        assert resp.status_code == 200

    def test_send_rejects_self(self, client: TestClient) -> None:
        _register(client, "alice")
        resp = client.post(
            "/bus/send",
            json={"sender": "alice", "recipient": "alice", "body": "hi"},
        )
        assert resp.status_code == 400
        assert "TARGET_SELF" in resp.json()["error"]["message"]

    def test_send_rejects_unknown_target(self, client: TestClient) -> None:
        _register(client, "alice")
        resp = client.post(
            "/bus/send",
            json={"sender": "alice", "recipient": "nobody", "body": "hi"},
        )
        assert resp.status_code == 400
        assert "TARGET_UNKNOWN" in resp.json()["error"]["message"]

    def test_send_missing_fields(self, client: TestClient) -> None:
        resp = client.post("/bus/send", json={"sender": "alice"})
        # Missing required field → 400 with VALIDATION_ERROR
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
            "/bus/send",
            json={"sender": "alice", "recipient": "bob", "body": "hello"},
        )
        resp = client.post(
            "/bus/inbox", json={"agent_id": "bob", "unread_only": True}
        )
        assert resp.status_code == 200
        msgs = resp.json()["messages"]
        assert len(msgs) == 1
        assert msgs[0]["body"] == "hello"
        assert msgs[0]["sender"] == "alice"
        assert msgs[0]["intent"] == "triage"  # default intent

    def test_inbox_marks_read(self, client: TestClient) -> None:
        _register(client, "alice")
        _register(client, "bob")
        client.post(
            "/bus/send",
            json={"sender": "alice", "recipient": "bob", "body": "hi"},
        )
        # First read consumes
        client.post("/bus/inbox", json={"agent_id": "bob"})
        # Second read: empty
        resp = client.post(
            "/bus/inbox", json={"agent_id": "bob", "unread_only": True}
        )
        assert resp.json()["messages"] == []

    def test_inbox_respects_limit(self, client: TestClient) -> None:
        _register(client, "alice")
        _register(client, "bob")
        for i in range(5):
            client.post(
                "/bus/send",
                json={"sender": "alice", "recipient": "bob", "body": f"msg{i}"},
            )
        resp = client.post(
            "/bus/inbox", json={"agent_id": "bob", "limit": 2, "unread_only": True}
        )
        assert len(resp.json()["messages"]) == 2


# ---------------------------------------------------------------------------
# Inbox peek
# ---------------------------------------------------------------------------


class TestInboxPeek:
    def test_peek_does_not_mark_read(self, client: TestClient) -> None:
        _register(client, "alice")
        _register(client, "bob")
        client.post(
            "/bus/send",
            json={"sender": "alice", "recipient": "bob", "body": "peek-test"},
        )
        # Peek should NOT consume
        resp = client.post(
            "/bus/inbox_peek", json={"agent_id": "bob", "limit": 10}
        )
        assert resp.status_code == 200
        msgs = resp.json()["messages"]
        assert len(msgs) == 1
        # Message should still be unread (read_at is null)
        assert msgs[0]["read_at"] is None

        # Verify still available via inbox (unread)
        resp2 = client.post(
            "/bus/inbox", json={"agent_id": "bob", "unread_only": True}
        )
        assert len(resp2.json()["messages"]) == 1

    def test_peek_with_since_ts(self, client: TestClient) -> None:
        _register(client, "alice")
        _register(client, "bob")
        client.post(
            "/bus/send",
            json={"sender": "alice", "recipient": "bob", "body": "old"},
        )
        # Use a future timestamp → no messages
        resp = client.post(
            "/bus/inbox_peek",
            json={"agent_id": "bob", "since_ts": "2099-01-01T00:00:00+00:00"},
        )
        assert len(resp.json()["messages"]) == 0


# ---------------------------------------------------------------------------
# List agents
# ---------------------------------------------------------------------------


class TestListAgents:
    def test_list_returns_registered(self, client: TestClient) -> None:
        _register(client, "alice")
        _register(client, "bob")
        resp = client.post("/bus/list", json={})
        assert resp.status_code == 200
        agents = resp.json()["agents"]
        ids = [a["agent_id"] for a in agents]
        assert "alice" in ids
        assert "bob" in ids

    def test_list_respects_window(self, client: TestClient) -> None:
        _register(client, "alice")
        resp = client.post(
            "/bus/list", json={"active_within_days": 7}
        )
        assert len(resp.json()["agents"]) >= 1


# ---------------------------------------------------------------------------
# Subscribe
# ---------------------------------------------------------------------------


class TestSubscribe:
    def test_subscribe_timeout(self, client: TestClient) -> None:
        """Subscribe with no SignalDir returns CONFIG_ERROR 500."""
        _register(client, "alice")
        resp = client.post(
            "/bus/subscribe",
            json={"agent_id": "alice", "timeout_seconds": 0.1},
        )
        assert resp.status_code == 500
        assert resp.json()["error"]["code"] == "CONFIG_ERROR"

    def test_subscribe_delivers_message(self, client: TestClient) -> None:
        """Subscribe without SignalDir returns CONFIG_ERROR 500
        even when messages are pending."""
        _register(client, "alice")
        _register(client, "bob")
        client.post(
            "/bus/send",
            json={"sender": "alice", "recipient": "bob", "body": "sub-test"},
        )
        resp = client.post(
            "/bus/subscribe",
            json={"agent_id": "bob", "timeout_seconds": 0.1},
        )
        assert resp.status_code == 500
        assert resp.json()["error"]["code"] == "CONFIG_ERROR"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestAuth:
    def test_no_auth_required_when_no_key(self, client: TestClient) -> None:
        _register(client, "alice")
        # No X-Api-Key header → should succeed (dev mode)
        resp = client.post(
            "/bus/inbox", json={"agent_id": "alice"}
        )
        assert resp.status_code == 200

    def test_auth_rejects_missing_key(self, authed_client: TestClient) -> None:
        resp = authed_client.post(
            "/bus/inbox", json={"agent_id": "alice"}
        )
        assert resp.status_code == 401

    def test_auth_rejects_wrong_key(self, authed_client: TestClient) -> None:
        resp = authed_client.post(
            "/bus/inbox",
            json={"agent_id": "alice"},
            headers={"X-Api-Key": "wrong"},
        )
        assert resp.status_code == 401

    def test_auth_accepts_correct_key(self, authed_client: TestClient) -> None:
        authed_client.post(
            "/bus/register",
            json={"agent_id": "alice"},
            headers={"X-Api-Key": "secret123"},
        )
        resp = authed_client.post(
            "/bus/inbox",
            json={"agent_id": "alice"},
            headers={"X-Api-Key": "secret123"},
        )
        assert resp.status_code == 200

    def test_health_never_requires_auth(self, authed_client: TestClient) -> None:
        resp = authed_client.get("/bus/health")
        assert resp.status_code == 200
