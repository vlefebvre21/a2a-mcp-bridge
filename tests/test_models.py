from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from a2a_mcp_bridge.models import (
    MAX_BODY_BYTES,
    AgentId,
    AgentRecord,
    Message,
    SendResult,
)

from a2a_mcp_bridge.intents import DEFAULT_INTENT, normalize_intent


class TestAgentId:
    def test_accepts_valid_ids(self):
        for good in ["agent-a", "a", "agent_1", "012agent", "a" * 64]:
            assert AgentId.validate(good) == good

    def test_rejects_invalid_ids(self):
        for bad in ["", "A", "Agent", "agent!", "-bad", "_bad", "x" * 65, "with space"]:
            with pytest.raises(ValueError):
                AgentId.validate(bad)


class TestMessage:
    def _valid_args(self) -> dict:
        return {
            "id": "01HXYZ",
            "sender_id": "alice",
            "recipient_id": "bob",
            "body": "hello",
            "metadata": None,
            "created_at": datetime.now(UTC),
            "read_at": None,
        }

    def test_valid_message(self):
        msg = Message(**self._valid_args())
        assert msg.body == "hello"
        assert msg.read_at is None

    def test_rejects_oversized_body(self):
        args = self._valid_args()
        args["body"] = "x" * (MAX_BODY_BYTES + 1)
        with pytest.raises(ValidationError):
            Message(**args)

    def test_rejects_invalid_sender_id(self):
        args = self._valid_args()
        args["sender_id"] = "UPPER"
        with pytest.raises(ValidationError):
            Message(**args)

    def test_validate_intent_unknown_downgrades_to_default_intent(self):
        """Unknown intent values must downgrade to DEFAULT_INTENT, not 'triage'."""
        args = self._valid_args()
        args["intent"] = "bogus"
        msg = Message(**args)
        assert msg.intent == DEFAULT_INTENT
        assert msg.intent == "execute"

    def test_validate_intent_consistent_with_normalize_intent(self):
        """_validate_intent and normalize_intent must agree on unknown values."""
        assert Message._validate_intent("bogus") == normalize_intent("bogus")[0]
        assert normalize_intent("bogus")[0] == DEFAULT_INTENT


class TestAgentRecord:
    def test_valid_record(self):
        now = datetime.now(UTC)
        rec = AgentRecord(
            agent_id="a",
            first_seen_at=now,
            last_seen_at=now,
            online=True,
            metadata={"capabilities": ["chat"]},
        )
        assert rec.online is True
        assert rec.metadata == {"capabilities": ["chat"]}

    def test_online_defaults_to_false(self):
        """v0.10.2 breaking change: online is now optional with default False.

        Previously, omitting ``online`` would raise ValidationError. Now it
        silently defaults to False — liveness is always decided at the server
        layer, not at construction time.
        """
        now = datetime.now(UTC)
        rec = AgentRecord(
            agent_id="a",
            first_seen_at=now,
            last_seen_at=now,
        )
        assert rec.online is False
        assert rec.metadata is None


class TestSendResult:
    def test_valid(self):
        now = datetime.now(UTC)
        r = SendResult(message_id="01HXYZ", sent_at=now, recipient="bob")
        assert r.recipient == "bob"
