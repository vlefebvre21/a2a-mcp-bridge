"""Tool implementations — thin adapters over Store (with v0.2 signals + v0.3 wake-up)."""

from __future__ import annotations

import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Any

from .bus_store import BusStore, HttpBusStore
from .intents import DEFAULT_INTENT, normalize_intent, wakes
from .logging_ext import hash_body, log_event
from .models import Message
from .signals import SignalDir
from .transfers import (
    delete_transfer,
    load_manifest,
    resolve_locator_path,
    stage_file,
)
from .wake import WebhookWaker

logger = logging.getLogger("a2a_mcp_bridge.tools")

# Default long-poll cap for agent_subscribe — keep below typical MCP client
# timeouts (60 s) so we always answer cleanly.
MAX_SUBSCRIBE_TIMEOUT_SECONDS: float = 55.0


def tool_agent_send(
    store: BusStore,
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
    store: BusStore,
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
    store: BusStore,
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
    store: BusStore,
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
    store: BusStore,
    caller_id: str,
    signal_dir: SignalDir | None = None,
    timeout_seconds: float = 30.0,
    poll_interval: float = 0.2,
    limit: int = 10,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Long-poll the caller's inbox until a message arrives or timeout expires.

    Delegates to ``store.subscribe()`` which abstracts the transport:
    local Store uses filesystem signals, HttpBusStore uses HTTP long-poll.

    The *signal_dir* parameter is retained for backward-compat with
    callers that still pass it, but is ignored — the store owns its
    signal mechanism.
    """
    start = time.perf_counter()
    store.upsert_agent(caller_id)

    try:
        messages, timed_out = store.subscribe(
            caller_id,
            timeout_seconds=timeout_seconds,
            limit=limit,
        )
    except RuntimeError:
        # Store without SignalDir — fall back to the legacy path.
        # This should not happen in production but keeps the function
        # robust during the migration.
        return {"messages": [], "timed_out": True}

    fast_path = timed_out is False and messages and len(messages) > 0
    log_event(
        logger,
        event="tool.agent_subscribe",
        agent_id=caller_id,
        session_id=session_id,
        timed_out=timed_out,
        fast_path=fast_path if timed_out is False else None,
        count=len(messages),
        duration_ms=round((time.perf_counter() - start) * 1000, 2),
    )
    return {
        "messages": [_serialize_message(m) for m in messages],
        "timed_out": timed_out,
    }


def _serialize_message(m: Message) -> dict[str, Any]:
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


def _facade_upload(
    bus_url: str,
    api_key: str,
    filepath: Path,
    sender: str,
    recipient: str,
    ttl_hours: int,
) -> dict[str, Any]:
    """Upload a file to the bus façade via POST /transfers/upload.

    Uses ``urllib.request`` (stdlib) to avoid a hard dependency on httpx
    for the *sender* side — only the HttpBusStore constructor requires
    httpx.  This keeps ``pip install a2a-mcp-bridge`` (without ``[remote]``)
    working for SqliteBusStore agents that only need to *upload*.

    Args:
        bus_url: Base URL of the façade (e.g. ``http://localhost:8080``).
        api_key: Bearer token for the ``Authorization`` header.
        filepath: Local file to upload.
        sender: Sender agent_id.
        recipient: Recipient agent_id.
        ttl_hours: Time-to-live in hours.

    Returns:
        Dict with keys ``transfer_id``, ``sha256``, ``size``,
        ``filename``, ``expires_at``, ``locator``.

    Raises:
        FileNotFoundError: Upload endpoint unreachable (404).
        ValueError: ``TRANSFER_TOO_LARGE``, ``TRANSFER_QUOTA_EXCEEDED``,
            or generic HTTP/network error with status code.
    """
    import json as _json
    import urllib.error
    import urllib.request
    import uuid

    url = f"{bus_url.rstrip('/')}/transfers/upload"
    boundary = uuid.uuid4().hex.encode()

    filename = filepath.name
    # Build multipart/form-data body manually (stdlib only).
    parts: list[bytes] = []

    def _add_field(name: str, value: str) -> None:
        parts.append(b"--" + boundary + b"\r\n")
        parts.append(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        )
        parts.append(value.encode() + b"\r\n")

    _add_field("sender", sender)
    _add_field("recipient", recipient)
    _add_field("ttl_hours", str(ttl_hours))

    # File part
    parts.append(b"--" + boundary + b"\r\n")
    parts.append(
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
    )
    parts.append(b"Content-Type: application/octet-stream\r\n\r\n")

    file_bytes = filepath.read_bytes()
    parts.append(file_bytes)
    parts.append(b"\r\n")
    parts.append(b"--" + boundary + b"--\r\n")

    body = b"".join(parts)
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary.decode()}",
            "Authorization": f"Bearer {api_key}",
        },
    )

    try:
        with urllib.request.urlopen(req) as resp:
            result: dict[str, Any] = _json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        if exc.code == 404:
            raise FileNotFoundError(f"upload endpoint not found: {url}") from exc
        if exc.code == 413:
            raise ValueError(f"TRANSFER_TOO_LARGE: {detail}") from exc
        if exc.code == 429:
            raise ValueError(f"TRANSFER_QUOTA_EXCEEDED: {detail}") from exc
        if exc.code == 400:
            raise ValueError(f"TRANSFER_BAD_REQUEST: {detail}") from exc
        raise ValueError(f"upload_transfer HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"upload_transfer network error: {exc.reason}") from exc

    # Augment with locator
    result["locator"] = {
        "scheme": "http",
        "url": f"{bus_url.rstrip('/')}/transfers/{result['transfer_id']}",
    }
    return result


class _FacadeDownloadResult:
    """Immutable result from :func:`_facade_download`."""

    __slots__ = ("filename", "path", "sha256", "verified")

    def __init__(
        self, path: Path, sha256: str, filename: str, verified: bool
    ) -> None:
        self.path = path
        self.sha256 = sha256
        self.filename = filename
        self.verified = verified


def _parse_content_disposition(header: str) -> str:
    """Extract filename from a Content-Disposition header value.

    Returns the filename if found, empty string otherwise.
    """
    for part in header.split(";"):
        part = part.strip()
        if part.startswith("filename="):
            return part.split("=", 1)[1].strip('"').strip()
    return ""


def _facade_download(
    url: str,
    api_key: str,
    dest_dir: str,
    verify: bool = True,
) -> _FacadeDownloadResult:
    """Download a transfer file from the bus façade.

    Uses ``urllib.request`` (stdlib) to avoid a hard httpx dependency.
    Streams the response to disk (no full-RAM buffer).

    Integrity check: when ``verify=True`` and the response includes an
    ``X-Transfer-SHA256`` header, the downloaded file's sha256 is
    compared against it.  A mismatch raises ``ValueError``.

    Args:
        url: Full URL to the transfer resource.
        api_key: Bearer token for the ``Authorization`` header.
        dest_dir: Directory to write the file into (created if needed).
        verify: If True, verify sha256 against ``X-Transfer-SHA256``
            header when present.

    Returns:
        A :class:`_FacadeDownloadResult` with ``path``, ``sha256``,
        ``filename``, and ``verified`` fields.

    Raises:
        FileNotFoundError: Transfer not found (HTTP 404).
        PermissionError: Access denied (HTTP 403).
        ValueError: Hash mismatch, bad request (400), rate-limited (429),
            or other HTTP/network error.
    """
    import shutil
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
    )

    try:
        resp = urllib.request.urlopen(req)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise FileNotFoundError(f"transfer not found: {url}") from exc
        if exc.code == 403:
            raise PermissionError(f"transfer access denied: {url}") from exc
        if exc.code == 400:
            detail = exc.read().decode(errors="replace")
            raise ValueError(f"TRANSFER_BAD_REQUEST: {detail}") from exc
        if exc.code == 429:
            detail = exc.read().decode(errors="replace")
            raise ValueError(f"TRANSFER_QUOTA_EXCEEDED: {detail}") from exc
        raise ValueError(f"download HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"download network error: {exc.reason}") from exc

    # Determine filename from Content-Disposition, fallback to URL slug.
    cd = resp.headers.get("Content-Disposition", "")
    filename = _parse_content_disposition(cd)
    if not filename:
        filename = url.rsplit("/", 1)[-1] if "/" in url else "download"

    # Expected sha256 from header (may be absent on older façades).
    expected_sha = resp.headers.get("X-Transfer-SHA256", "")

    dest_path = Path(dest_dir) / filename
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")

    # Stream download — avoid buffering the entire file in RAM.
    h = hashlib.sha256()
    try:
        with open(tmp_path, "wb") as fout:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                h.update(chunk)
                fout.write(chunk)
            fout.flush()
            os.fsync(fout.fileno())
        tmp_path.rename(dest_path)
    except BaseException:
        # Clean up temp file on any error (tempdir leak prevention).
        tmp_path.unlink(missing_ok=True)
        dest_path.unlink(missing_ok=True)
        # Clean up dest_dir if we created it (best-effort, ignore errors)
        shutil.rmtree(Path(dest_dir), ignore_errors=True)
        raise
    finally:
        resp.close()

    sha256_hex = h.hexdigest()
    verified = False

    if verify and expected_sha:
        if sha256_hex != expected_sha:
            dest_path.unlink(missing_ok=True)
            raise ValueError(
                f"SHA-256 mismatch for transfer: expected {expected_sha}, got {sha256_hex}"
            )
        verified = True

    return _FacadeDownloadResult(
        path=dest_path,
        sha256=sha256_hex,
        filename=filename,
        verified=verified,
    )


def tool_agent_send_file(
    store: BusStore,
    caller_id: str,
    target: str,
    file_path: str,
    description: str = "",
    expires_in: int | None = None,
    signal_dir: SignalDir | None = None,
    waker: WebhookWaker | None = None,
    intent: str | None = None,
) -> dict[str, Any]:
    """Stage a local file and send its reference to *target* via A2A.

    Wire protocol: ADR-007 §4.1. The file content never enters either
    LLM's context window — only the reference message does.

    Dispatch priority (v0.7.2+):

    1. If ``A2A_BUS_URL`` is set in the environment → upload via the
       façade HTTP endpoint (``_facade_upload``), regardless of whether
       *store* is ``Store`` or ``HttpBusStore``.  This fixes the bug
       where VPS agents using ``Store`` (direct SQLite) would stage
       files locally — unreachable by remote (NAT'd) recipients.
    2. If *store* is ``HttpBusStore`` (no ``A2A_BUS_URL`` but the
       caller explicitly built an ``HttpBusStore``) → use its
       ``upload_transfer`` method (pre-v0.7.2 Phase C path).
    3. Otherwise → Phase A local ``stage_file`` (same-machine only).

    Errors returned as ``{"error": {"code": ..., "message": ...}}``:
        TRANSFER_SOURCE_NOT_FOUND, TRANSFER_TOO_LARGE,
        TRANSFER_QUOTA_EXCEEDED, <delegated from agent_send>.
    """
    import json as _json

    src = Path(file_path)
    filename = src.name

    bus_url = os.environ.get("A2A_BUS_URL", "").strip()

    if bus_url:
        # --- v0.7.2: façade upload when A2A_BUS_URL is set ---
        api_key = os.environ.get("A2A_FACADE_API_KEY", "")
        ttl_hours = (expires_in or 86400) / 3600
        try:
            upload = _facade_upload(
                bus_url=bus_url,
                api_key=api_key,
                filepath=src,
                sender=caller_id,
                recipient=target,
                ttl_hours=int(ttl_hours),
            )
        except FileNotFoundError:
            return {"error": {"code": "TRANSFER_SOURCE_NOT_FOUND", "message": str(src)}}
        except ValueError as e:
            code, _, msg = str(e).partition(":")
            return {"error": {"code": code.strip(), "message": msg.strip()}}

        body_obj = {
            "kind": "file_transfer",
            "version": 1,
            "transfer_id": upload["transfer_id"],
            "filename": upload["filename"],
            "size": upload["size"],
            "sha256": upload["sha256"],
            "description": description,
            "expires_at": _iso_utc(upload["expires_at"]),
            "locator": {"scheme": "http", "url": upload["locator"]["url"]},
        }

        send_result = tool_agent_send(
            store, caller_id, target, _json.dumps(body_obj),
            metadata=None,
            signal_dir=signal_dir,
            waker=waker,
            intent=intent,
        )
        if "error" in send_result:
            return {
                "error": send_result["error"],
                "transfer_id": upload["transfer_id"],
                "hint": "file uploaded but notification failed; caller may retry agent_send",
            }
        return {
            "transfer_id": upload["transfer_id"],
            "sha256": upload["sha256"],
            "size": upload["size"],
            "filename": upload["filename"],
            "expires_at": body_obj["expires_at"],
            "message_id": send_result.get("message_id"),
        }

    if isinstance(store, HttpBusStore):
        # --- Phase C: remote upload via HttpBusStore (pre-v0.7.2 path) ---
        try:
            upload = store.upload_transfer(
                file_path=file_path,
                sender_id=caller_id,
                recipient_id=target,
                description=description,
                expires_in=expires_in,
            )
        except FileNotFoundError:
            return {"error": {"code": "TRANSFER_SOURCE_NOT_FOUND", "message": str(src)}}
        except ValueError as e:
            code, _, msg = str(e).partition(":")
            return {"error": {"code": code.strip(), "message": msg.strip()}}

        body_obj = {
            "kind": "file_transfer",
            "version": 1,
            "transfer_id": upload["transfer_id"],
            "filename": upload["filename"],
            "size": upload["size"],
            "sha256": upload["sha256"],
            "description": description,
            "expires_at": _iso_utc(upload["expires_at"]),
            "locator": {"scheme": "http", "url": upload["locator"]["url"]},
        }

        send_result = tool_agent_send(
            store, caller_id, target, _json.dumps(body_obj),
            metadata=None,
            signal_dir=signal_dir,
            waker=waker,
            intent=intent,
        )
        if "error" in send_result:
            return {
                "error": send_result["error"],
                "transfer_id": upload["transfer_id"],
                "hint": "file uploaded but notification failed; caller may retry agent_send",
            }
        return {
            "transfer_id": upload["transfer_id"],
            "sha256": upload["sha256"],
            "size": upload["size"],
            "filename": upload["filename"],
            "expires_at": body_obj["expires_at"],
            "message_id": send_result.get("message_id"),
        }

    # --- Phase A: local stage_file ---
    try:
        rec = stage_file(
            src,
            sender_id=caller_id,
            recipient_id=target,
            filename=filename,
            description=description,
            expires_in=expires_in,
        )
    except FileNotFoundError:
        return {"error": {"code": "TRANSFER_SOURCE_NOT_FOUND", "message": str(src)}}
    except ValueError as e:
        code, _, msg = str(e).partition(":")
        return {"error": {"code": code.strip(), "message": msg.strip()}}

    # Build ADR-007 body. expires_at in manifest is epoch; serialise as ISO.
    manifest = load_manifest(rec.transfer_id)
    body_obj = {
        "kind": "file_transfer",
        "version": 1,
        "transfer_id": rec.transfer_id,
        "filename": rec.filename,
        "size": rec.size,
        "sha256": rec.sha256,
        "description": description,
        "expires_at": _iso_utc(manifest["expires_at"]),
        "locator": {"scheme": "file", "path": rec.locator_path},
    }

    send_result = tool_agent_send(
        store, caller_id, target, _json.dumps(body_obj),
        metadata=None,
        signal_dir=signal_dir,
        waker=waker,
        intent=intent,
    )
    if "error" in send_result:
        # The file is staged but the message failed — surface both.
        return {
            "error": send_result["error"],
            "transfer_id": rec.transfer_id,
            "hint": "file staged but notification failed; caller may retry agent_send",
        }
    return {
        "transfer_id": rec.transfer_id,
        "sha256": rec.sha256,
        "size": rec.size,
        "filename": rec.filename,
        "expires_at": body_obj["expires_at"],
        "message_id": send_result.get("message_id"),
    }


def tool_agent_fetch_file(
    store: BusStore,
    caller_id: str,
    transfer_id: str,
    verify: bool = True,
) -> dict[str, Any]:
    """Resolve *transfer_id* to a local path for the caller.

    The path is returned verbatim — the LLM tool that actually reads
    the bytes (e.g. ``read_file``) is invoked separately. Validates
    sha256 by default (cost ~50 ms per 100 MB).

    Dispatch priority (v0.7.2+):

    1. If ``A2A_BUS_URL`` is set in the environment → download via the
       façade HTTP endpoint (``_facade_download``), regardless of
       whether *store* is ``Store`` or ``HttpBusStore``.
    2. If *store* is ``HttpBusStore`` → use its ``download_transfer``
       method (pre-v0.7.2 Phase C path).
    3. Otherwise → Phase A local ``load_manifest`` + ``resolve_locator_path``.

    Phase C (remote): the manifest lives on the façade server — there
    is **no** local ``meta.json`` on the receiving host.

    Known limitation (Phase C): ``description`` and ``expires_at`` are
    returned as empty strings because the façade does not currently
    expose these fields via the download endpoint headers.  This is
    acceptable because the Hermes caller already has the full
    ADR-007 message body (which contains both fields) from the
    original ``agent_send_file`` notification.
    """
    import tempfile as _tf

    bus_url = os.environ.get("A2A_BUS_URL", "").strip()

    if bus_url:
        # --- v0.7.2: façade download when A2A_BUS_URL is set ---
        api_key = os.environ.get("A2A_FACADE_API_KEY", "")
        download_url = _rewrite_transfer_url(
            f"{bus_url.rstrip('/')}/transfers/{transfer_id}"
        )
        tmp_dir = _tf.mkdtemp(prefix="a2a_fetch_")

        try:
            dl = _facade_download(
                url=download_url,
                api_key=api_key,
                dest_dir=tmp_dir,
                verify=verify,
            )
        except FileNotFoundError:
            return {"error": {"code": "TRANSFER_NOT_FOUND", "message": transfer_id}}
        except PermissionError as e:
            return {"error": {"code": "TRANSFER_ACL_DENIED", "message": str(e)}}
        except ValueError as e:
            return {"error": {"code": "TRANSFER_HASH_MISMATCH", "message": str(e)}}

        file_stat = dl.path.stat()
        return {
            "transfer_id": transfer_id,
            "path": str(dl.path),
            "size": file_stat.st_size,
            "sha256": dl.sha256,
            "filename": dl.filename,
            "description": "",
            "expires_at": "",
        }

    if isinstance(store, HttpBusStore):
        # --- Phase C: remote download via HttpBusStore (pre-v0.7.2 path) ---
        tmp_dir = _tf.mkdtemp(prefix="a2a_fetch_")
        try:
            local_path = store.download_transfer(
                transfer_id, dest_dir=tmp_dir
            )
        except FileNotFoundError:
            return {"error": {"code": "TRANSFER_NOT_FOUND", "message": transfer_id}}
        except PermissionError as e:
            return {"error": {"code": "TRANSFER_ACL_DENIED", "message": str(e)}}
        except ValueError as e:
            # SHA-256 mismatch from download_transfer header check
            return {"error": {"code": "TRANSFER_HASH_MISMATCH", "message": str(e)}}

        # Build minimal metadata from the downloaded file itself.
        local_file = Path(local_path)
        file_stat = local_file.stat()
        dl_sha: str | None = None

        if verify:
            h = hashlib.sha256()
            with open(local_path, "rb") as f:
                for chunk in iter(lambda: f.read(64 * 1024), b""):
                    h.update(chunk)
            dl_sha = h.hexdigest()

        return {
            "transfer_id": transfer_id,
            "path": str(local_path),
            "size": file_stat.st_size,
            "sha256": dl_sha or "",
            "filename": local_file.name,
            "description": "",
            "expires_at": "",
        }

    # --- Phase A: local file scheme (manifest on disk) ---
    try:
        m = load_manifest(transfer_id)
    except FileNotFoundError:
        return {"error": {"code": "TRANSFER_NOT_FOUND", "message": transfer_id}}
    except PermissionError as e:
        return {"error": {"code": "TRANSFER_ACL_DENIED", "message": str(e)}}

    try:
        path = resolve_locator_path(transfer_id, caller_id=caller_id)
    except FileNotFoundError:
        return {"error": {"code": "TRANSFER_NOT_FOUND", "message": transfer_id}}
    except PermissionError as e:
        return {"error": {"code": "TRANSFER_ACL_DENIED", "message": str(e)}}

    if verify:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(64 * 1024), b""):
                h.update(chunk)
        if h.hexdigest() != m["sha256"]:
            return {"error": {"code": "TRANSFER_HASH_MISMATCH", "message": transfer_id}}

    return {
        "transfer_id": transfer_id,
        "path": str(path),
        "size": m["size"],
        "sha256": m["sha256"],
        "filename": m["filename"],
        "description": m.get("description", ""),
        "expires_at": _iso_utc(m["expires_at"]),
    }


def tool_agent_delete_file(
    store: BusStore,
    caller_id: str,
    transfer_id: str,
) -> dict[str, Any]:
    """Explicit deletion. Caller must be sender or recipient."""
    if isinstance(store, HttpBusStore):
        # --- Phase C: remote deletion via HttpBusStore ---
        try:
            return store.delete_transfer(transfer_id, caller_id=caller_id)
        except FileNotFoundError:
            return {"error": {"code": "TRANSFER_NOT_FOUND", "message": transfer_id}}
        except PermissionError as e:
            return {"error": {"code": "TRANSFER_ACL_DENIED", "message": str(e)}}

    # --- Phase A: local deletion ---
    try:
        delete_transfer(transfer_id, caller_id=caller_id)
    except FileNotFoundError:
        return {"error": {"code": "TRANSFER_NOT_FOUND", "message": transfer_id}}
    except PermissionError as e:
        return {"error": {"code": "TRANSFER_ACL_DENIED", "message": str(e)}}
    return {"deleted": True, "transfer_id": transfer_id}


def _rewrite_transfer_url(locator_url: str) -> str:
    """Rewrite an http(s) locator URL to use the local ``A2A_BUS_URL`` host.

    When the sender (VPS) embeds ``http://127.0.0.1:8080/transfers/abc``
    in the locator, a remote (NAT'd) recipient cannot reach it.  If
    ``A2A_BUS_URL`` is set, the scheme+netloc are replaced so the
    recipient downloads from the correct host.

    Non-http URLs (e.g. ``file:///...``) and cases where
    ``A2A_BUS_URL`` is unset are returned unchanged.
    """
    from urllib.parse import urlparse, urlunparse

    bus_url = os.environ.get("A2A_BUS_URL", "").strip()
    if not bus_url:
        return locator_url

    parsed_locator = urlparse(locator_url)
    if parsed_locator.scheme not in ("http", "https"):
        return locator_url

    parsed_bus = urlparse(bus_url)
    rewritten = parsed_locator._replace(
        scheme=parsed_bus.scheme or parsed_locator.scheme,
        netloc=parsed_bus.netloc or parsed_locator.netloc,
    )
    return urlunparse(rewritten)


def _iso_utc(epoch_or_iso: float | str) -> str:
    """Return ``epoch_or_iso`` as ISO-8601 Z string.

    Accepts either a numeric epoch (from local manifests / old mocks)
    or an ISO-8601 string (from the façade API).  Strings are
    normalised to Z-suffix; numeric values are converted.
    """
    from datetime import UTC, datetime

    if isinstance(epoch_or_iso, str):
        # Already ISO — just normalise +00:00 → Z
        return epoch_or_iso.replace("+00:00", "Z")
    return datetime.fromtimestamp(epoch_or_iso, UTC).isoformat().replace("+00:00", "Z")
