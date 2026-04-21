from datetime import datetime, timezone
import pytest
from pydantic import ValidationError
from a2a_mcp_bridge.models import (
    AgentId,
    AgentRecord,
    Message,
    SendResult,
    MAX_BODY_BYTES,
    MAX_METADATA_BYTES,
)


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
            "created_at": datetime.now(timezone.utc),
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


class TestAgentRecord:
    def test_valid_record(self):
        now = datetime.now(timezone.utc)
        rec = AgentRecord(
            agent_id="a",
            first_seen_at=now,
            last_seen_at=now,
            online=True,
            metadata={"capabilities": ["chat"]},
        )
        assert rec.online is True
        assert rec.metadata == {"capabilities": ["chat"]}


class TestSendResult:
    def test_valid(self):
        now = datetime.now(timezone.utc)
        r = SendResult(message_id="01HXYZ", sent_at=now, recipient="bob")
        assert r.recipient == "bob"
