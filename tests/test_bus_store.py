"""Tests for BusStore Protocol conformance and HttpBusStore with mocked httpx."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from a2a_mcp_bridge.bus_store import (
    BusStore,
    HttpBusStore,
    _parse_agent_record,
    _parse_message,
)
from a2a_mcp_bridge.models import AgentRecord, Message, SendResult
from a2a_mcp_bridge.signals import SignalDir
from a2a_mcp_bridge.store import Store

# ---------------------------------------------------------------------------
# Part 1: Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_store_satisfies_bus_store_protocol(self, tmp_path: Path) -> None:
        signal_dir = SignalDir(str(tmp_path / "signals"))
        store = Store(str(tmp_path / "bus.sqlite"), signal_dir=signal_dir)
        store.init_schema()
        assert isinstance(store, BusStore)
        store.close()

    def test_http_bus_store_satisfies_bus_store_protocol(self) -> None:
        # Verify the class satisfies the Protocol by checking a bare instance
        # created via __new__ (avoids needing httpx installed).
        obj = HttpBusStore.__new__(HttpBusStore)
        assert isinstance(obj, BusStore)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Fake httpx exception hierarchy so ``except httpx.HTTPError`` etc. inside
# HttpBusStore methods actually catch the errors we raise from the mock client.
class _FakeHTTPError(Exception):
    """Stand-in for httpx.HTTPError."""


class _FakeHTTPStatusError(_FakeHTTPError):
    """Stand-in for httpx.HTTPStatusError."""

    def __init__(self, *args, response=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.response = response or MagicMock()


def _make_http_store() -> tuple[HttpBusStore, MagicMock]:
    """Create an HttpBusStore with a fully mocked _client.

    We build the instance via ``__new__`` to avoid the ``import httpx``
    inside ``__init__``, then manually wire up the attributes.  We also
    inject a fake ``httpx`` module into ``bus_store``'s namespace so
    that ``except httpx.HTTPError`` / ``except httpx.HTTPStatusError``
    in the methods catch our fake exceptions.
    """
    import a2a_mcp_bridge.bus_store as mod

    fake_httpx = MagicMock()
    fake_httpx.HTTPError = _FakeHTTPError
    fake_httpx.HTTPStatusError = _FakeHTTPStatusError
    mod.httpx = fake_httpx

    store = HttpBusStore.__new__(HttpBusStore)
    store._base_url = "http://localhost:8443/bus"
    store._agent_id = "test-agent"
    store._timeout = 65.0
    store._httpx = fake_httpx
    store._client = MagicMock()
    return store, store._client


def _mock_response(json_data: dict, status_code: int = 200):
    """Build a mock httpx.Response."""
    resp = MagicMock()
    resp.json.return_value = json_data
    resp.status_code = status_code
    resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def http_store_and_client():
    store, client = _make_http_store()
    yield store, client
    # Clean up the injected httpx module
    import a2a_mcp_bridge.bus_store as mod

    if hasattr(mod, "httpx"):
        del mod.httpx


@pytest.fixture
def http_store(http_store_and_client):
    return http_store_and_client[0]


@pytest.fixture
def mock_client(http_store_and_client):
    return http_store_and_client[1]


# ---------------------------------------------------------------------------
# Part 2: HttpBusStore method tests
# ---------------------------------------------------------------------------


class TestUpsertAgent:
    def test_upsert_agent_posts_to_register(self, http_store, mock_client) -> None:
        resp = _mock_response({"ok": True})
        mock_client.post.return_value = resp

        http_store.upsert_agent("alice", metadata={"role": "worker"})

        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "http://localhost:8443/bus/register"
        payload = call_args[1]["json"]
        assert payload["agent_id"] == "alice"
        assert payload["metadata"] == {"role": "worker"}

    def test_upsert_agent_best_effort_on_error(self, http_store, mock_client) -> None:
        mock_client.post.side_effect = Exception("connection refused")
        # Must not raise — best-effort
        http_store.upsert_agent("alice")

    def test_upsert_agent_without_metadata(self, http_store, mock_client) -> None:
        resp = _mock_response({"ok": True})
        mock_client.post.return_value = resp

        http_store.upsert_agent("alice")

        payload = mock_client.post.call_args[1]["json"]
        assert "metadata" not in payload
        assert payload["agent_id"] == "alice"


class TestSendMessage:
    def test_send_message_posts_and_returns_send_result(
        self, http_store, mock_client
    ) -> None:
        resp = _mock_response(
            {
                "message_id": "abc123",
                "sent_at": "2026-01-01T00:00:00+00:00",
                "recipient": "bob",
            }
        )
        mock_client.post.return_value = resp

        result = http_store.send_message(
            "alice", "bob", "hello", intent="triage"
        )

        assert isinstance(result, SendResult)
        assert result.message_id == "abc123"
        assert result.recipient == "bob"
        assert result.sent_at == datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

        call_args = mock_client.post.call_args
        assert call_args[0][0] == "http://localhost:8443/bus/send"
        payload = call_args[1]["json"]
        assert payload["sender"] == "alice"
        assert payload["recipient"] == "bob"
        assert payload["body"] == "hello"
        assert payload["intent"] == "triage"

    def test_send_message_raises_on_error_response(
        self, http_store, mock_client
    ) -> None:
        resp = _mock_response(
            {"error": {"code": "TARGET_UNKNOWN", "message": "bob"}}
        )
        mock_client.post.return_value = resp

        with pytest.raises(ValueError, match="TARGET_UNKNOWN"):
            http_store.send_message("alice", "bob", "hi")

    def test_send_message_with_metadata(
        self, http_store, mock_client
    ) -> None:
        resp = _mock_response(
            {
                "message_id": "m1",
                "sent_at": "2026-06-01T12:00:00+00:00",
                "recipient": "bob",
            }
        )
        mock_client.post.return_value = resp

        http_store.send_message(
            "alice", "bob", "hi", metadata={"session_id": "s1"}, intent="execute"
        )

        payload = mock_client.post.call_args[1]["json"]
        assert payload["metadata"] == {"session_id": "s1"}
        assert payload["intent"] == "execute"


class TestReadInbox:
    def test_read_inbox_returns_messages(self, http_store, mock_client) -> None:
        resp = _mock_response(
            {
                "messages": [
                    {
                        "id": "1",
                        "sender": "alice",
                        "recipient": "test-agent",
                        "body": "hi",
                        "sent_at": "2026-01-15T10:30:00+00:00",
                        "intent": "triage",
                    }
                ]
            }
        )
        mock_client.post.return_value = resp

        messages = http_store.read_inbox("test-agent", limit=5, unread_only=True)

        assert len(messages) == 1
        assert isinstance(messages[0], Message)
        assert messages[0].id == "1"
        assert messages[0].sender_id == "alice"
        assert messages[0].body == "hi"

        call_args = mock_client.post.call_args
        assert call_args[0][0] == "http://localhost:8443/bus/inbox"
        payload = call_args[1]["json"]
        assert payload["agent_id"] == "test-agent"
        assert payload["limit"] == 5
        assert payload["unread_only"] is True

    def test_read_inbox_returns_empty_on_error(
        self, http_store, mock_client
    ) -> None:
        mock_client.post.side_effect = _FakeHTTPError("network down")
        result = http_store.read_inbox("test-agent")
        assert result == []


class TestPeekInbox:
    def test_peek_inbox_with_since_ts(self, http_store, mock_client) -> None:
        resp = _mock_response({"messages": []})
        mock_client.post.return_value = resp

        http_store.peek_inbox(
            "test-agent", since_ts="2026-01-01T00:00:00Z", limit=25
        )

        call_args = mock_client.post.call_args
        assert call_args[0][0] == "http://localhost:8443/bus/inbox_peek"
        payload = call_args[1]["json"]
        assert payload["agent_id"] == "test-agent"
        assert payload["limit"] == 25
        assert payload["since_ts"] == "2026-01-01T00:00:00Z"

    def test_peek_inbox_without_since_ts(self, http_store, mock_client) -> None:
        resp = _mock_response({"messages": []})
        mock_client.post.return_value = resp

        http_store.peek_inbox("test-agent")

        payload = mock_client.post.call_args[1]["json"]
        assert "since_ts" not in payload


class TestListAgents:
    def test_list_agents_returns_records(self, http_store, mock_client) -> None:
        resp = _mock_response(
            {
                "agents": [
                    {
                        "agent_id": "alice",
                        "first_seen_at": "2026-01-01T00:00:00+00:00",
                        "last_seen_at": "2026-04-01T12:00:00+00:00",
                    }
                ]
            }
        )
        mock_client.post.return_value = resp

        records = http_store.list_agents(active_within_days=3)

        assert len(records) == 1
        assert isinstance(records[0], AgentRecord)
        assert records[0].agent_id == "alice"

        call_args = mock_client.post.call_args
        assert call_args[0][0] == "http://localhost:8443/bus/list"
        payload = call_args[1]["json"]
        assert payload["active_within_days"] == 3


class TestSubscribe:
    def test_subscribe_returns_messages_and_timed_out(
        self, http_store, mock_client
    ) -> None:
        resp = _mock_response(
            {
                "messages": [
                    {
                        "id": "2",
                        "sender": "bob",
                        "recipient": "test-agent",
                        "body": "wake up",
                        "sent_at": "2026-03-01T08:00:00+00:00",
                        "intent": "execute",
                    }
                ],
                "timed_out": False,
            }
        )
        mock_client.post.return_value = resp

        messages, timed_out = http_store.subscribe(
            "test-agent", timeout_seconds=10, limit=5
        )

        assert len(messages) == 1
        assert messages[0].body == "wake up"
        assert timed_out is False

        call_args = mock_client.post.call_args
        assert call_args[0][0] == "http://localhost:8443/bus/subscribe"
        payload = call_args[1]["json"]
        assert payload["timeout_seconds"] == 10
        assert payload["limit"] == 5

    def test_subscribe_returns_empty_on_network_error(
        self, http_store, mock_client
    ) -> None:
        mock_client.post.side_effect = _FakeHTTPError("timeout")
        result = http_store.subscribe("test-agent")
        assert result == ([], True)


class TestParseHelpers:
    def test_parse_message_field_mapping(self) -> None:
        data = {
            "id": "msg-1",
            "sender": "alice",
            "recipient": "bob",
            "body": "hello",
            "sent_at": "2026-02-14T12:00:00+00:00",
            "intent": "question",
            "metadata": {"key": "val"},
            "sender_session_id": "sess-1",
            "read_at": "2026-02-14T13:00:00+00:00",
        }
        msg = _parse_message(data)

        assert msg.id == "msg-1"
        # sender → sender_id mapping
        assert msg.sender_id == "alice"
        # sent_at → created_at mapping
        assert msg.created_at == datetime(2026, 2, 14, 12, 0, 0, tzinfo=UTC)
        assert msg.recipient_id == "bob"
        assert msg.body == "hello"
        assert msg.intent == "question"
        assert msg.metadata == {"key": "val"}
        assert msg.sender_session_id == "sess-1"
        assert msg.read_at == datetime(2026, 2, 14, 13, 0, 0, tzinfo=UTC)

    def test_parse_message_sender_id_fallback(self) -> None:
        """When 'sender' is absent but 'sender_id' is present, use sender_id."""
        data = {
            "id": "msg-2",
            "sender_id": "carol",
            "recipient_id": "dave",
            "body": "yo",
            "sent_at": "2026-03-01T00:00:00+00:00",
        }
        msg = _parse_message(data)
        assert msg.sender_id == "carol"
        assert msg.recipient_id == "dave"

    def test_parse_agent_record(self) -> None:
        data = {
            "agent_id": "alice",
            "first_seen_at": "2026-01-01T00:00:00+00:00",
            "last_seen_at": "2026-04-01T00:00:00+00:00",
            "online": True,
            "metadata": {"version": "1.0"},
        }
        record = _parse_agent_record(data)
        assert record.agent_id == "alice"
        assert record.online is True
        assert record.metadata == {"version": "1.0"}
        assert record.first_seen_at == datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


class TestClose:
    def test_close_calls_client_close(self, http_store, mock_client) -> None:
        http_store.close()
        mock_client.close.assert_called_once()
