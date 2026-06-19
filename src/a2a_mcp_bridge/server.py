"""MCP stdio server exposing a2a tools."""

from __future__ import annotations

import contextlib
import functools
import logging
import os
import re
import sys
import threading
import time
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel import NotificationOptions
from mcp.server.stdio import stdio_server

from .bus_store import BusStore
from .signals import SignalDir
from .store import Store
from .tools import (
    tool_agent_delete_file,
    tool_agent_fetch_file,
    tool_agent_inbox,
    tool_agent_inbox_peek,
    tool_agent_list,
    tool_agent_send,
    tool_agent_send_file,
    tool_agent_subscribe,
)
from .validation import validate_tool_params
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
    path = Path(raw).expanduser()
    abs_path = path.resolve()
    # Prevent path traversal outside the intended location.
    # A2A_DB_PATH should never contain '..' components that escape
    # a user-controlled directory.
    if ".." in raw.split("/"):
        print(
            f"error: A2A_DB_PATH={raw!r} contains path traversal, "
            "must be a safe absolute or ~-expanded path",
            file=sys.stderr,
        )
        sys.exit(2)
    return str(abs_path)


def _resolve_signal_dir() -> str:
    raw = os.environ.get("A2A_SIGNAL_DIR", DEFAULT_SIGNAL_DIR)
    path = Path(raw).expanduser()
    if ".." in raw.split("/"):
        print(
            f"error: A2A_SIGNAL_DIR={raw!r} contains path traversal",
            file=sys.stderr,
        )
        sys.exit(2)
    return str(path.resolve())


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


# Module-level cache for _load_waker_if_stale: (waker, mtime) tuple.
_waker_cache: tuple[WebhookWaker | None, float] | None = None


def _load_waker_if_stale() -> WebhookWaker | None:
    """Return a cached waker, reloading if the registry file's mtime changed.

    On the first call (or after :func:`_reset_waker_cache`), the registry is
    loaded unconditionally. On subsequent calls, the file's mtime is compared
    to the cached value. If the mtime differs, the waker is reloaded; otherwise
    the cached instance is returned.

    This allows long-running bridge processes to pick up registry changes
    (e.g. new agents added by the CLI) without restarting.
    """
    global _waker_cache
    path = _resolve_wake_registry_path()
    try:
        current_mtime = Path(path).stat().st_mtime
    except OSError:
        # File missing or unreadable: if we have a cached waker it's stale,
        # otherwise just load (which returns None for missing files).
        if _waker_cache is not None:
            logger.info("wake registry %s gone, clearing cache", path)
            _waker_cache = None
        return _load_waker()

    if _waker_cache is not None:
        cached_waker, cached_mtime = _waker_cache
        if cached_mtime == current_mtime:
            return cached_waker
        logger.info(
            "wake registry %s mtime changed (%s -> %s), reloading",
            path,
            cached_mtime,
            current_mtime,
        )

    waker = _load_waker()
    _waker_cache = (waker, current_mtime)
    return waker


def _reset_waker_cache() -> None:
    """Clear the module-level waker cache (for testing)."""
    global _waker_cache
    _waker_cache = None


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

    Note (documented caveat for the user's setup):
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

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._on_startup: list[Any] = []
        self._on_shutdown: list[Any] = []

    def on_startup(self, fn: Any) -> None:
        """Register an async callable to run when the server starts."""
        self._on_startup.append(fn)

    def on_shutdown(self, fn: Any) -> None:
        """Register an async callable to run when the server stops."""
        self._on_shutdown.append(fn)

    async def run_stdio_async(self) -> None:
        # Kept in sync with FastMCP.run_stdio_async (mcp>=1.0,<2). See class docstring.
        for fn in self._on_startup:
            await fn()
        try:
            async with stdio_server() as (read_stream, write_stream):
                await self._mcp_server.run(
                    read_stream,
                    write_stream,
                    self._mcp_server.create_initialization_options(
                        notification_options=NotificationOptions(tools_changed=True),
                    ),
                )
        finally:
            for fn in self._on_shutdown:
                await fn()


def _migrate_legacy_registry(store: Store, db_path: str) -> None:
    """Auto-migrate legacy .registry.db to bus.sqlite (ADR-008).

    Runs at bridge startup. If a legacy ``{db_path}.registry.db`` file
    exists, its ``capabilities`` rows are copied into the shared
    ``a2a-bus.sqlite`` via INSERT OR IGNORE (idempotent). The legacy
    file is then renamed to ``.registry.db.bak`` (not deleted, for safety).
    """
    legacy_path = Path(str(db_path).removesuffix(".sqlite") + ".registry.db")
    if not legacy_path.exists():
        return

    import sqlite3

    try:
        legacy_conn = sqlite3.connect(str(legacy_path))
        rows = legacy_conn.execute(
            "SELECT agent_id, skill_id, domain, description, "
            "monetary_cost_usd, tokens_per_call, announced_at "
            "FROM capabilities"
        ).fetchall()
        legacy_conn.close()
    except Exception as exc:
        logger.warning("Failed to read legacy registry.db: %s", exc)
        return

    if rows:
        for row in rows:
            with contextlib.suppress(Exception):  # INSERT OR IGNORE semantics
                store.register_capability(
                    agent_id=row[0],
                    skill_id=row[1],
                    domain=row[2] or "general",
                    description=row[3],
                    monetary_cost_usd=row[4],
                    tokens_per_call=row[5] or 0,
                )
        logger.info("Migrated %d capabilities from legacy registry.db", len(rows))

    # Rename legacy file (safer than delete)
    bak_path = legacy_path.with_suffix(".registry.db.bak")
    try:
        legacy_path.rename(bak_path)
        logger.info("Legacy registry.db renamed to %s", bak_path)
    except OSError as exc:
        logger.warning("Could not rename legacy registry.db: %s", exc)


def _start_transfer_sweep_thread() -> threading.Thread | None:
    """Start a daemon thread that periodically purges expired file transfers.

    Controlled by env vars:
      A2A_TRANSFER_SWEEP_ENABLED (default "1") — set to "0" to disable.
      A2A_TRANSFER_SWEEP_INTERVAL_SECONDS (default 300) — seconds between ticks.

    Returns the Thread if started, None if disabled.
    """
    enabled = os.environ.get("A2A_TRANSFER_SWEEP_ENABLED", "1").lower() in ("1", "true", "yes")
    if not enabled:
        return None

    try:
        interval = int(os.environ.get("A2A_TRANSFER_SWEEP_INTERVAL_SECONDS", "300"))
    except ValueError:
        interval = 300
    if interval < 1:
        interval = 300

    from .transfers import _transfer_sweep

    def _sweep_loop() -> None:
        while True:
            time.sleep(interval)
            try:
                removed = _transfer_sweep()
                if removed:
                    logger.debug("transfer_sweep_thread removed %d expired transfer(s)", removed)
            except Exception:
                logger.warning("transfer_sweep_thread error", exc_info=True)

    thread = threading.Thread(target=_sweep_loop, daemon=True, name="a2a-transfer-sweep")
    thread.start()
    logger.info("started transfer sweep thread (interval=%ds)", interval)
    return thread


def build_server(
    agent_id: str,
    db_path: str,
    signal_dir_path: str | None = None,
    bus_url: str | None = None,
    bus_api_key: str | None = None,
) -> FastMCP:
    if bus_url:
        # ADR-006 Step 1: remote bus via HTTP façade.
        # Import here to avoid hard dep on httpx at the top level.
        from .bus_store import HttpBusStore
        store: BusStore = HttpBusStore(bus_url, agent_id=agent_id, api_key=bus_api_key)
        signal_dir: SignalDir | None = None
    else:
        sd = SignalDir(signal_dir_path or _resolve_signal_dir())
        store = Store(db_path, signal_dir=sd)
        store.init_schema()
        signal_dir = sd
        # Warm the module-level waker cache so the first tool call doesn't pay
        # the registry-read cost. The return value is discarded — tool closures
        # below call _load_waker_if_stale() directly to pick up hot reloads.
        _load_waker_if_stale()
        _start_transfer_sweep_thread()
    store.upsert_agent(agent_id)

    # Capability Registry — centralized in a2a-bus.sqlite (ADR-008)
    # Legacy .registry.db auto-migrated on first boot if present.
    # Only applies to the local Store backend; HttpBusStore points to a
    # remote façade that owns its own registry state.
    if isinstance(store, Store):
        _migrate_legacy_registry(store, db_path)

    mcp = A2AMcp("a2a-mcp-bridge")

    @mcp.tool()
    def agent_send(
        target: str,
        message: str,
        metadata: dict[str, Any] | None = None,
        intent: str | None = None,
    ) -> dict[str, Any]:
        """Send a message to another agent on the bus.

        Note (multi-session): ``target`` identifies a **profile**, not a
        conversation. A profile may be served concurrently by multiple
        Hermes sessions behind a local gateway cache — see
        ``docs/adr/ADR-001-multi-session-concurrency.md``. If you need to
        correlate a reply with the exact sender session, pass
        ``metadata={"session_id": "<id>"}``.

        Args:
            target: recipient agent_id (lowercase, matches ^[a-z0-9][a-z0-9_-]{0,63}$).
            message: UTF-8 text body, max 65536 bytes.
            metadata: optional JSON-serialisable dict, max 4096 bytes serialised.
                A reserved key ``session_id`` (string, ≤ 128 bytes UTF-8) is
                hoisted into a dedicated column and surfaced in the inbox
                payload — see ADR-001 §4 #2.
            intent: optional wake-up intent (ADR-002). One of
                ``triage`` (default), ``execute``, ``review``, ``question``,
                ``fyi``. Controls the recipient wake-up behaviour:
                  * ``fyi`` — no webhook wake-up; recipient reads the message
                    at the next natural inbox poll. Use for notifications
                    that do not require an immediate reply.
                  * all others — standard webhook wake-up (current behaviour).
                Unknown values downgrade to ``triage`` with a WARNING log.

        Returns:
            ``{"message_id", "sent_at", "recipient", "intent"}`` on success
            (``intent`` is the normalised value actually stored), or
            ``{"error": {"code", "message"}}``.
            Validation errors on the reserved session_id key:
            ``SESSION_ID_INVALID`` (not a string) or ``SESSION_ID_TOO_LARGE``.

        Side effect (v0.2): writes a signal file to `A2A_SIGNAL_DIR` so that any
        agent long-polling via `agent_subscribe` wakes up immediately.

        Side effect (v0.4.4+): if `A2A_WAKE_REGISTRY` points at a valid v0.4.4
        webhook registry, fires an HMAC-signed wake-up POST to the recipient's
        local gateway endpoint. SKIPPED for ``intent=fyi`` (ADR-002).
        """
        validate_tool_params(
            tool="agent_send",
            params={"target": target, "message": message, "metadata": metadata, "intent": intent},
        )
        return tool_agent_send(
            store, agent_id, target, message, metadata, signal_dir, _load_waker_if_stale(),
            intent=intent,
        )

    @mcp.tool()
    def agent_inbox(
        limit: int = 10,
        unread_only: bool = True,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Read messages addressed to the calling agent.

        Note (multi-session): the caller's identity (``A2A_AGENT_ID``)
        identifies a **profile**. When ``unread_only=True`` the read is
        atomic mark-as-read — the message then becomes invisible to any
        sibling session of the same profile. In the v0.5 leader-at-gateway
        model this tool is expected to be called by the gateway only; other
        sessions should read their cache or use :func:`agent_inbox_peek`.
        See ``docs/adr/ADR-001-multi-session-concurrency.md``.

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
            signal_dir=signal_dir,
        )

    @mcp.tool()
    def agent_inbox_peek(
        since_ts: str | None = None,
        limit: int = 50,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Read-only view of the caller's inbox — no mark-as-read.

        Note (multi-session): the caller's identity identifies a **profile**
        (see ``docs/adr/ADR-001-multi-session-concurrency.md``). Because
        this tool never mutates ``read_at``, it is safe to call from any
        session of a profile concurrently — there is no consumption race
        with siblings. This is specifically what makes it suitable for the
        gateway's cache recovery path.

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

        Note (multi-session): each row describes a **profile** identity,
        not a live session. Liveness (a session actually running behind
        that profile) is not carried here — use ``agent_ping`` or send an
        actual message to confirm. See
        ``docs/adr/ADR-001-multi-session-concurrency.md``.

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

        Note (multi-session): the inbox consumption performed when the
        long-poll resolves is the same atomic mark-as-read as
        ``agent_inbox``. In the v0.5 leader-at-gateway model this should
        be called by the gateway only; sessions that want to react to
        sibling-cached deltas should use a profile-local cache. See
        ``docs/adr/ADR-001-multi-session-concurrency.md``.

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
        validate_tool_params(
            tool="agent_subscribe",
            params={"timeout_seconds": timeout_seconds, "limit": limit},
        )
        return tool_agent_subscribe(
            store,
            agent_id,
            signal_dir=signal_dir,  # passed for compat; store.subscribe() owns the mechanism
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

    @mcp.tool()
    def agent_send_file(
        target: str,
        file_path: str,
        description: str = "",
        expires_in: int | None = None,
        intent: str | None = None,
    ) -> dict[str, Any]:
        """Send a local file to *target* without loading it into LLM context.

        See ADR-007 for the full protocol. The file is staged under
        ``A2A_TRANSFER_DIR`` and a JSON reference is sent over the A2A
        message bus. Recipient fetches via :func:`agent_fetch_file`.

        Args:
            target: recipient agent_id.
            file_path: absolute path to a local file.
            description: optional human-readable note.
            expires_in: seconds until TTL expiry (default 86400, hard
                cap A2A_TRANSFER_MAX_TTL_SECONDS).
            intent: ADR-002 wake intent (triage / execute / review /
                question / fyi).

        Returns:
            ``{"transfer_id", "sha256", "size", "filename", "expires_at",
            "message_id"}`` on success, ``{"error": {"code", "message"}}``
            otherwise.
        """
        validate_tool_params(
            tool="agent_send_file",
            params={
                "target": target,
                "file_path": file_path,
                "description": description,
                "intent": intent,
            },
        )
        return tool_agent_send_file(
            store, agent_id, target, file_path,
            description=description, expires_in=expires_in,
            signal_dir=signal_dir, waker=_load_waker_if_stale(), intent=intent,
        )

    @mcp.tool()
    def agent_fetch_file(transfer_id: str, verify: bool = True) -> dict[str, Any]:
        """Resolve *transfer_id* to a local path for this agent.

        Caller must be the declared recipient (or sender). When
        ``verify=True`` (default), sha256 is re-checked — ~50 ms per
        100 MB, negligible vs. silent-corruption risk.
        """
        validate_tool_params(
            tool="agent_fetch_file",
            params={"transfer_id": transfer_id, "verify": verify},
        )
        return tool_agent_fetch_file(store, agent_id, transfer_id, verify=verify)

    @mcp.tool()
    def agent_delete_file(transfer_id: str) -> dict[str, Any]:
        """Delete a staged transfer. Caller must be sender or recipient."""
        validate_tool_params(
            tool="agent_delete_file",
            params={"transfer_id": transfer_id},
        )
        return tool_agent_delete_file(store, agent_id, transfer_id)

    # ── Capability Registry tools (ADR-008, centralized) ──────────────

    @mcp.tool()
    def capability_announce(
        agent_id: str,
        name: str,
        capabilities: list[dict[str, Any]] | None = None,
        status: str = "online",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Register or update an agent's capabilities in the registry.

        Args:
            agent_id: Unique agent identifier (lowercase, matches ^[a-z0-9][a-z0-9_-]{0,63}$).
            name: Human-readable display name for the agent.
            capabilities: List of capability objects, each with skill_id, description,
                domain, cost (with tokens_per_call, latency_ms, type), and optional fields.
            status: Agent status — "online", "offline", or "degraded".
            metadata: Optional arbitrary key/value metadata for the agent.
        """
        from pydantic import ValidationError

        from .exceptions import MCPValidationError
        from .models import AgentInfo

        validate_tool_params(
            tool="capability_announce",
            params={"agent_id": agent_id, "name": name},
        )
        agent_data: dict[str, Any] = {
            "agent_id": agent_id,
            "name": name,
            "capabilities": capabilities or [],
            "status": status,
            "metadata": metadata or {},
        }
        try:
            agent = AgentInfo.model_validate(agent_data)
        except ValidationError as exc:
            raise MCPValidationError(f"invalid capability data: {exc}") from exc

        store.upsert_agent(agent.agent_id, metadata=agent.metadata)
        for cap in agent.capabilities:
            cost_obj = getattr(cap, "cost", None)
            store.register_capability(
                agent_id=agent.agent_id,
                skill_id=cap.skill_id,
                domain=cap.domain or "general",
                description=cap.description,
                monetary_cost_usd=(
                    getattr(cost_obj, "monetary_cost_usd", None)
                    if cost_obj
                    else None
                ),
                tokens_per_call=getattr(cost_obj, "tokens_per_call", 0) if cost_obj else 0,
            )
        return {
            "status": "ok",
            "agent_id": agent.agent_id,
            "capabilities_registered": len(agent.capabilities),
        }

    @mcp.tool()
    def capability_discover() -> dict[str, Any]:
        """List all available capabilities across all registered agents."""
        results = store.get_capabilities()
        return {"capabilities": results, "count": len(results)}

    @mcp.tool()
    def capability_query(keyword: str = "", max_cost_usd: float | None = None) -> dict[str, Any]:
        """Query agents by keyword and/or cost ceiling.

        Args:
            keyword: Match against skill_id, description, or domain.
            max_cost_usd: Maximum monetary cost (USD) per call filter.
        """
        results = store.get_capabilities(keyword=keyword, max_cost_usd=max_cost_usd)
        return {"capabilities": results, "count": len(results)}

    @mcp.tool()
    def capability_find_best(skill: str, max_tokens: int | None = None) -> dict[str, Any]:
        """Find the best matching agent for a specific skill keyword.

        Args:
            skill: Keyword to match against skill_id or description.
            max_tokens: Optional token-cost ceiling for scoring.
        """
        results = store.get_capabilities(keyword=skill, max_tokens=max_tokens)
        return {
            "status": "success",
            "query": skill,
            "results": results[:10],
            "count": len(results),
        }

    @mcp.tool()
    def capability_ping(agent_id: str) -> dict[str, Any]:
        """Signal that an agent is still alive (heartbeat ping).

        Args:
            agent_id: The agent sending the heartbeat.
        """
        store.upsert_agent(agent_id)
        return {"status": "ok", "agent_id": agent_id}

    return mcp


def main(*, bus_url: str | None = None) -> None:
    logging.basicConfig(
        level=os.environ.get("A2A_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    agent_id = _resolve_agent_id()
    db_path = _resolve_db_path()
    signal_dir_path = _resolve_signal_dir()
    bus_api_key = os.environ.get("A2A_FACADE_API_KEY")
    logger.info(
        "starting a2a-mcp-bridge agent_id=%s db=%s signals=%s bus_url=%s version=%s",
        agent_id,
        db_path,
        signal_dir_path,
        bus_url or "(local)",
        _bridge_version(),
    )
    server = build_server(
        agent_id, db_path, signal_dir_path,
        bus_url=bus_url, bus_api_key=bus_api_key,
    )
    server.run()


if __name__ == "__main__":
    main()
