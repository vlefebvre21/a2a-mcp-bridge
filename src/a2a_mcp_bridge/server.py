"""MCP stdio server exposing a2a tools."""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .signals import SignalDir
from .store import Store
from .tools import (
    tool_agent_inbox,
    tool_agent_list,
    tool_agent_send,
    tool_agent_subscribe,
)

logger = logging.getLogger("a2a_mcp_bridge")
AGENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
DEFAULT_SIGNAL_DIR = "/tmp/a2a-signals"  # advisory notification files


def _resolve_agent_id() -> str:
    agent_id = os.environ.get("A2A_AGENT_ID", "").strip()
    if not agent_id:
        print(
            "error: A2A_AGENT_ID env var is required (see README).",
            file=sys.stderr,
        )
        sys.exit(2)
    if not AGENT_ID_PATTERN.match(agent_id):
        print(
            f"error: A2A_AGENT_ID={agent_id!r} invalid. Must match ^[a-z0-9][a-z0-9_-]{{0,63}}$",
            file=sys.stderr,
        )
        sys.exit(2)
    return agent_id


def _resolve_db_path() -> str:
    raw = os.environ.get("A2A_DB_PATH", "~/.a2a-bus.sqlite")
    return str(Path(raw).expanduser())


def _resolve_signal_dir() -> str:
    raw = os.environ.get("A2A_SIGNAL_DIR", DEFAULT_SIGNAL_DIR)
    return str(Path(raw).expanduser())


def build_server(agent_id: str, db_path: str, signal_dir_path: str | None = None) -> FastMCP:
    store = Store(db_path)
    store.init_schema()
    store.upsert_agent(agent_id)

    signal_dir = SignalDir(signal_dir_path or _resolve_signal_dir())

    mcp = FastMCP("a2a-mcp-bridge")

    @mcp.tool()
    def agent_send(
        target: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send a message to another agent on the bus.

        Args:
            target: recipient agent_id (lowercase, matches ^[a-z0-9][a-z0-9_-]{0,63}$).
            message: UTF-8 text body, max 65536 bytes.
            metadata: optional JSON-serialisable dict, max 4096 bytes serialised.

        Returns:
            {"message_id", "sent_at", "recipient"} on success, or {"error": {"code", "message"}}.

        Side effect (v0.2): writes a signal file to `A2A_SIGNAL_DIR` so that any
        agent long-polling via `agent_subscribe` wakes up immediately.
        """
        return tool_agent_send(store, agent_id, target, message, metadata, signal_dir)

    @mcp.tool()
    def agent_inbox(limit: int = 10, unread_only: bool = True) -> dict[str, Any]:
        """Read messages addressed to the calling agent.

        When unread_only=True (default), returned messages are atomically marked read.
        """
        return tool_agent_inbox(store, agent_id, limit=limit, unread_only=unread_only)

    @mcp.tool()
    def agent_list(active_within_days: int = 7) -> dict[str, Any]:
        """List agents seen on the bus in the given window (default 7 days)."""
        return tool_agent_list(store, agent_id, active_within_days=active_within_days)

    @mcp.tool()
    def agent_subscribe(
        timeout_seconds: float = 30.0,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Long-poll for new messages (v0.2 real-time delivery).

        Blocks up to ``timeout_seconds`` (capped at 55 s by the server) waiting
        for a new message to arrive for the calling agent. Returns immediately
        if messages are already pending. Payload shape matches ``agent_inbox``
        plus a ``timed_out`` boolean.

        Usage pattern for a continuously-listening agent::

            while True:
                r = agent_subscribe(timeout_seconds=30)
                for m in r["messages"]:
                    handle(m)
        """
        return tool_agent_subscribe(
            store,
            agent_id,
            signal_dir=signal_dir,
            timeout_seconds=timeout_seconds,
            limit=limit,
        )

    return mcp


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("A2A_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    agent_id = _resolve_agent_id()
    db_path = _resolve_db_path()
    signal_dir_path = _resolve_signal_dir()
    logger.info(
        "starting a2a-mcp-bridge agent_id=%s db=%s signals=%s",
        agent_id,
        db_path,
        signal_dir_path,
    )
    server = build_server(agent_id, db_path, signal_dir_path)
    server.run()


if __name__ == "__main__":
    main()
