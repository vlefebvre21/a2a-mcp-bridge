"""HTTP façade server — exposes the a2a-mcp-bridge SQLite bus over REST.

This module defines :func:`create_app` (factory pattern) so the FastAPI
application can be instantiated with real or faked backend dependencies.
"""

from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .signals import SignalDir
from .store import Store
from .wake import WebhookWaker

logger = logging.getLogger(__name__)


# -------------------------------------------------------
# Pydantic request models
# -------------------------------------------------------

class RegisterBody(BaseModel):
    agent_id: str = Field(..., min_length=1)
    metadata: dict[str, Any] | None = None


class SendBody(BaseModel):
    sender: str = Field(..., min_length=1)
    recipient: str = Field(..., min_length=1)
    body: str = Field(..., min_length=1)
    intent: str = "triage"
    metadata: dict[str, Any] | None = None


class InboxBody(BaseModel):
    agent_id: str = Field(..., min_length=1)
    limit: int = Field(default=10, ge=1, le=100)
    unread_only: bool = True


class InboxPeekBody(BaseModel):
    agent_id: str = Field(..., min_length=1)
    limit: int = Field(default=50, ge=1, le=200)
    since_ts: str | None = None


class ListBody(BaseModel):
    active_within_days: int = Field(default=7, ge=0)


class SubscribeBody(BaseModel):
    agent_id: str = Field(..., min_length=1)
    timeout_seconds: float = Field(default=30.0, ge=0.1, le=55.0)
    limit: int = Field(default=10, ge=1, le=100)


# -------------------------------------------------------
# Serialisation helpers
# -------------------------------------------------------

def _serialise_message(msg: Any) -> dict[str, Any]:
    """Convert a Message (Pydantic model) into a serialisable dict.

    Field mapping (mirrors what HttpBusStore._parse_message expects):
      sender_id → sender,  recipient_id → recipient,  created_at → sent_at
    """
    return {
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


def _serialise_agent(agent: Any) -> dict[str, Any]:
    """Convert an AgentRecord into a serialisable dict."""
    return {
        "agent_id": agent.agent_id,
        "first_seen_at": agent.first_seen_at.isoformat(),
        "last_seen_at": agent.last_seen_at.isoformat(),
        "online": agent.online,
        "metadata": agent.metadata,
    }


# -------------------------------------------------------
# Endpoint handlers
# -------------------------------------------------------

def _check_auth(request: Request, api_key: str | None) -> None:
    """Raise HTTP 401 if API key auth is enabled and missing/invalid.

    Uses ``hmac.compare_digest`` to prevent timing attacks.
    Expects a ``Bearer <key>`` Authorization header.
    """
    if api_key is None:
        return  # dev mode — no auth
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    if not hmac.compare_digest(token, api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")


def register_handler(
    store: Store, body: RegisterBody
) -> JSONResponse:
    store.upsert_agent(body.agent_id, body.metadata)
    return JSONResponse({"ok": True})


def send_handler(
    store: Store,
    waker: WebhookWaker | None,
    signal_dir: SignalDir | None,
    body: SendBody,
) -> JSONResponse:
    try:
        result = store.send_message(
            body.sender, body.recipient, body.body, body.metadata, body.intent
        )
    except ValueError as exc:
        err_str = str(exc)
        if "TARGET_SELF" in err_str:
            return JSONResponse(
                {"error": {"code": "TARGET_SELF", "message": err_str}},
                status_code=400,
            )
        if "TARGET_UNKNOWN" in err_str:
            return JSONResponse(
                {"error": {"code": "TARGET_UNKNOWN", "message": err_str}},
                status_code=400,
            )
        raise

    if waker is not None:
        try:
            waker.wake(body.recipient, body.sender)
        except Exception:  # pragma: no cover — defensive
            logger.warning("waker.wake(%s) failed (best-effort)", body.recipient, exc_info=True)
    if signal_dir:
        signal_dir.notify(body.recipient)

    return JSONResponse(result.model_dump(mode="json"))


def inbox_handler(
    store: Store, body: InboxBody
) -> JSONResponse:
    messages = store.read_inbox(
        body.agent_id, limit=body.limit, unread_only=body.unread_only
    )
    serialised = [_serialise_message(m) for m in messages]
    return JSONResponse({"messages": serialised})


def inbox_peek_handler(
    store: Store, body: InboxPeekBody
) -> JSONResponse:
    messages = store.peek_inbox(
        body.agent_id, limit=body.limit, since_ts=body.since_ts
    )
    serialised = [_serialise_message(m) for m in messages]
    return JSONResponse({"messages": serialised})


def list_handler(
    store: Store, body: ListBody
) -> JSONResponse:
    agents = store.list_agents(active_within_days=body.active_within_days)
    serialised = [_serialise_agent(a) for a in agents]
    return JSONResponse({"agents": serialised})


def subscribe_handler(
    store: Store,
    signal_dir: SignalDir | None,
    body: SubscribeBody,
) -> JSONResponse:
    try:
        messages, timed_out = store.subscribe(
            body.agent_id,
            timeout_seconds=body.timeout_seconds,
            limit=body.limit,
        )
    except RuntimeError as exc:
        return JSONResponse(
            {"error": {"code": "CONFIG_ERROR", "message": str(exc)}},
            status_code=500,
        )
    serialised = [_serialise_message(m) for m in messages]
    return JSONResponse({"messages": serialised, "timed_out": timed_out})


# -------------------------------------------------------
# Application factory
# -------------------------------------------------------

def create_app(
    *,
    db_path: str = ":memory:",
    api_key: str | None = None,
    signal_dir: SignalDir | None = None,
    waker: WebhookWaker | None = None,
) -> FastAPI:
    """Create the FastAPI application with the specified backend.

    This is the factory function used by both the CLI *serve-facade*
    action (production) and the test suite (testing).

    Routes are registered **without** a ``/bus`` prefix.  The base URL
    is the caller's responsibility: ``HttpBusStore`` passes
    ``http://host:port`` as ``base_url``, and a reverse-proxy can
    mount this app at ``/bus`` if desired.
    """
    store = Store(db_path, signal_dir=signal_dir, check_same_thread=False)
    try:
        store.init_schema()
    except Exception:
        store.close()
        raise

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[override]
        yield
        store.close()

    app = FastAPI(
        title="a2a-mcp-bridge Bus Façade",
        version=__version__,
        lifespan=lifespan,  # type: ignore[arg-type]
    )

    # Normalise Pydantic validation errors into our error envelope.
    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(  # type: ignore[override]
        request: Request, exc: RequestValidationError,
    ) -> JSONResponse:
        missing = [
            str(e["loc"][-1]) for e in exc.errors()
            if e["type"] in ("missing", "value_error", "string_too_short")
        ]
        msg = f"Missing or invalid fields: {', '.join(missing)}" if missing else str(exc)
        return JSONResponse(
            {"error": {"code": "VALIDATION_ERROR", "message": msg}},
            status_code=400,
        )

    @app.get("/health")
    async def health() -> JSONResponse:
        agent_count = len(store.list_agents())
        return JSONResponse({"status": "ok", "version": __version__, "agents": agent_count})

    @app.get("/ping")
    async def ping() -> JSONResponse:
        return JSONResponse({"server": "a2a-mcp-bridge", "version": __version__})

    @app.post("/register")
    async def register(request: Request, body: RegisterBody) -> JSONResponse:
        _check_auth(request, api_key)
        return register_handler(store, body)

    @app.post("/send")
    async def send(request: Request, body: SendBody) -> JSONResponse:
        _check_auth(request, api_key)
        return send_handler(store, waker, signal_dir, body)

    @app.post("/inbox")
    async def inbox(request: Request, body: InboxBody) -> JSONResponse:
        _check_auth(request, api_key)
        return inbox_handler(store, body)

    @app.post("/inbox_peek")
    async def inbox_peek(request: Request, body: InboxPeekBody) -> JSONResponse:
        _check_auth(request, api_key)
        return inbox_peek_handler(store, body)

    @app.post("/list")
    async def list_agents(request: Request, body: ListBody) -> JSONResponse:
        _check_auth(request, api_key)
        return list_handler(store, body)

    @app.post("/subscribe")
    async def subscribe(request: Request, body: SubscribeBody) -> JSONResponse:
        _check_auth(request, api_key)
        import anyio
        return await anyio.to_thread.run_sync(
            lambda: subscribe_handler(store, signal_dir, body)
        )

    return app
