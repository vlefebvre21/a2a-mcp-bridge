"""Tool implementations — thin adapters over Store (with v0.2 signals + v0.3 wake-up)."""

from __future__ import annotations

import logging
import time
from typing import Any

from .intents import DEFAULT_INTENT, normalize_intent, wakes
from .logging_ext import hash_body, log_event
from .signals import SignalDir
from .store import Store
from .wake import WebhookWaker

logger = logging.getLogger("a2a_mcp_bridge.tools")

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
    waker: WebhookWaker | None = None,
    intent: str | None = None,
) -> dict[str, Any]:
    """Send a message from ``caller_id`` to ``target``.

    On success the function fires two optional best-effort notifications:

    * ``signal_dir.notify(target)`` — touches ``<signal_dir>/<target>.notify``
      so any ``agent_subscribe`` long-poll on the recipient wakes up (v0.2).
    * ``waker.wake(target, sender_id=caller_id)`` — POSTs an HMAC-signed
      webhook to the recipient's Hermes gateway, which spawns a real
      agent session that reads the inbox (v0.4.4+).

    Both hooks are best-effort: failures are logged but never propagated — the
    authoritative record is always the SQLite store.

    ADR-002 wake-intent coupling (v0.6+):
      The optional ``intent`` parameter annotates the message with its
      delivery semantics (``triage``, ``execute``, ``review``, ``question``,
      ``fyi``). Intents in ``NO_WAKE_INTENTS`` (currently just ``fyi``)
      persist the message and touch the signal file but **skip** the webhook
      wake-up — the recipient will see the message at the next natural
      ``agent_inbox`` call instead of spawning a fresh LLM session. Unknown
      values are downgraded to ``triage`` with a WARNING log, preserving
      forward-compat as prescribed by ADR-002 §5.3.
    """
    start = time.perf_counter()
    session_id: str | None = None
    if metadata is not None:
        sid = metadata.get("session_id")
        if isinstance(sid, str):
            session_id = sid

    # Normalise intent (absent → default, unknown → downgrade + warn).
    normalized_intent, downgraded = normalize_intent(intent)
    if downgraded:
        log_event(
            logger,
            event="tool.agent_send.intent_downgraded",
            agent_id=caller_id,
            level=logging.WARNING,
            session_id=session_id,
            target=target,
            requested_intent=intent,
            effective_intent=normalized_intent,
        )

    store.upsert_agent(caller_id)
    try:
        result = store.send_message(
            sender=caller_id,
            recipient=target,
            body=message,
            metadata=metadata,
            intent=normalized_intent,
        )
    except ValueError as e:
        code, _, msg = str(e).partition(":")
        err_code = code.strip() or "ERROR"
        log_event(
            logger,
            event="tool.agent_send",
            agent_id=caller_id,
            level=logging.WARNING,
            session_id=session_id,
            target=target,
            body_hash=hash_body(message),
            error_code=err_code,
            duration_ms=round((time.perf_counter() - start) * 1000, 2),
        )
        return {"error": {"code": err_code, "message": msg.strip() or str(e)}}

    if signal_dir is not None:
        signal_dir.notify(target)

    # Wake policy (ADR-002): skip the webhook for no-wake intents (``fyi``).
    # The message is still persisted and still signals agent_subscribe; we
    # just don't spawn a fresh LLM session for notifications that do not
    # require immediate action.
    if waker is not None:
        if wakes(normalized_intent):
            try:
                waker.wake(target, sender_id=caller_id)
            except Exception as exc:  # pragma: no cover - waker must swallow, defensive
                logger.warning("waker raised for %s: %s", target, exc)
        else:
            logger.info(
                "wake skipped (intent=%s) for target=%s from sender=%s",
                normalized_intent,
                target,
                caller_id,
            )

    log_event(
        logger,
        event="tool.agent_send",
        agent_id=caller_id,
        session_id=session_id,
        target=target,
        message_id=result.message_id,
        body_hash=hash_body(message),
        intent=normalized_intent,
        duration_ms=round((time.perf_counter() - start) * 1000, 2),
    )
    return {
        "message_id": result.message_id,
        "sent_at": result.sent_at.isoformat(),
        "recipient": result.recipient,
        "intent": normalized_intent,
    }


def tool_agent_inbox(
    store: Store,
    caller_id: str,
    limit: int = 10,
    unread_only: bool = True,
    session_id: str | None = None,
    signal_dir: SignalDir | None = None,
) -> dict[str, Any]:
    start = time.perf_counter()
    store.upsert_agent(caller_id)
    messages = store.read_inbox(caller_id, limit=limit, unread_only=unread_only)
    # v0.5.1 — clear the signal file after a consuming read so a subsequent
    # ``agent_subscribe`` does not fast-path on a stale signal whose unread
    # messages have just been drained. We only clear when:
    #   * a ``signal_dir`` is wired (omitted in unit tests with no real fs)
    #   * the read was consuming (``unread_only=True`` — mark-as-read happened)
    #   * at least one message was returned (nothing to drain otherwise; also
    #     avoids masking a future send's signal that arrived between the store
    #     read and this point — see docstring of ``SignalDir.clear``).
    # ``peek_inbox`` (``tool_agent_inbox_peek``) NEVER clears the signal because
    # it does not mutate ``read_at``.
    if signal_dir is not None and unread_only and messages:
        signal_dir.clear(caller_id)
    log_event(
        logger,
        event="tool.agent_inbox",
        agent_id=caller_id,
        session_id=session_id,
        unread_only=unread_only,
        count=len(messages),
        duration_ms=round((time.perf_counter() - start) * 1000, 2),
    )
    return {"messages": [_serialize_message(m) for m in messages]}


def tool_agent_inbox_peek(
    store: Store,
    caller_id: str,
    since_ts: str | None = None,
    limit: int = 50,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Read-only inbox view — no mark-as-read, no state mutation.

    Thin adapter over :meth:`Store.peek_inbox`; see that method's docstring
    for semantics. This is the v0.5 primitive added for ADR-001 (Option A'):
    the Hermes gateway uses it to recover its inbox cache after a restart or
    when the cache is lagging behind the bus, and external tooling uses it
    to inspect an agent's history without consuming messages.

    The response shape matches :func:`tool_agent_inbox` so callers that
    already handle the ``messages`` list can switch freely between the two.
    """
    start = time.perf_counter()
    store.upsert_agent(caller_id)
    messages = store.peek_inbox(caller_id, since_ts=since_ts, limit=limit)
    log_event(
        logger,
        event="tool.agent_inbox_peek",
        agent_id=caller_id,
        session_id=session_id,
        since_ts=since_ts,
        count=len(messages),
        duration_ms=round((time.perf_counter() - start) * 1000, 2),
    )
    return {"messages": [_serialize_message(m) for m in messages]}


def tool_agent_list(
    store: Store,
    caller_id: str,
    active_within_days: int = 7,
    session_id: str | None = None,
) -> dict[str, Any]:
    start = time.perf_counter()
    store.upsert_agent(caller_id)
    agents = store.list_agents(active_within_days=active_within_days)
    log_event(
        logger,
        event="tool.agent_list",
        agent_id=caller_id,
        session_id=session_id,
        count=len(agents),
        duration_ms=round((time.perf_counter() - start) * 1000, 2),
    )
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
    session_id: str | None = None,
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
    start = time.perf_counter()
    store.upsert_agent(caller_id)

    timeout = max(0.0, min(timeout_seconds, MAX_SUBSCRIBE_TIMEOUT_SECONDS))

    # Fast path: messages already waiting → flush & return.
    existing = store.read_inbox(caller_id, limit=limit, unread_only=True)
    if existing:
        log_event(
            logger,
            event="tool.agent_subscribe",
            agent_id=caller_id,
            session_id=session_id,
            timed_out=False,
            fast_path=True,
            count=len(existing),
            duration_ms=round((time.perf_counter() - start) * 1000, 2),
        )
        return {
            "messages": [_serialize_message(m) for m in existing],
            "timed_out": False,
        }

    fired = signal_dir.wait(caller_id, timeout_seconds=timeout, poll_interval=poll_interval)
    if not fired:
        log_event(
            logger,
            event="tool.agent_subscribe",
            agent_id=caller_id,
            session_id=session_id,
            timed_out=True,
            count=0,
            duration_ms=round((time.perf_counter() - start) * 1000, 2),
        )
        return {"messages": [], "timed_out": True}

    messages = store.read_inbox(caller_id, limit=limit, unread_only=True)
    log_event(
        logger,
        event="tool.agent_subscribe",
        agent_id=caller_id,
        session_id=session_id,
        timed_out=False,
        fast_path=False,
        count=len(messages),
        duration_ms=round((time.perf_counter() - start) * 1000, 2),
    )
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
        "sender_session_id": m.sender_session_id,
        "intent": getattr(m, "intent", DEFAULT_INTENT),
    }
