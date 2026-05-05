"""Pydantic models for the Capability Registry (Hermes Agents)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class CostModel(BaseModel):
    """Cost and performance model for a capability."""

    tokens_per_call: float = Field(..., description="Estimated tokens per typical call")
    latency_ms: int = Field(..., description="Average latency in milliseconds")
    monetary_cost_usd: Optional[float] = Field(None, description="Monetary cost if applicable")
    type: Literal["local", "api", "hybrid"] = "local"


class Capability(BaseModel):
    """Represents one specialized skill announced by a Hermes agent."""

    skill_id: str = Field(..., description="Unique skill identifier, e.g. 'code-review-python'")
    description: str = Field(..., description="Human readable description")
    parameters_schema: Dict[str, Any] = Field(default_factory=dict)
    return_schema: Dict[str, Any] = Field(default_factory=dict)
    domain: str = Field(..., description="Domain like 'code', 'research', 'media'")
    cost: CostModel
    supports_streaming: bool = False
    max_context_tokens: Optional[int] = None
    permissions: List[str] = Field(default_factory=lambda: ["read"])
    version: str = "1.0.0"


class AgentInfo(BaseModel):
    """Full information about a registered Hermes agent."""

    agent_id: str
    name: str
    capabilities: List[Capability] = Field(default_factory=list)
    status: Literal["online", "offline", "degraded"] = "online"
    last_heartbeat: datetime = Field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = Field(default_factory=dict)
