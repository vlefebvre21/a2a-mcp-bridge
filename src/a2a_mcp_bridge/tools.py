"""Tool implementations — thin adapters over Store."""

from __future__ import annotations

from typing import Any

from .store import Store


def tool_agent_send(
    store: Store,
    caller_id: str,
    target: str,
    message: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    store.upsert_agent(caller_id)
    try:
        result = store.send_message(
            sender=caller_id,
            recipient=target,
            body=message,
            metadata=metadata,
        )
    except ValueError as e:
        code, _, msg = str(e).partition(":")
        return {"error": {"code": code.strip() or "ERROR", "message": msg.strip() or str(e)}}
    return {
        "message_id": result.message_id,
        "sent_at": result.sent_at.isoformat(),
        "recipient": result.recipient,
    }


def tool_agent_inbox(
    store: Store,
    caller_id: str,
    limit: int = 10,
    unread_only: bool = True,
) -> dict[str, Any]:
    store.upsert_agent(caller_id)
    messages = store.read_inbox(caller_id, limit=limit, unread_only=unread_only)
    return {
        "messages": [
            {
                "message_id": m.id,
                "sender": m.sender_id,
                "body": m.body,
                "metadata": m.metadata,
                "sent_at": m.created_at.isoformat(),
                "read_at": m.read_at.isoformat() if m.read_at else None,
            }
            for m in messages
        ]
    }


def tool_agent_list(
    store: Store,
    caller_id: str,
    active_within_days: int = 7,
) -> dict[str, Any]:
    store.upsert_agent(caller_id)
    agents = store.list_agents(active_within_days=active_within_days)
    return {
        "agents": [
            {
                "agent_id": a.agent_id,
                "first_seen_at": a.first_seen_at.isoformat(),
                "last_seen_at": a.last_seen_at.isoformat(),
                "online": a.online,
                "metadata": a.metadata,
            }
            for a in agents
        ]
    }
