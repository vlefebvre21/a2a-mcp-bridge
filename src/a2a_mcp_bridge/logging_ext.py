"""Session-tagged logging helpers for MCP tool handlers (ADR-001 §4 #3, v0.5).

Two output formats are supported and selected at import time via the
``A2A_LOG_JSON`` environment variable:

* ``A2A_LOG_JSON=1`` → one JSON object per line, suitable for log shippers.
* anything else (including unset) → classic plain-text lines that match the
  pre-v0.5 output so existing tailers don't break.

Log records always carry a minimum schema:

* ``ts``          — ISO-8601 UTC timestamp
* ``level``       — logging level name (INFO / WARNING / ERROR)
* ``event``       — short snake_case identifier (e.g. ``tool.agent_send``)
* ``agent_id``    — the bridge's own identity (caller of the MCP tool)

Additional optional fields when relevant:

* ``session_id``  — from the caller-provided ``session_id`` metadata on
  ``agent_send`` or the ``session_id`` parameter on read tools
* ``message_id``  — for ``agent_send`` responses
* ``target``      — recipient of ``agent_send``
* ``duration_ms`` — wall time of the tool call in milliseconds
* ``error_code``  — on error paths
* ``body_hash``   — blake2b(digest_size=8) of the message body, used as a
  traceable-but-non-PII fingerprint in logs. NEVER the raw body.

The plain-text format renders the key/value pairs after the logger's own
prefix, e.g.::

    2026-04-23 09:31:12 INFO a2a_mcp_bridge.tools: agent_send [session=abc123] target=vlbeau-main message_id=ff... duration_ms=8.4

Design decisions:

* We do not introduce a logging dependency (no ``structlog`` / ``orjson``) —
  ``json.dumps`` is enough and the record count is small (one line per tool
  call).
* The JSON flag is read at import time. Runtime changes would require a
  server restart, but so does any other ``A2A_*`` env var, so the asymmetry
  is acceptable.
* PII: bodies and caller-supplied metadata (other than ``session_id``,
  which is a routing id) are never emitted verbatim. Only ``body_hash``
  goes into logs, computed with ``blake2b(digest_size=8)``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

A2A_LOG_JSON: bool = os.environ.get("A2A_LOG_JSON", "").strip() in {"1", "true", "yes"}


def hash_body(body: str | bytes | None) -> str | None:
    """Short, collision-resistant digest of a message body for logs.

    Returns ``None`` for a missing body. Uses :func:`hashlib.blake2b` with
    ``digest_size=8`` (16-char hex output), per Opus' 2026-04-22 guidance:
    enough to trace a body across log lines, not enough to leak contents.
    """
    if body is None:
        return None
    if isinstance(body, str):
        body = body.encode("utf-8", errors="replace")
    return hashlib.blake2b(body, digest_size=8).hexdigest()


def log_event(
    logger: logging.Logger,
    *,
    event: str,
    agent_id: str,
    level: int = logging.INFO,
    session_id: str | None = None,
    **fields: Any,
) -> None:
    """Emit a structured event through ``logger``.

    Only includes non-``None`` optional fields in the record so plain-text
    lines stay compact. In JSON mode the full record (with the minimum
    schema plus the provided fields) is emitted as one line.
    """
    record: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(),
        "level": logging.getLevelName(level),
        "event": event,
        "agent_id": agent_id,
    }
    if session_id is not None:
        record["session_id"] = session_id
    for k, v in fields.items():
        if v is None:
            continue
        record[k] = v

    if A2A_LOG_JSON:
        logger.log(level, json.dumps(record, separators=(",", ":")))
        return

    # Plain-text format: "event [session=<id>] k=v k=v ..."
    parts: list[str] = [event]
    if session_id is not None:
        parts.append(f"[session={session_id}]")
    for k, v in fields.items():
        if v is None:
            continue
        parts.append(f"{k}={v}")
    logger.log(level, " ".join(parts))
