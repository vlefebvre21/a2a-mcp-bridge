"""Pydantic models and validation constants for a2a-mcp-bridge."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

AGENT_ID_PATTERN: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_BODY_BYTES: int = 64 * 1024
MAX_METADATA_BYTES: int = 4 * 1024
MAX_SESSION_ID_BYTES: int = 128


class AgentId:
    """Validator helper (not a model). Enforces the agent_id format."""

    PATTERN: ClassVar[re.Pattern[str]] = AGENT_ID_PATTERN

    @classmethod
    def validate(cls, value: str) -> str:
        if not isinstance(value, str) or not cls.PATTERN.match(value):
            raise ValueError(f"invalid agent_id: {value!r}")
        return value


class Message(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    sender_id: str
    recipient_id: str
    body: str = Field(max_length=MAX_BODY_BYTES)
    metadata: dict[str, Any] | None = None
    created_at: datetime
    read_at: datetime | None = None
    sender_session_id: str | None = None
    intent: str = "triage"

    @field_validator("sender_id", "recipient_id")
    @classmethod
    def _validate_agent_id(cls, v: str) -> str:
        return AgentId.validate(v)


class AgentRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    agent_id: str
    first_seen_at: datetime
    last_seen_at: datetime
    online: bool
    metadata: dict[str, Any] | None = None

    @field_validator("agent_id")
    @classmethod
    def _validate_agent_id(cls, v: str) -> str:
        return AgentId.validate(v)


class SendResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_id: str
    sent_at: datetime
    recipient: str

    @field_validator("recipient")
    @classmethod
    def _validate_recipient(cls, v: str) -> str:
        return AgentId.validate(v)
