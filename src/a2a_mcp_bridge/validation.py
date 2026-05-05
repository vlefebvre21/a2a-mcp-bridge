"""Validation helpers for incoming MCP messages."""

from __future__ import annotations

import json
import re
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default maximum size for an incoming message body (1 MB).
# Configurable via A2A_MAX_MESSAGE_BYTES env var.
_DEFAULT_MAX_MESSAGE_BYTES = 1 * 1024 * 1024

# Re-compile the agent id pattern (shared with models.py).
_AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# MCP JSON-RPC envelope must contain these keys.
_RPC_REQUIRED_KEYS = frozenset({"jsonrpc", "method", "id"})
# Minimum viable MCP tool call structure.
_TOOL_CALL_REQUIRED_KEYS = frozenset({"id", "method", "params"})


def _max_message_bytes() -> int:
    """Return the configured max message size in bytes."""
    import os

    env = os.environ.get("A2A_MAX_MESSAGE_BYTES", "").strip()
    if env:
        try:
            val = int(env)
            if val > 0:
                return val
        except ValueError:
            pass
    return _DEFAULT_MAX_MESSAGE_BYTES


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_mcp_envelope(raw: str | bytes) -> dict[str, Any]:
    """Parse and validate a raw MCP JSON-RPC message.

    Raises :class:`MCPProtocolError` on malformed JSON, missing fields,
    or wrong versions.  Returns the parsed dict on success.
    """
    from .exceptions import MCPProtocolError, MessageTooLargeError

    if isinstance(raw, bytes):
        data_size = len(raw)
        text = raw.decode("utf-8", errors="replace")
    else:
        data_size = len(raw.encode("utf-8"))
        text = raw

    if data_size > _max_message_bytes():
        raise MessageTooLargeError(
            f"Incoming message is {data_size} bytes, "
            f"limit is {_max_message_bytes()} bytes "
            f"(configure with A2A_MAX_MESSAGE_BYTES)"
        )

    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MCPProtocolError(f"Invalid JSON: {exc}") from exc

    if not isinstance(obj, dict):
        raise MCPProtocolError("MCP message must be a JSON object")

    missing = _RPC_REQUIRED_KEYS - obj.keys()
    if missing:
        raise MCPProtocolError(f"Missing required JSON-RPC fields: {missing}")

    if obj.get("jsonrpc") != "2.0":
        raise MCPProtocolError(
            f"Unsupported JSON-RPC version: {obj.get('jsonrpc')!r}, expected '2.0'"
        )

    return obj


def validate_tool_params(
    tool: str, params: dict[str, Any] | None
) -> dict[str, Any]:
    """Validate and normalise parameters for a specific MCP tool call.

    Raises :class:`MCPValidationError` or :class:`MessageTooLargeError`
    on invalid input.  Returns the (possibly normalised) params dict.
    """
    from .exceptions import MCPValidationError

    if params is None:
        params = {}

    if not isinstance(params, dict):
        raise MCPValidationError(f"Tool params must be an object, got {type(params).__name__}")

    if tool == "agent_send":
        _validate_agent_send(params)
    elif tool == "agent_send_file":
        _validate_agent_send_file(params)
    elif tool == "agent_subscribe":
        _validate_agent_subscribe(params)
    elif tool == "agent_fetch_file":
        _validate_agent_fetch_file(params)
    elif tool == "agent_delete_file":
        _validate_agent_delete_file(params)

    return params


def _validate_agent_send(params: dict[str, Any]) -> None:
    """Validate agent_send parameters."""
    from .exceptions import MCPValidationError, MessageTooLargeError

    # Required fields
    target = params.get("target")
    message = params.get("message")

    if not isinstance(target, str) or not target:
        raise MCPValidationError("'target' must be a non-empty string")
    if not _AGENT_ID_RE.match(target):
        raise MCPValidationError(
            f"'target' must match ^[a-z0-9][a-z0-9_-]{{0,63}}$, got {target!r}"
        )

    if not isinstance(message, str):
        raise MCPValidationError("'message' must be a string")
    if len(message.encode("utf-8")) > 65_536:
        raise MCPValidationError("'message' exceeds 65536 bytes")

    # Optional metadata
    metadata = params.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict):
            raise MCPValidationError("'metadata' must be a dict or null")
        meta_bytes = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
        if len(meta_bytes) > 4_096:
            raise MessageTooLargeError(
                f"metadata is {len(meta_bytes)} bytes, limit is 4096"
            )
        sid = metadata.get("session_id")
        if sid is not None and not isinstance(sid, str):
            raise MCPValidationError("metadata.session_id must be a string or null")
        if isinstance(sid, str) and len(sid.encode("utf-8")) > 128:
            raise MCPValidationError("metadata.session_id exceeds 128 bytes")


def _validate_agent_send_file(params: dict[str, Any]) -> None:
    """Validate agent_send_file parameters."""
    from .exceptions import MCPValidationError

    target = params.get("target")
    file_path = params.get("file_path")

    if not isinstance(target, str) or not target:
        raise MCPValidationError("'target' must be a non-empty string")
    if not isinstance(file_path, str) or not file_path:
        raise MCPValidationError("'file_path' must be a non-empty string")


def _validate_agent_subscribe(params: dict[str, Any]) -> None:
    """Validate agent_subscribe parameters."""
    from .exceptions import MCPValidationError

    timeout = params.get("timeout_seconds", 30.0)
    if not isinstance(timeout, (int, float)):
        raise MCPValidationError("'timeout_seconds' must be a number")
    if timeout <= 0:
        raise MCPValidationError("'timeout_seconds' must be positive")
    if timeout > 55.0:
        raise MCPValidationError(
            f"'timeout_seconds' capped at 55 s, got {timeout}"
        )


def _validate_agent_fetch_file(params: dict[str, Any]) -> None:
    """Validate agent_fetch_file parameters."""
    from .exceptions import MCPValidationError

    transfer_id = params.get("transfer_id")
    if not isinstance(transfer_id, str) or not transfer_id:
        raise MCPValidationError("'transfer_id' must be a non-empty string")

    verify = params.get("verify", True)
    if not isinstance(verify, bool):
        raise MCPValidationError("'verify' must be a boolean")


def _validate_agent_delete_file(params: dict[str, Any]) -> None:
    """Validate agent_delete_file parameters."""
    from .exceptions import MCPValidationError

    transfer_id = params.get("transfer_id")
    if not isinstance(transfer_id, str) or not transfer_id:
        raise MCPValidationError("'transfer_id' must be a non-empty string")
