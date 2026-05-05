"""Query and discovery logic for Capability Registry."""

from __future__ import annotations

from typing import Any, Dict, List

from .manager import CapabilityRegistry
from .models import AgentInfo


class RegistryQuery:
    """Handles discovery and intelligent querying of capabilities."""

    def __init__(self, registry: CapabilityRegistry) -> None:
        self.registry = registry

    # ── discovery ──────────────────────────────────────────────────────

    def discover_all(self) -> List[Dict[str, Any]]:
        """Return all available capabilities in a clean format for A2A/MCP."""
        result: List[Dict[str, Any]] = []
        for agent in self.registry.get_all_agents():
            for cap in agent.capabilities:
                result.append(
                    {
                        "agent_id": agent.agent_id,
                        "agent_name": agent.name,
                        "skill_id": cap.skill_id,
                        "description": cap.description,
                        "domain": cap.domain,
                        "cost": cap.cost.model_dump(),
                        "supports_streaming": cap.supports_streaming,
                        "permissions": cap.permissions,
                    }
                )
        return result

    # ── scoring ────────────────────────────────────────────────────────

    def find_best(
        self,
        skill_keyword: str,
        max_cost: float | None = None,
    ) -> List[Dict[str, Any]]:
        """Find best matching agents for a skill (simple scoring for now).

        Score heuristics:
          - base = 1.0 if keyword matches, 0.0 otherwise
          - penalty 0.5 if max_cost is set and token cost exceeds it
          - sorted descending by score, then ascending by token cost
        """
        matches: List[Dict[str, Any]] = []
        kw = skill_keyword.lower()

        for agent in self.registry.get_all_agents():
            for cap in agent.capabilities:
                if kw in cap.skill_id.lower() or kw in cap.description.lower():
                    score = 1.0
                    if max_cost is not None and cap.cost.tokens_per_call > max_cost:
                        score = 0.5
                    matches.append(
                        {
                            "agent_id": agent.agent_id,
                            "skill_id": cap.skill_id,
                            "score": score,
                            "cost_tokens": cap.cost.tokens_per_call,
                        }
                    )

        # Sort by score descending, then cost ascending
        matches.sort(key=lambda x: (-x["score"], x["cost_tokens"]))
        return matches
