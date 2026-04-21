"""Tool implementations — thin adapters over Store (and SignalDir in v0.2)."""

from __future__ import annotations

from typing import Any

from .signals import SignalDir
from .store import Store

# Default long-poll cap for agent_subscribe — keep below typical MCP client
# timeouts (60 s) so we always answer cleanly.
MAX_SUBSCRIBE_TIMEOUT_SECONDS: float = 55.0


def tool_agent_send(
    store: Store,
    caller_id: str,
    target: str,
    message: str,
    metadata: dict[str, Any] | None = None,
    signal_dir: SignalDir | None = None,
) -> dict[str, Any]:
    """Send a message from ``caller_id`` to ``target``.

    If ``signal_dir`` is provided, touch the recipient's signal file on success
    so that any ``agent_subscribe`` long-poll wakes up immediately. The signal
    is a best-effort optimisation; the authoritative record lives in SQLite.
    """
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

    if signal_dir is not None:
        signal_dir.notify(target)

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


def tool_agent_subscribe(
    store: Store,
    caller_id: str,
    signal_dir: SignalDir,
    timeout_seconds: float = 30.0,
    poll_interval: float = 0.2,
    limit: int = 10,
) -> dict[str, Any]:
    """Long-poll the caller's inbox until a message arrives or timeout expires.

    This is the v0.2 real-time delivery primitive. The tool returns at most
    after ``timeout_seconds`` (capped at :data:`MAX_SUBSCRIBE_TIMEOUT_SECONDS`
    to stay within MCP transport deadlines). If messages are already waiting
    at call time, it returns them immediately without sleeping.

    Returns a payload with the same shape as :func:`tool_agent_inbox`, plus a
    ``timed_out`` boolean indicating whether the wait hit its deadline with no
    signal.
    """
    store.upsert_agent(caller_id)

    timeout = max(0.0, min(timeout_seconds, MAX_SUBSCRIBE_TIMEOUT_SECONDS))

    # Fast path: messages already waiting → flush & return.
    existing = store.read_inbox(caller_id, limit=limit, unread_only=True)
    if existing:
        return {
            "messages": [_serialize_message(m) for m in existing],
            "timed_out": False,
        }

    fired = signal_dir.wait(caller_id, timeout_seconds=timeout, poll_interval=poll_interval)
    if not fired:
        return {"messages": [], "timed_out": True}

    messages = store.read_inbox(caller_id, limit=limit, unread_only=True)
    return {
        "messages": [_serialize_message(m) for m in messages],
        "timed_out": False,
    }


def _serialize_message(m: Any) -> dict[str, Any]:
    return {
        "message_id": m.id,
        "sender": m.sender_id,
        "body": m.body,
        "metadata": m.metadata,
        "sent_at": m.created_at.isoformat(),
        "read_at": m.read_at.isoformat() if m.read_at else None,
    }
