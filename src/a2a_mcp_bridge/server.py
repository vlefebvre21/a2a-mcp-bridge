"""MCP stdio server exposing a2a tools."""

from __future__ import annotations

import functools
import logging
import os
import re
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel import NotificationOptions
from mcp.server.stdio import stdio_server

from .signals import SignalDir
from .store import Store
from .tools import (
    tool_agent_inbox,
    tool_agent_inbox_peek,
    tool_agent_list,
    tool_agent_send,
    tool_agent_subscribe,
)
from .wake import WebhookWaker, load_registry

logger = logging.getLogger("a2a_mcp_bridge")
AGENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
DEFAULT_SIGNAL_DIR = "/tmp/a2a-signals"  # advisory notification files
DEFAULT_WAKE_REGISTRY = "~/.a2a-wake-registry.json"


@functools.lru_cache(maxsize=1)
def _bridge_version() -> str:
    """Return the installed a2a-mcp-bridge version, or 'unknown' if undiscoverable.

    Cached with ``lru_cache(maxsize=1)``: the distribution metadata does not
    change within a server process's lifetime, and ``importlib.metadata.version``
    scans installed distributions on every call (~5-10 ms on a warm Python).
    Caching makes :func:`agent_ping` effectively free to spam and avoids paying
    the lookup twice at startup (log line + first tool call).
    """
    try:
        return _pkg_version("a2a-mcp-bridge")
    except PackageNotFoundError:  # pragma: no cover — only hit in editable dev without install
        return "unknown"


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


def _resolve_wake_registry_path() -> str:
    raw = os.environ.get("A2A_WAKE_REGISTRY", DEFAULT_WAKE_REGISTRY)
    return str(Path(raw).expanduser())


def _load_waker() -> WebhookWaker | None:
    """Load the wake-up registry, returning ``None`` if unavailable.

    Never raises: a missing or malformed registry logs a warning and disables
    wake-up instead of blocking server startup. A legacy (pre-v0.4.4)
    Telegram-based registry is detected upstream in ``load_registry`` and
    surfaces here as an empty registry, logging a migration WARNING.
    """
    path = _resolve_wake_registry_path()
    try:
        shared_secret, registry = load_registry(path)
    except ValueError as exc:
        logger.warning("wake registry %s is malformed, disabling wake-up: %s", path, exc)
        return None
    if not registry:
        return None
    logger.info(
        "wake registry loaded: %d agent(s) from %s (transport=webhook)",
        len(registry),
        path,
    )
    return WebhookWaker(registry, shared_secret=shared_secret)


class A2AMcp(FastMCP):
    """FastMCP subclass that advertises ``tools.listChanged`` capability.

    Why this matters (v0.4 — Option A, future-proof):
    ---------------------------------------------------
    The MCP spec defines ``notifications/tools/list_changed`` to let a server
    tell clients "my tool set changed, please re-fetch ``tools/list``". In this
    project the tool set is registered statically at import time, so we never
    actually emit this notification today. However, declaring the capability
    early has two benefits:

    1. MCP clients that observe the capability will subscribe to the
       notification stream, so future dynamic tool additions (e.g. plugins,
       per-session tool gating) become a drop-in change — no client-side
       restart required.
    2. The spec encourages servers to declare every capability they *might*
       use; this keeps our handshake honest.

    Note (documented caveat for Vincent's setup):
    This does **not** solve the "client keeps talking to an old stdio server
    after a version upgrade" issue. An upgraded binary only runs after the
    parent process (Hermes gateway) restarts its stdio child. Emitting
    ``list_changed`` from a new process reaches no one on the old channel.
    The proper mitigation for that scenario is the new ``agent_ping`` tool
    below, which lets a client query the server's running version and warn
    the operator about a stale child.

    .. warning::
        :meth:`run_stdio_async` below mirrors the upstream
        ``FastMCP.run_stdio_async`` implementation so we can pass a custom
        ``notification_options`` to ``create_initialization_options``. If the
        upstream ``mcp`` SDK adds lifecycle hooks (setup/teardown, shutdown
        handlers, transport middleware) or changes the ``stdio_server`` ctx
        manager signature in a future version, **this override will silently
        skip them**. Keep the override in sync with upstream and enforce the
        version ceiling via ``mcp>=1.0,<2`` in ``pyproject.toml``. A cleaner
        long-term fix would be to land a PR against the MCP SDK exposing
        ``notification_options`` as a ``FastMCP.__init__`` argument, after
        which this override can be deleted.
    """

    async def run_stdio_async(self) -> None:
        # Kept in sync with FastMCP.run_stdio_async (mcp>=1.0,<2). See class docstring.
        async with stdio_server() as (read_stream, write_stream):
            await self._mcp_server.run(
                read_stream,
                write_stream,
                self._mcp_server.create_initialization_options(
                    notification_options=NotificationOptions(tools_changed=True),
                ),
            )


def build_server(agent_id: str, db_path: str, signal_dir_path: str | None = None) -> FastMCP:
    store = Store(db_path)
    store.init_schema()
    store.upsert_agent(agent_id)

    signal_dir = SignalDir(signal_dir_path or _resolve_signal_dir())
    waker = _load_waker()

    mcp = A2AMcp("a2a-mcp-bridge")

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

        Side effect (v0.3): if `A2A_WAKE_REGISTRY` points at a valid registry
        and the recipient is listed, fires a Telegram prompt to their bot.
        """
        return tool_agent_send(store, agent_id, target, message, metadata, signal_dir, waker)

    @mcp.tool()
    def agent_inbox(
        limit: int = 10,
        unread_only: bool = True,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Read messages addressed to the calling agent.

        When unread_only=True (default), returned messages are atomically marked read.

        Args:
            limit: max messages to return.
            unread_only: if True (default), atomically mark the returned
                messages as read.
            session_id: optional opaque session correlator used by the
                caller (e.g. the Hermes gateway) to tag its own log lines.
                Plumbing metadata — not interpreted beyond log tagging.
                See ADR-001 §4 #3.
        """
        return tool_agent_inbox(
            store,
            agent_id,
            limit=limit,
            unread_only=unread_only,
            session_id=session_id,
        )

    @mcp.tool()
    def agent_inbox_peek(
        since_ts: str | None = None,
        limit: int = 50,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Read-only view of the caller's inbox — no mark-as-read.

        Unlike ``agent_inbox``, this tool never mutates ``read_at``. Use it
        when you want to inspect what's waiting (or what has been delivered)
        without consuming it — e.g. a gateway reconstructing its local cache
        after a restart, or tooling that wants a global view.

        Args:
            since_ts: ISO-8601 UTC timestamp. When provided, only messages
                with ``created_at >= since_ts`` are returned, sorted ASC by
                arrival time (replay order). When omitted, returns the
                ``limit`` most recent messages sorted newest-first.
            limit: max number of messages to return (clamped to [1, 200]).
            session_id: optional opaque session correlator for log tagging
                (ADR-001 §4 #3).

        Returns:
            ``{"messages": [...]}`` with the same payload shape as
            ``agent_inbox``. Already-read messages are included with their
            ``read_at`` populated.

        See ADR-001 §4 for the concurrency rationale (bridge-side primitive
        introduced in v0.5).
        """
        return tool_agent_inbox_peek(
            store,
            agent_id,
            since_ts=since_ts,
            limit=limit,
            session_id=session_id,
        )

    @mcp.tool()
    def agent_list(
        active_within_days: int = 7,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """List agents seen on the bus in the given window (default 7 days).

        Args:
            active_within_days: only return agents whose ``last_seen_at``
                is within this many days of now.
            session_id: optional opaque session correlator for log tagging
                (ADR-001 §4 #3).
        """
        return tool_agent_list(
            store,
            agent_id,
            active_within_days=active_within_days,
            session_id=session_id,
        )

    @mcp.tool()
    def agent_subscribe(
        timeout_seconds: float = 30.0,
        limit: int = 10,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Long-poll for new messages (v0.2 real-time delivery).

        Blocks up to ``timeout_seconds`` (capped at 55 s by the server) waiting
        for a new message to arrive for the calling agent. Returns immediately
        if messages are already pending. Payload shape matches ``agent_inbox``
        plus a ``timed_out`` boolean.

        Args:
            timeout_seconds: max seconds to block (capped at 55 s).
            limit: max messages to return when the wait resolves.
            session_id: optional opaque session correlator for log tagging
                (ADR-001 §4 #3).

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
            session_id=session_id,
        )

    @mcp.tool()
    def agent_ping() -> dict[str, Any]:
        """Return the bridge's running version and the caller's agent_id.

        Use this to detect a stale stdio child after an a2a-mcp-bridge upgrade.
        The MCP ``tools/list_changed`` notification cannot help in that case
        because the new binary only runs once the parent (Hermes gateway)
        restarts its child process — the client is still talking to the old
        server. Call ``agent_ping`` at session start and compare the returned
        ``version`` against the installed package version (or a known-good
        minimum) to decide whether to prompt the operator for a gateway
        restart.

        Returns:
            {"version", "agent_id", "server": "a2a-mcp-bridge"}
        """
        return {
            "server": "a2a-mcp-bridge",
            "version": _bridge_version(),
            "agent_id": agent_id,
        }

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
        "starting a2a-mcp-bridge agent_id=%s db=%s signals=%s version=%s",
        agent_id,
        db_path,
        signal_dir_path,
        _bridge_version(),
    )
    server = build_server(agent_id, db_path, signal_dir_path)
    server.run()


if __name__ == "__main__":
    main()
