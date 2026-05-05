"""Custom exception classes for a2a-mcp-bridge and exception-migration helper.

When migrating from ``except Exception`` to specific types, import from
here rather than from the stdlib so the intent is clear.

    from .exceptions import JSONDecodeError, OperationalError

Usage after migration:
  from .exceptions import (
      A2ABridgeError, MCPConnectionError, MCPValidationError,
      MessageTooLargeError, MCPConfigError, MCPProtocolError,
  )
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Re-export stdlib exceptions that were previously caught as
# ``except Exception`` and should now be caught explicitly.
# ---------------------------------------------------------------------------
import json
import sqlite3

# Alias so callers import from a single location.
JSONDecodeError = json.JSONDecodeError
OperationalError = sqlite3.OperationalError
DatabaseError = sqlite3.DatabaseError


class A2ABridgeError(Exception):
    """Base exception for all a2a-mcp-bridge errors."""

    code: str = "A2A_BRIDGE_ERROR"


class MCPConnectionError(A2ABridgeError):
    """Raised when a network connection to an MCP or HTTP endpoint fails."""

    code = "CONNECTION_ERROR"


class MCPValidationError(A2ABridgeError):
    """Raised when an incoming MCP message fails schema or content validation."""

    code = "VALIDATION_ERROR"


class MessageTooLargeError(MCPValidationError):
    """Raised when a message exceeds the configured size limit."""

    code = "MESSAGE_TOO_LARGE"


class MCPConfigError(A2ABridgeError):
    """Raised when the bridge is misconfigured (missing env, bad paths, etc.)."""

    code = "CONFIG_ERROR"


class MCPProtocolError(MCPValidationError):
    """Raised on MCP protocol violations (bad JSON, missing required fields)."""

    code = "PROTOCOL_ERROR"
