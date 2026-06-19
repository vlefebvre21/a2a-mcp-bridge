"""Pydantic models and validation constants for a2a-mcp-bridge."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from a2a_mcp_bridge.intents import DEFAULT_INTENT

AGENT_ID_PATTERN: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_BODY_BYTES: int = 64 * 1024
MAX_METADATA_BYTES: int = 4 * 1024
MAX_SESSION_ID_BYTES: int = 128

# Valid intents (ADR-002)
IntentLiteral = Literal["triage", "execute", "review", "question", "fyi"]


class AgentId:
    """Validator helper (not a model). Enforces the agent_id format."""

    PATTERN: ClassVar[re.Pattern[str]] = AGENT_ID_PATTERN

    @classmethod
    def validate(cls, value: str) -> str:
        if not isinstance(value, str) or not cls.PATTERN.match(value):
            raise ValueError(f"invalid agent_id: {value!r}")
        return value


class Message(BaseModel):
    """A message on the A2A bus.

    Type safety:
      - ``metadata`` is strictly `dict[str, Any] | None`, rejects lists/strings.
      - ``intent`` is strictly one of the ADR-002 enum values (Literal).
    """
    model_config = ConfigDict(frozen=True)

    id: str
    sender_id: str
    recipient_id: str
    body: str = Field(max_length=MAX_BODY_BYTES)
    metadata: dict[str, Any] | None = Field(default=None)  # Strict type
    created_at: datetime
    read_at: datetime | None = None
    sender_session_id: str | None = None
    intent: IntentLiteral = "triage"  # Strict Literal type (ADR-002)

    @field_validator("sender_id", "recipient_id")
    @classmethod
    def _validate_agent_id(cls, v: str) -> str:
        return AgentId.validate(v)

    @field_validator("metadata")
    @classmethod
    def _validate_metadata(cls, v: Any) -> dict[str, Any] | None:
        """Ensure metadata is strictly a dict or None (not list, str, etc.)."""
        if v is not None and isinstance(v, dict):
            return v
        elif v is None:
            return None
        raise ValueError(f"metadata must be dict or None, got {type(v).__name__}")

    @field_validator("intent", mode="before")
    @classmethod
    def _validate_intent(cls, v: Any) -> IntentLiteral:
        """Validate intent is one of the allowed ADR-002 values.

        Unknown values downgrade to ``DEFAULT_INTENT`` (from ``intents.py``),
        matching ``normalize_intent`` behaviour — not a hardcoded fallback.
        The Pydantic field default ``"triage"`` (line above) is separate and
        only applies when the field is omitted entirely (backward-compat for
        old DB rows without an intent column).
        """
        if isinstance(v, str):
            valid = {"triage", "execute", "review", "question", "fyi"}
            if v in valid:
                return v  # type: ignore[return-value]
        return DEFAULT_INTENT  # type: ignore[return-value]


class AgentRecord(BaseModel):
    """Registry entry for an agent profile.

    .. note:: ``online`` defaults to ``False`` (v0.10.2+). Previously the field
        was required — callers that omitted ``online`` would get a
        ``ValidationError``. The default was introduced because liveness is
        always decided at the server layer (see ``Store.list_agents`` and
        ``HttpBusStore``), so requiring the caller to pass ``online=False``
        was redundant. This is a **breaking change** for code that relied on
        the field being mandatory to catch construction errors, but no such
        code path existed in the bridge itself.
    """
    model_config = ConfigDict(frozen=True)

    agent_id: str
    first_seen_at: datetime
    last_seen_at: datetime
    online: bool = False  # Default since v0.10.2 — liveness is server-layer concern
    metadata: dict[str, Any] | None = Field(default=None)  # Strict type

    @field_validator("agent_id")
    @classmethod
    def _validate_agent_id(cls, v: str) -> str:
        return AgentId.validate(v)

    @field_validator("metadata")
    @classmethod
    def _validate_metadata(cls, v: Any) -> dict[str, Any] | None:
        """Ensure metadata is strictly a dict or None."""
        if v is not None and isinstance(v, dict):
            return v
        elif v is None:
            return None
        raise ValueError(f"metadata must be dict or None, got {type(v).__name__}")


class SendResult(BaseModel):
    """Result of a send operation."""
    model_config = ConfigDict(frozen=True)

    message_id: str
    sent_at: datetime
    recipient: str
    intent: IntentLiteral = "triage"  # Strict Literal type (ADR-002)

    @field_validator("recipient")
    @classmethod
    def _validate_recipient(cls, v: str) -> str:
        return AgentId.validate(v)
