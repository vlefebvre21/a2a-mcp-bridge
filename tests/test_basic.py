"""Basic unit tests for parsers, validators, and handlers."""
from datetime import UTC, datetime

import pytest

from a2a_mcp_bridge.bus_store import _parse_agent_record, _parse_message
from a2a_mcp_bridge.intents import DEFAULT_INTENT, VALID_INTENTS, normalize_intent
from a2a_mcp_bridge.models import AgentId
from a2a_mcp_bridge.tools import _parse_content_disposition


class TestParseMessage:
    """Tests for the _parse_message helper in bus_store.py."""

    def test_parse_message_minimal(self) -> None:
        data = {
            "id": "msg-01",
            "sender": "alice",
            "recipient": "bob",
            "body": "hello",
            "sent_at": "2025-01-01T00:00:00+00:00",
        }
        msg = _parse_message(data)
        assert msg.id == "msg-01"
        assert msg.sender_id == "alice"
        assert msg.body == "hello"
        assert msg.intent == "triage"  # default

    def test_parse_message_full(self) -> None:
        data = {
            "id": "msg-02",
            "sender_id": "charlie",
            "recipient_id": "dave",
            "body": "world",
            "sent_at": "2025-06-15T12:30:00+00:00",
            "read_at": "2025-06-15T12:31:00+00:00",
            "metadata": {"key": "value"},
            "sender_session_id": "sess-abc",
            "intent": "execute",
        }
        msg = _parse_message(data)
        assert msg.id == "msg-02"
        assert msg.sender_id == "charlie"
        assert msg.read_at is not None
        assert msg.intent == "execute"
        assert msg.sender_session_id == "sess-abc"

    def test_parse_message_missing_fields_raises(self) -> None:
        with pytest.raises(KeyError):
            _parse_message({"id": "msg-03"})

    def test_parse_message_datetime_passthrough(self) -> None:
        """When sent_at is already a datetime object, it should pass through."""
        now = datetime.now(UTC)
        data = {
            "id": "msg-04",
            "sender": "x",
            "recipient": "y",
            "body": "test",
            "sent_at": now,
        }
        msg = _parse_message(data)
        assert msg.created_at == now


class TestParseAgentRecord:
    """Tests for the _parse_agent_record helper in bus_store.py."""

    def test_parse_agent_record(self) -> None:
        data = {
            "agent_id": "vlbeau-qwen36",
            "first_seen_at": "2025-01-01T00:00:00+00:00",
            "last_seen_at": "2025-06-01T00:00:00+00:00",
            "online": False,
        }
        rec = _parse_agent_record(data)
        assert rec.agent_id == "vlbeau-qwen36"
        assert rec.online is False

    def test_parse_agent_record_with_online_true(self) -> None:
        now = datetime.now(UTC).isoformat()
        data = {
            "agent_id": "vlbeau-opus",
            "first_seen_at": now,
            "last_seen_at": now,
            "online": True,
            "metadata": {"role": "reviewer"},
        }
        rec = _parse_agent_record(data)
        assert rec.online is True
        assert rec.metadata == {"role": "reviewer"}


class TestNormalizeIntent:
    """Tests for intent normalization in intents.py."""

    def test_none_returns_default(self) -> None:
        result, downgraded = normalize_intent(None)
        assert result == DEFAULT_INTENT
        assert downgraded is False

    def test_valid_intent_preserved(self) -> None:
        for intent in VALID_INTENTS:
            result, downgraded = normalize_intent(intent)
            assert result == intent
            assert downgraded is False

    def test_unknown_intent_downgraded(self) -> None:
        result, downgraded = normalize_intent("foobar")
        assert result == DEFAULT_INTENT
        assert downgraded is True

    def test_fyi_no_wake(self) -> None:
        result, downgraded = normalize_intent("fyi")
        assert result == "fyi"
        assert downgraded is False


class TestAgentIdValidation:
    """Tests for agent ID validation in models.py."""

    def test_valid_ids(self) -> None:
        for valid in ["a", "agent-1", "agent_2", "x" * 64, "0-prefix"]:
            assert AgentId.validate(valid) == valid

    def test_invalid_ids_raise(self) -> None:
        for invalid in [
            "", "UPPER", "-starts-with-dash", "_under", "has space",
            "x" * 65, "emoji🎉",
        ]:
            with pytest.raises(ValueError, match="invalid agent_id"):
                AgentId.validate(invalid)


class TestParseContentDisposition:
    """Tests for the Content-Disposition parser in tools.py."""

    def test_simple_filename(self) -> None:
        assert _parse_content_disposition(
            'attachment; filename="readme.txt"'
        ) == "readme.txt"

    def test_no_filename(self) -> None:
        assert _parse_content_disposition("attachment") == ""

    def test_filename_without_quotes(self) -> None:
        assert _parse_content_disposition("attachment; filename=report.pdf") == "report.pdf"

    def test_empty_header(self) -> None:
        assert _parse_content_disposition("") == ""
