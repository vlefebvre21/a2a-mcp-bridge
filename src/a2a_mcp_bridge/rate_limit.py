"""Configurable rate limiter for the HTTP facade (audit recommendation).

Uses a sliding-window algorithm with per-key (IP) counters. No external
dependencies — pure stdlib.

Environment variables:

    A2A_RATE_LIMIT_GLOBAL   = 200   (req/min, 0 = disabled)
    A2A_RATE_LIMIT_SEND     = 60
    A2A_RATE_LIMIT_INBOX    = 120
    A2A_RATE_LIMIT_REGISTER = 10
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_MAX_HITS_KEYS = 10_000  # hard limit — GC triggered when dict grows past this
_PRUNE_INTERVAL_S = 60.0  # seconds between periodic stale-entry sweeps


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer, using default %d", name, raw, default)
        return default


@dataclass
class RateLimiter:
    """Sliding-window rate limiter keyed by an arbitrary string (usually IP).

    Args:
        rpm: max requests per minute. ``0`` disables rate limiting
            (``allow()`` always returns ``True``).

    The ``hits`` dict stores a list of ``time.monotonic()`` timestamps
    for each key. On every ``allow()`` call, timestamps older than 60 s
    are pruned from the window. A periodic stale-entry sweep runs every
    ``_PRUNE_INTERVAL_S`` (60 s) to garbage-collect entries that have
    no active timestamps left. When the dict grows past ``_MAX_HITS_KEYS``
    (10 000), an immediate full sweep is triggered to prevent unbounded
    memory growth from transient IPs.
    """

    rpm: int
    hits: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    _last_prune: float = field(default=-1.0, init=False, repr=False)

    @property
    def enabled(self) -> bool:
        return self.rpm > 0

    def allow(self, key: str) -> bool:
        """Return ``True`` if the request is allowed for ``key``.

        When *rpm* is 0 the limiter is a no-op (always returns ``True``).
        """
        if not self.enabled:
            return True

        now = time.monotonic()
        cutoff = now - 60.0

        # Prune expired timestamps and count the current window.
        window = [t for t in self.hits[key] if t > cutoff]
        self.hits[key] = window

        # Lazy init of _last_prune on the very first call — avoids a spurious
        # sweep at startup (when `now - 0.0` always exceeds the interval).
        if self._last_prune < 0:
            self._last_prune = now

        # Periodic stale-entry sweep. Triggers:
        # 1. Every _PRUNE_INTERVAL_S (60 s) — lightweight, catches slow leaks.
        # 2. When the dict exceeds _MAX_HITS_KEYS — aggressive, prevents OOM.
        # _last_prune is bumped in both cases so the two triggers don't fire
        # redundantly back-to-back.
        if (
            len(self.hits) > _MAX_HITS_KEYS
            or now - self._last_prune >= _PRUNE_INTERVAL_S
        ):
            self.prune_stale()
            self._last_prune = now

        if len(window) >= self.rpm:
            return False

        window.append(now)
        return True

    def prune_stale(self) -> int:
        """Remove entries whose timestamps have all expired.

        Only entries with non-empty timestamp lists are considered;
        entries with empty lists are transient (being processed in the
        current ``allow()`` call) and are skipped.

        Returns:
            Number of entries removed.
        """
        now = time.monotonic()
        cutoff = now - 60.0
        stale = [
            k for k, timestamps in self.hits.items()
            if timestamps and not any(t > cutoff for t in timestamps)
        ]
        for k in stale:
            del self.hits[k]
        if stale:
            logger.debug("prune_stale removed %d entries from %d", len(stale), len(self.hits) + len(stale))
        return len(stale)

    def cleanup(self) -> int:
        """Aggressive cleanup: remove ALL stale entries.

        Alias for :meth:`prune_stale`. Kept for backwards compatibility
        and for external callers that prefer the name.

        Returns:
            Number of entries removed.
        """
        return self.prune_stale()

    def reset(self, key: str) -> None:
        """Clear all hits for *key* (useful for testing)."""
        self.hits.pop(key, None)


# ---------------------------------------------------------------------------
# Route-keyed limiter collection used by the facade middleware
# ---------------------------------------------------------------------------


@dataclass
class FacadeRateLimiters:
    """Holds per-route :class:`RateLimiter` instances for the facade.

    The *global_* limiter is applied first (before the per-route one)
    so that an IP flooding any endpoint gets a 429 regardless.
    """

    global_: RateLimiter
    send: RateLimiter
    inbox: RateLimiter
    register: RateLimiter

    @staticmethod
    def disabled() -> FacadeRateLimiters:
        """Return a no-op instance (all RPMs set to 0).

        Useful for tests and development mode where rate limiting
        should be entirely bypassed without environment variables.
        """
        return FacadeRateLimiters(
            global_=RateLimiter(0),
            send=RateLimiter(0),
            inbox=RateLimiter(0),
            register=RateLimiter(0),
        )

    def for_route(self, route: str) -> RateLimiter | None:
        """Return the per-route limiter or ``None`` for unknown routes.

        Trailing slashes are stripped for robustness.
        """
        route = route.rstrip("/")
        if route == "/send":
            return self.send
        if route in ("/inbox", "/inbox_peek"):
            return self.inbox
        if route == "/register":
            return self.register
        return None

    def prune_stale(self) -> int:
        """Sweep stale entries across all sub-limiters.

        Returns:
            Total number of entries removed.
        """
        total = 0
        total += self.global_.prune_stale()
        total += self.send.prune_stale()
        total += self.inbox.prune_stale()
        total += self.register.prune_stale()
        if total:
            logger.debug("FacadeRateLimiters.prune_stale removed %d entries total", total)
        return total


def build_limiters() -> FacadeRateLimiters:
    """Build limiter instances from environment config.

    Environment variables are read on every call so that tests can
    set them via ``monkeypatch.setenv`` after import time.
    """
    return FacadeRateLimiters(
        global_=RateLimiter(_env_int("A2A_RATE_LIMIT_GLOBAL", 200)),
        send=RateLimiter(_env_int("A2A_RATE_LIMIT_SEND", 60)),
        inbox=RateLimiter(_env_int("A2A_RATE_LIMIT_INBOX", 120)),
        register=RateLimiter(_env_int("A2A_RATE_LIMIT_REGISTER", 10)),
    )


# ---------------------------------------------------------------------------
# FastAPI middleware adapter
# ---------------------------------------------------------------------------

# The ``facade`` module imports this function and wraps it as a FastAPI
# HTTP middleware. It is kept here so unit tests can exercise the logic
# without launching a full ASGI app.

# Routes exempt from rate limiting — health checks and monitoring
# should never be blocked even when an IP is flooding.
_EXEMPT_ROUTES: frozenset[str] = frozenset({"/health", "/ping"})


def _should_skip_rate_limit(route: str) -> bool:
    """Return ``True`` if *route* should bypass rate limiting.

    Currently exempts ``/health`` and ``/ping`` so that monitoring
    tooling never sees a 429. Trailing slashes are stripped before
    matching (``/health/`` is treated as ``/health``).
    """
    return route.rstrip("/") in _EXEMPT_ROUTES


def ratelimit_middleware_factory(
    limiters: FacadeRateLimiters,
    get_client_ip: Any = None,
) -> Any:
    """Return an ASGI middleware callable that enforces rate limits.

    Args:
        limiters: :class:`FacadeRateLimiters` built from env config.
        get_client_ip: callable receiving a Starlette ``Request`` and
            returning the client IP. If ``None``, defaults to reading
            ``request.client.host``. The parameter is exposed so tests
            can inject a fake IP extractor.

    Returns:
        An ASGI middleware callable compatible with Starlette/FastAPI
        ``app.add_middleware(..., dispatch=...)`` patterns.

    Usage in ``facade.py``::

        from .rate_limit import build_limiters, ratelimit_middleware_factory
        limiters = build_limiters()
        app.add_middleware(BaseHTTPMiddleware, dispatch=ratelimit_middleware_factory(limiters))
    """
    if get_client_ip is None:
        def _get_ip(request: Any) -> str:
            client = getattr(request, "client", None)
            if client is not None:
                return str(client.host)
            return str(getattr(request, "headers", {}).get("x-forwarded-for", "127.0.0.1"))

        get_client_ip = _get_ip

    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    async def dispatch(request: Request, call_next: RequestResponseEndpoint) -> Any:
        ip = get_client_ip(request)
        route = request.url.path

        # Health and monitoring endpoints are never rate-limited.
        if not _should_skip_rate_limit(route):

            # Global limit first — applies to every request.
            if not limiters.global_.allow(ip):
                return JSONResponse(
                    {"error": {"code": "RATE_LIMITED", "message": "Too many requests"}},
                    status_code=429,
                    headers={"Retry-After": "60"},
                )

            # Per-route limit (if configured).
            route_limiter = limiters.for_route(route)
            if route_limiter is not None and not route_limiter.allow(ip):
                return JSONResponse(
                    {"error": {"code": "RATE_LIMITED", "message": "Too many requests"}},
                    status_code=429,
                    headers={"Retry-After": "60"},
                )

        return await call_next(request)

    return dispatch
