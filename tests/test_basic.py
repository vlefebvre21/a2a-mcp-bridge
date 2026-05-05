"""Basic unit tests for parsers, validators, and handlers."""
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from a2a_mcp_bridge.bus_store import _parse_agent_record, _parse_message
from a2a_mcp_bridge.exceptions import (
    A2ABridgeError,
    MCPConfigError,
    MCPConnectionError,
    MCPProtocolError,
    MCPValidationError,
    MessageTooLargeError,
)
from a2a_mcp_bridge.intents import DEFAULT_INTENT, VALID_INTENTS, normalize_intent
from a2a_mcp_bridge.models import AgentId
from a2a_mcp_bridge.tools import _parse_content_disposition
from a2a_mcp_bridge.validation import validate_mcp_envelope, validate_tool_params


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


class TestValidateMCPEnvelope:
    """Tests for validate_mcp_envelope() in validation.py."""

    def test_valid_envelope(self) -> None:
        raw = '{"jsonrpc": "2.0", "method": "tools/call", "id": 1}'
        result = validate_mcp_envelope(raw)
        assert isinstance(result, dict)
        assert result["jsonrpc"] == "2.0"
        assert result["method"] == "tools/call"
        assert result["id"] == 1

    def test_invalid_json(self) -> None:
        with pytest.raises(MCPProtocolError) as exc_info:
            validate_mcp_envelope("{invalid json!!!")
        assert exc_info.value.code == "PROTOCOL_ERROR"

    def test_not_object(self) -> None:
        with pytest.raises(MCPProtocolError, match="JSON object"):
            validate_mcp_envelope("[1, 2, 3]")

    def test_missing_required_fields(self) -> None:
        with pytest.raises(MCPProtocolError, match="Missing required"):
            validate_mcp_envelope('{"jsonrpc": "2.0", "id": 1}')

    def test_wrong_version(self) -> None:
        with pytest.raises(MCPProtocolError, match="Unsupported JSON-RPC version"):
            validate_mcp_envelope('{"jsonrpc": "1.0", "method": "test", "id": 1}')

    def test_message_too_large(self) -> None:
        with (
            patch("a2a_mcp_bridge.validation._max_message_bytes", return_value=10),
            pytest.raises(MessageTooLargeError),
        ):
            validate_mcp_envelope("x" * 100)


class TestValidateToolParams:
    """Tests for validate_tool_params() in validation.py."""

    def test_agent_send_valid(self) -> None:
        params = {"target": "bob", "message": "hello"}
        result = validate_tool_params("agent_send", params)
        assert result == params

    def test_agent_send_bad_target(self) -> None:
        with pytest.raises(MCPValidationError):
            validate_tool_params("agent_send", {"target": "BadTarget", "message": "hi"})

    def test_agent_send_empty_message(self) -> None:
        result = validate_tool_params("agent_send", {"target": "bob", "message": ""})
        assert result["message"] == ""

    def test_agent_send_oversize_message(self) -> None:
        big_msg = "x" * 65537
        with pytest.raises(MCPValidationError, match="65536"):
            validate_tool_params("agent_send", {"target": "bob", "message": big_msg})

    def test_agent_send_file_valid(self) -> None:
        params = {"target": "bob", "file_path": "/tmp/data.csv"}
        result = validate_tool_params("agent_send_file", params)
        assert result == params

    def test_agent_send_file_no_path(self) -> None:
        with pytest.raises(MCPValidationError, match="file_path"):
            validate_tool_params("agent_send_file", {"target": "bob"})

    def test_agent_subscribe_valid(self) -> None:
        params = {"timeout_seconds": 30}
        result = validate_tool_params("agent_subscribe", params)
        assert result == params

    def test_agent_subscribe_timeout_too_high(self) -> None:
        with pytest.raises(MCPValidationError, match="55"):
            validate_tool_params("agent_subscribe", {"timeout_seconds": 60})

    def test_agent_fetch_file_valid(self) -> None:
        params = {"transfer_id": "abc-123"}
        result = validate_tool_params("agent_fetch_file", params)
        assert result == params

    def test_agent_fetch_file_no_transfer_id(self) -> None:
        with pytest.raises(MCPValidationError, match="transfer_id"):
            validate_tool_params("agent_fetch_file", {"transfer_id": ""})

    def test_agent_delete_file_valid(self) -> None:
        params = {"transfer_id": "abc-123"}
        result = validate_tool_params("agent_delete_file", params)
        assert result == params

    def test_agent_delete_file_empty_id(self) -> None:
        with pytest.raises(MCPValidationError, match="transfer_id"):
            validate_tool_params("agent_delete_file", {"transfer_id": ""})

    def test_params_not_dict(self) -> None:
        with pytest.raises(MCPValidationError, match="object"):
            validate_tool_params("agent_send", "string")

    def test_params_none_becomes_empty(self) -> None:
        result = validate_tool_params("unknown_tool", None)
        assert result == {}


class TestExceptions:
    """Tests for the exception hierarchy in exceptions.py."""

    def test_hierarchy(self) -> None:
        assert issubclass(MCPProtocolError, MCPValidationError)
        assert issubclass(MCPValidationError, A2ABridgeError)

    def test_message_too_large_is_validation(self) -> None:
        assert issubclass(MessageTooLargeError, MCPValidationError)

    def test_exception_codes(self) -> None:
        assert A2ABridgeError.code == "A2A_BRIDGE_ERROR"
        assert MCPConnectionError.code == "CONNECTION_ERROR"
        assert MCPValidationError.code == "VALIDATION_ERROR"
        assert MessageTooLargeError.code == "MESSAGE_TOO_LARGE"
        assert MCPConfigError.code == "CONFIG_ERROR"
        assert MCPProtocolError.code == "PROTOCOL_ERROR"

    def test_base_is_exception(self) -> None:
        assert issubclass(A2ABridgeError, Exception)
