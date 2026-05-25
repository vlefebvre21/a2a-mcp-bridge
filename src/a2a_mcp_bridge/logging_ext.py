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

    2026-04-23 09:31:12 INFO a2a_mcp_bridge.tools: agent_send
    [session=abc123] target=vlbeau-main message_id=ff... duration_ms=8.4

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


def get_json_formatter() -> logging.Formatter:
    """Return a JSON formatter for structured logging (Datadog/ELK compatible).

    When the ``A2A_LOG_JSON=1`` environment variable is set, this formatter
    outputs one JSON object per line with the following schema:

    {
      "timestamp": "2026-05-25T19:30:00.123Z",
      "level": "INFO",
      "logger": "a2a_mcp_bridge.tools",
      "event": "tool.agent_send",
      "agent_id": "vlbeau-macqwen36",
      "session_id": "abc-123",  # optional
      ... additional fields ...
    }

    This is suitable for ingestion by Datadog, Logstash, or Splunk.
    """
    _STRUCTURED_FIELDS = (
        "event", "agent_id", "session_id", "message_id", "target",
        "duration_ms", "error_code", "body_hash", "count",
        "since_ts", "unread_only", "intent", "requested_intent",
        "effective_intent",
    )

    class StructuredFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            # Base dictionary
            log_data = {
                "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "level": record.levelname,
                "logger": record.name,
            }

            # Extract custom fields injected by log_event() or tool handlers
            for field in _STRUCTURED_FIELDS:
                val = getattr(record, field, None)
                if val is not None:
                    log_data[field] = val

            # Ajouter message si présent
            if record.msg:
                log_data["message"] = record.getMessage()

            # Thread ID pour debugging multi-session ADR-001
            log_data["thread_id"] = record.threadName or str(record.thread)

            try:
                return json.dumps(log_data, ensure_ascii=False)
            except Exception:
                # Fallback sécurisé en cas d'erreur JSON (très rare)
                return (
                f"{{'timestamp': '{log_data['timestamp']}', "
                f"'error': 'JSON serialization failed'}}"
            )

    return StructuredFormatter()


def setup_bridge_logger(
    name: str = "a2a_mcp_bridge",
    json_format: bool | None = None,
) -> logging.Logger:
    """Configure a logger with appropriate formatting.

    Args:
        name: Logger name (default "a2a_mcp_bridge")
        json_format: If True, force JSON output. If False, force text.
                    If None (default), respect A2A_LOG_JSON env var.

    Returns:
        Configured logging.Logger instance.
    """
    use_json = A2A_LOG_JSON if json_format is None else json_format

    logger = logging.getLogger(name)
    log_level = os.environ.get("A2A_LOG_LEVEL", "info").lower()
    logger.setLevel(logging.DEBUG if log_level == "debug" else logging.INFO)

    # Eviter double handler si déjà configuré
    if not logger.handlers:
        handler = logging.StreamHandler()
        if use_json:
            handler.setFormatter(get_json_formatter())
        else:
            # Format text classique enrichi
            handler.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z"
            ))
        logger.addHandler(handler)

    return logger
