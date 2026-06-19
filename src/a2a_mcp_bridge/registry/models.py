"""Thin re-export shim — models moved to ``a2a_mcp_bridge.models`` (ADR-008)."""

from a2a_mcp_bridge.models import AgentInfo, Capability, CostModel

__all__ = ["AgentInfo", "Capability", "CostModel"]
