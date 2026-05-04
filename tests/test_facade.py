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

    # Note: the real "event-loop is not blocked by /subscribe" test lives in
    # tests/test_facade_integration.py::TestSubscribeLoopBlocking. FastAPI
    # TestClient and httpx.ASGITransport both execute each request in their
    # own short-lived event loop, so they cannot detect the C2 bug class
    # (blocking the uvicorn event loop). Only a real uvicorn subprocess with
    # concurrent async httpx requests exercises the right topology.


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


# ---------------------------------------------------------------------------
# Send intent wake policy (ADR-002)
# ---------------------------------------------------------------------------

class TestSendIntentWakePolicy:
    """Verify that send_handler respects ADR-002 wake policy.

    intent=fyi must NOT trigger waker.wake(); all other intents must.
    Mirrors the same checks in test_tools.py for the MCP tool path.
    """

    @pytest.fixture()
    def waker_client(self) -> TestClient:
        """TestClient with a recording WebhookWaker mock."""
        from unittest.mock import MagicMock

        from a2a_mcp_bridge.wake import WakeEntry

        registry = {"bob": WakeEntry(wake_webhook_url="http://localhost:9999/wake")}
        mock_waker = MagicMock()
        mock_waker._registry = registry
        mock_waker._shared_secret = "test-secret"
        # Make has() work correctly
        mock_waker.has = lambda agent_id: agent_id in registry
        mock_waker.wake.return_value = True

        app = create_app(db_path=":memory:", waker=mock_waker)
        client = TestClient(app)
        # Stash the mock so tests can inspect .wake.call_count
        client._waker_mock = mock_waker  # type: ignore[attr-defined]
        return client

    def test_fyi_intent_does_not_wake(self, waker_client: TestClient) -> None:
        _register(waker_client, "alice")
        _register(waker_client, "bob")
        resp = waker_client.post(
            "/send",
            json={"sender": "alice", "recipient": "bob", "body": "fyi", "intent": "fyi"},
        )
        assert resp.status_code == 200
        assert waker_client._waker_mock.wake.call_count == 0

    def test_execute_intent_does_wake(self, waker_client: TestClient) -> None:
        _register(waker_client, "alice")
        _register(waker_client, "bob")
        resp = waker_client.post(
            "/send",
            json={"sender": "alice", "recipient": "bob", "body": "do it", "intent": "execute"},
        )
        assert resp.status_code == 200
        assert waker_client._waker_mock.wake.call_count == 1

    def test_triage_intent_does_wake(self, waker_client: TestClient) -> None:
        _register(waker_client, "alice")
        _register(waker_client, "bob")
        resp = waker_client.post(
            "/send",
            json={"sender": "alice", "recipient": "bob", "body": "triage", "intent": "triage"},
        )
        assert resp.status_code == 200
        assert waker_client._waker_mock.wake.call_count == 1

    def test_default_intent_wakes(self, waker_client: TestClient) -> None:
        """SendBody defaults intent to 'triage', which is a wake intent."""
        _register(waker_client, "alice")
        _register(waker_client, "bob")
        resp = waker_client.post(
            "/send",
            json={"sender": "alice", "recipient": "bob", "body": "default"},
        )
        assert resp.status_code == 200
        assert waker_client._waker_mock.wake.call_count == 1

    def test_review_intent_wakes(self, waker_client: TestClient) -> None:
        _register(waker_client, "alice")
        _register(waker_client, "bob")
        resp = waker_client.post(
            "/send",
            json={"sender": "alice", "recipient": "bob", "body": "review", "intent": "review"},
        )
        assert resp.status_code == 200
        assert waker_client._waker_mock.wake.call_count == 1

    def test_question_intent_wakes(self, waker_client: TestClient) -> None:
        _register(waker_client, "alice")
        _register(waker_client, "bob")
        resp = waker_client.post(
            "/send",
            json={"sender": "alice", "recipient": "bob", "body": "question", "intent": "question"},
        )
        assert resp.status_code == 200
        assert waker_client._waker_mock.wake.call_count == 1

    def test_invalid_intent_downgrades_and_wakes(self, waker_client: TestClient) -> None:
        """Unknown intent is downgraded to DEFAULT_INTENT ('execute') which wakes."""
        _register(waker_client, "alice")
        _register(waker_client, "bob")
        resp = waker_client.post(
            "/send",
            json={"sender": "alice", "recipient": "bob", "body": "banana", "intent": "banana"},
        )
        assert resp.status_code == 200
        # 'banana' normalizes to 'execute' (DEFAULT_INTENT), which wakes
        assert waker_client._waker_mock.wake.call_count == 1
