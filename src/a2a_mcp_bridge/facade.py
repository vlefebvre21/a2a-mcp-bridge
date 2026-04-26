"""HTTP façade server — exposes the a2a-mcp-bridge SQLite bus over REST.

This module defines :func:`create_app` (factory pattern) so the FastAPI
application can be instantiated with real or faked backends for testing.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .intents import wakes
from .signals import SignalDir
from .store import Store
from .wake import WebhookWaker


def _serialise_message(msg: Any) -> dict[str, Any]:
    """Convert a Message or sqlite3.Row into a serialisable dict.

    Field mapping (mirrors what HttpBusStore._parse_message expects):
      sender_id → sender,  recipient_id → recipient,  created_at → sent_at
    """
    # Handle both pydantic Message objects and raw sqlite3.Row objects
    if hasattr(msg, "sender_id"):
        # Pydantic model (Message)
        data = {
            "id": msg.id,
            "sender": msg.sender_id,
            "recipient": msg.recipient_id,
            "body": msg.body,
            "metadata": msg.metadata,
            "sent_at": msg.created_at.isoformat(),
            "read_at": msg.read_at.isoformat() if msg.read_at else None,
            "sender_session_id": msg.sender_session_id,
            "intent": msg.intent,
        }
    else:
        # sqlite3.Row (from test factory or raw queries)
        data = {
            "id": msg["id"],
            "sender": msg["sender_id"],
            "recipient": msg["recipient_id"],
            "body": msg["body"],
            "metadata": msg["metadata"],
            "sent_at": msg["created_at"],
            "read_at": msg["read_at"],
            "sender_session_id": msg["sender_session_id"],
            "intent": msg.get("intent", "triage"),
        }
    return data


def _serialise_agent(agent: Any) -> dict[str, Any]:
    """Convert an AgentRecord into a serialisable dict."""
    if hasattr(agent, "agent_id"):
        return {
            "agent_id": agent.agent_id,
            "first_seen_at": agent.first_seen_at.isoformat(),
            "last_seen_at": agent.last_seen_at.isoformat(),
            "online": agent.online,
            "metadata": agent.metadata,
        }
    else:
        return agent


# -------------------------------------------------------
# Endpoint handlers
# -------------------------------------------------------

def _check_auth(request: Request, api_key: str | None) -> None:
    """Raise HTTP 401 if API key auth is enabled and missing/invalid."""
    if api_key is None:
        return  # dev mode — no auth
    key = request.headers.get("X-Api-Key")
    if not key or key != api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")


def register_handler(
    store: Store, body: dict[str, Any]
) -> JSONResponse:
    store.upsert_agent(body.get("agent_id", ""), body.get("metadata"))
    return JSONResponse({"ok": True})


def send_handler(
    store: Store,
    waker: WebhookWaker | None,
    signal_dir: SignalDir | None,
    body: dict[str, Any],
) -> JSONResponse:
    try:
        sender = body["sender"]
        recipient = body["recipient"]
        message = body["body"]
        intent = body.get("intent", "triage")
        metadata = body.get("metadata")
    except KeyError as exc:
        return JSONResponse(
            {"error": {"code": "VALIDATION_ERROR", "message": f"Missing required field: {exc}"}},
            status_code=400,
        )

    try:
        result = store.send_message(
            sender, recipient, message, metadata, intent
        )
    except ValueError as exc:
        err_str = str(exc)
        if "TARGET_SELF" in err_str:
            return JSONResponse(
                {"error": {"code": "VALIDATION_ERROR", "message": err_str}},
                status_code=400,
            )
        if "TARGET_UNKNOWN" in err_str:
            return JSONResponse(
                {"error": {"code": "TARGET_UNKNOWN", "message": err_str}},
                status_code=400,
            )
        return JSONResponse(
            {"error": {"code": "VALIDATION_ERROR", "message": err_str}},
            status_code=400,
        )

    # Wake the recipient if intent warrants it
    if waker is not None and wakes(intent):
        waker.wake(recipient, sender)
    if signal_dir:
        signal_dir.notify(recipient)

    return JSONResponse(
        {
            "message_id": result.message_id,
            "sent_at": result.sent_at.isoformat(),
            "recipient": result.recipient,
        }
    )


def inbox_handler(
    store: Store, body: dict[str, Any]
) -> JSONResponse:
    agent_id = body["agent_id"]
    limit = body.get("limit", 10)
    unread_only = body.get("unread_only", True)
    messages = store.read_inbox(agent_id, limit, unread_only)
    # Messages are Message objects with datetime fields — need serialisation
    serialised = [_serialise_message(m) for m in messages]
    return JSONResponse({"messages": serialised})


def inbox_peek_handler(
    store: Store, body: dict[str, Any]
) -> JSONResponse:
    agent_id = body["agent_id"]
    since_ts = body.get("since_ts")
    limit = body.get("limit", 50)
    messages = store.peek_inbox(agent_id, since_ts, limit)
    serialised = [_serialise_message(m) for m in messages]
    return JSONResponse({"messages": serialised})


def list_handler(
    store: Store, body: dict[str, Any]
) -> JSONResponse:
    active_within_days = body.get("active_within_days", 7)
    agents = store.list_agents(active_within_days)
    serialised = [_serialise_agent(a) for a in agents]
    return JSONResponse({"agents": serialised})


def subscribe_handler(
    store: Store,
    signal_dir: SignalDir | None,
    body: dict[str, Any],
) -> JSONResponse:
    agent_id = body["agent_id"]
    timeout_seconds = min(body.get("timeout_seconds", 30.0), 55.0)
    limit = body.get("limit", 10)
    try:
        messages, timed_out = store.subscribe(agent_id, timeout_seconds, limit)
    except RuntimeError as exc:
        return JSONResponse(
            {"error": {"code": "CONFIG_ERROR", "message": str(exc)}},
            status_code=500,
        )
    serialised = [_serialise_message(m) for m in messages]
    return JSONResponse({"messages": serialised, "timed_out": timed_out})


# -------------------------------------------------------
# App factory
# -------------------------------------------------------

def create_app(
    db_path: str = ":memory:",
    api_key: str | None = None,
    signal_dir: SignalDir | None = None,
    waker: WebhookWaker | None = None,
) -> FastAPI:
    """Create the FastAPI application with the specified backend.

    This is the factory function used by both the CLI *serve* action
    (production) and the test suite (testing).
    """
    store = Store(db_path, signal_dir=signal_dir, check_same_thread=False)
    store.init_schema()

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[override]
        yield
        store.close()

    app = FastAPI(
        title="a2a-mcp-bridge Bus Façade",
        version="0.5.1",
        lifespan=lifespan,  # type: ignore[arg-type]
    )

    @app.get("/bus/health")
    async def health() -> JSONResponse:
        agent_count = len(store.list_agents())
        return JSONResponse({"status": "ok", "version": "0.5.1", "agents": agent_count})

    @app.post("/bus/register")
    async def register(request: Request) -> JSONResponse:
        _check_auth(request, api_key)
        body = await request.json()
        return register_handler(store, body)

    @app.post("/bus/send")
    async def send(request: Request) -> JSONResponse:
        _check_auth(request, api_key)
        body = await request.json()
        return send_handler(store, waker, signal_dir, body)

    @app.post("/bus/inbox")
    async def inbox(request: Request) -> JSONResponse:
        _check_auth(request, api_key)
        body = await request.json()
        return inbox_handler(store, body)

    @app.post("/bus/inbox_peek")
    async def inbox_peek(request: Request) -> JSONResponse:
        _check_auth(request, api_key)
        body = await request.json()
        return inbox_peek_handler(store, body)

    @app.post("/bus/list")
    async def list_agents(request: Request) -> JSONResponse:
        _check_auth(request, api_key)
        body = await request.json()
        return list_handler(store, body)

    @app.post("/bus/subscribe")
    async def subscribe(request: Request) -> JSONResponse:
        _check_auth(request, api_key)
        body = await request.json()
        return subscribe_handler(store, signal_dir, body)

    return app
