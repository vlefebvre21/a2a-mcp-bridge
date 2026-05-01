"""HTTP façade server — exposes the a2a-mcp-bridge SQLite bus over REST.

This module defines :func:`create_app` (factory pattern) so the FastAPI
application can be instantiated with real or faked backend dependencies.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import shutil
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from . import __version__
from .rate_limit import FacadeRateLimiters, build_limiters, ratelimit_middleware_factory
from .signals import SignalDir
from .store import Store
from .transfer_store import TransferStore
from .transfers import _env_int, is_safe_path, resolve_transfer_dir
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
    limiters: FacadeRateLimiters | None = None,
) -> FastAPI:
    """Create the FastAPI application with the specified backend.

    This is the factory function used by both the CLI *serve-facade*
    action (production) and the test suite (testing).

    Routes are registered **without** a ``/bus`` prefix.  The base URL
    is the caller's responsibility: ``HttpBusStore`` passes
    ``http://host:port`` as ``base_url``, and a reverse-proxy can
    mount this app at ``/bus`` if desired.

    Args:
        limiters: optional :class:`FacadeRateLimiters` for per-IP rate
            limiting. When ``None`` (default), a fresh instance is built
            from environment variables via :func:`build_limiters`. Pass
            an explicit instance in tests to control limits.
    """
    if limiters is None:
        limiters = build_limiters()
    store = Store(db_path, signal_dir=signal_dir, check_same_thread=False)
    try:
        store.init_schema()
    except Exception:
        store.close()
        raise
    xfer_store = TransferStore(str(Path(db_path).parent / "transfers.db"), check_same_thread=False)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async def _transfer_sweep_loop():
            while True:
                await asyncio.sleep(300)  # 5 minutes
                expired = xfer_store.list_expired()
                for t in expired:
                    try:
                        xfer_store.delete(t["id"])
                        dir_path = resolve_transfer_dir() / t["id"]
                        if dir_path.is_dir() and is_safe_path(dir_path):
                            shutil.rmtree(dir_path, ignore_errors=True)
                    except Exception:
                        logger.warning("sweep: failed to delete %s", t["id"], exc_info=True)
                if expired:
                    logger.info("transfer_sweep removed %d expired transfer(s)", len(expired))

        sweep_task = asyncio.create_task(_transfer_sweep_loop())
        yield
        sweep_task.cancel()
        xfer_store.close()
        store.close()

    app = FastAPI(
        title="a2a-mcp-bridge Bus Façade",
        version=__version__,
        lifespan=lifespan,
    )

    # Rate limiting middleware — applied before auth so flooders get 429
    # regardless of credentials.
    app.add_middleware(
        BaseHTTPMiddleware,
        dispatch=ratelimit_middleware_factory(limiters),
    )

    # Normalise Pydantic validation errors into our error envelope.
    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
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

    # -------------------------------------------------------
    # File transfer endpoints
    # -------------------------------------------------------

    max_ttl_hours = _env_int("A2A_TRANSFER_MAX_TTL_HOURS", 168)
    max_pending = _env_int("A2A_TRANSFER_MAX_PENDING", 50)
    max_size_bytes = _env_int("A2A_TRANSFER_MAX_SIZE_MB", 100) * 1024 * 1024

    @app.post("/transfers/upload")
    async def transfer_upload(
        request: Request,
        file: UploadFile = File(...),
        sender: str = Form(...),
        recipient: str = Form(...),
        ttl_hours: int = Form(default=24),
    ) -> JSONResponse:
        _check_auth(request, api_key)

        if ttl_hours > max_ttl_hours:
            return JSONResponse(
                {"error": {"code": "TTL_EXCEEDED", "message": f"ttl_hours exceeds maximum ({max_ttl_hours})"}},
                status_code=400,
            )

        if xfer_store.count_pending(sender) >= max_pending:
            return JSONResponse(
                {"error": {"code": "TOO_MANY_PENDING", "message": f"sender {sender} has too many pending transfers"}},
                status_code=429,
            )

        transfer_id = str(uuid.uuid4())
        transfer_dir = resolve_transfer_dir() / transfer_id
        transfer_dir.mkdir(parents=True, exist_ok=True)

        tmp_path = transfer_dir / ".tmp"
        sha = hashlib.sha256()
        total_size = 0

        with open(tmp_path, "wb") as f:
            while True:
                chunk = await file.read(65536)  # 64KB
                if not chunk:
                    break
                total_size += len(chunk)
                if total_size > max_size_bytes:
                    # Clean up partial upload
                    tmp_path.unlink(missing_ok=True)
                    return JSONResponse(
                        {"error": {"code": "PAYLOAD_TOO_LARGE", "message": "file exceeds maximum allowed size"}},
                        status_code=413,
                    )
                sha.update(chunk)
                f.write(chunk)

        safe_filename = os.path.basename(file.filename or "upload")
        final_path = transfer_dir / safe_filename
        tmp_path.rename(final_path)
        os.chmod(final_path, 0o600)

        from datetime import datetime, timedelta, timezone

        expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)

        xfer_store.create(
            transfer_id=transfer_id,
            sender_id=sender,
            recipient_id=recipient,
            filename=safe_filename,
            size=total_size,
            sha256=sha.hexdigest(),
            expires_at=expires_at.isoformat(),
        )

        return JSONResponse({
            "transfer_id": transfer_id,
            "sha256": sha.hexdigest(),
            "size": total_size,
            "filename": safe_filename,
            "expires_at": expires_at.isoformat(),
        })

    @app.get("/transfers/{transfer_id}")
    async def transfer_download(request: Request, transfer_id: str) -> FileResponse:
        _check_auth(request, api_key)

        record = xfer_store.get(transfer_id)
        if record is None:
            return JSONResponse(
                {"error": {"code": "NOT_FOUND", "message": "transfer not found"}},
                status_code=404,
            )

        agent_id = request.headers.get("X-Agent-Id") or request.query_params.get("agent_id", "")
        if agent_id != record["recipient_id"]:
            return JSONResponse(
                {"error": {"code": "FORBIDDEN", "message": "agent_id is not the transfer recipient"}},
                status_code=403,
            )

        file_path = resolve_transfer_dir() / transfer_id / os.path.basename(record["filename"])
        if not file_path.is_file():
            return JSONResponse(
                {"error": {"code": "NOT_FOUND", "message": "staged file missing on disk"}},
                status_code=404,
            )

        xfer_store.mark_fetched(transfer_id)

        return FileResponse(
            file_path,
            headers={
                "X-Transfer-SHA256": record["sha256"],
                "Content-Disposition": f'attachment; filename="{record["filename"]}"',
            },
        )

    @app.delete("/transfers/{transfer_id}")
    async def transfer_delete(request: Request, transfer_id: str) -> Response:
        _check_auth(request, api_key)

        record = xfer_store.get(transfer_id)
        if record is None:
            return JSONResponse(
                {"error": {"code": "NOT_FOUND", "message": "transfer not found"}},
                status_code=404,
            )

        agent_id = request.headers.get("X-Agent-Id") or request.query_params.get("agent_id", "")
        if agent_id != record["sender_id"] and agent_id != record["recipient_id"]:
            return JSONResponse(
                {"error": {"code": "FORBIDDEN", "message": "agent_id is not sender or recipient"}},
                status_code=403,
            )

        xfer_store.delete(transfer_id)
        dir_path = resolve_transfer_dir() / transfer_id
        if dir_path.is_dir() and is_safe_path(dir_path):
            shutil.rmtree(dir_path, ignore_errors=True)

        return Response(status_code=204)

    return app
