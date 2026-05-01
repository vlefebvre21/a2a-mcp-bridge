"""Tests for rate_limit.py."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from a2a_mcp_bridge.rate_limit import (
    FacadeRateLimiters,
    RateLimiter,
    _should_skip_rate_limit,
    build_limiters,
    ratelimit_middleware_factory,
)

# ---------------------------------------------------------------------------
# RateLimiter unit tests
# ---------------------------------------------------------------------------


class TestRateLimiter:
    """Unit tests for the sliding-window RateLimiter."""

    def test_allow_when_below_limit(self) -> None:
        rl = RateLimiter(rpm=10)
        for _ in range(10):
            assert rl.allow("test-key") is True

    def test_reject_when_at_limit(self) -> None:
        rl = RateLimiter(rpm=3)
        assert rl.allow("k") is True
        assert rl.allow("k") is True
        assert rl.allow("k") is True
        assert rl.allow("k") is False  # 4th request blocked

    def test_disabled_with_zero_rpm(self) -> None:
        rl = RateLimiter(rpm=0)
        for _ in range(1000):
            assert rl.allow("k") is True

    def test_reset_clears_hits(self) -> None:
        rl = RateLimiter(rpm=2)
        rl.allow("x")
        rl.allow("x")
        assert rl.allow("x") is False

        rl.reset("x")
        assert rl.allow("x") is True  # fresh start

    def test_window_slides_with_expired_timestamps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rl = RateLimiter(rpm=2)

        now = 1000.0
        monkeypatch.setattr(time, "monotonic", lambda: now)

        rl.allow("k")
        monkeypatch.setattr(time, "monotonic", lambda: now + 1)
        rl.allow("k")
        monkeypatch.setattr(time, "monotonic", lambda: now + 2)
        assert rl.allow("k") is False  # still full

        monkeypatch.setattr(time, "monotonic", lambda: now + 61)
        assert rl.allow("k") is True
        assert rl.allow("k") is True
        assert rl.allow("k") is False

    def test_multiple_keys_independent(self) -> None:
        rl = RateLimiter(rpm=1)
        assert rl.allow("ip1") is True
        assert rl.allow("ip1") is False
        assert rl.allow("ip2") is True
        assert rl.allow("ip2") is False

    def test_enabled_property(self) -> None:
        assert RateLimiter(rpm=0).enabled is False
        assert RateLimiter(rpm=1).enabled is True

    def test_hits_dict_garbage_collection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the hits dict grows past _MAX_HITS_KEYS, stale entries are pruned."""
        # Use a very small limit by patching the module constant.
        from a2a_mcp_bridge import rate_limit as _rl

        monkeypatch.setattr(_rl, "_MAX_HITS_KEYS", 3)
        # Disable time-based GC so it doesn't interfere.
        monkeypatch.setattr(_rl, "_PRUNE_INTERVAL_S", 9999.0)

        rl = RateLimiter(rpm=100)
        # Fill 3 keys, then expire them by advancing time.
        now = 1000.0
        monkeypatch.setattr(time, "monotonic", lambda: now)
        rl.allow("a")
        rl.allow("b")
        rl.allow("c")

        # All 3 keys in dict.
        assert len(rl.hits) == 3

        # Advance past 60s so all timestamps expire.
        monkeypatch.setattr(time, "monotonic", lambda: now + 61)

        # This call triggers GC (len > 3).
        rl.allow("d")
        # "a", "b", "c" should be removed, only "d" remains.
        assert len(rl.hits) == 1
        assert "d" in rl.hits
        assert "a" not in rl.hits
        assert "b" not in rl.hits
        assert "c" not in rl.hits

    def test_prune_stale_removes_expired_entries(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """prune_stale() removes entries where all timestamps are expired."""
        rl = RateLimiter(rpm=100)
        now = 1000.0
        monkeypatch.setattr(time, "monotonic", lambda: now)
        rl.allow("a")
        rl.allow("b")
        assert len(rl.hits) == 2

        # Advance past 60s.
        monkeypatch.setattr(time, "monotonic", lambda: now + 61)
        removed = rl.prune_stale()
        assert removed == 2
        assert len(rl.hits) == 0

    def test_prune_stale_preserves_fresh_entries(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """prune_stale() keeps entries with active timestamps."""
        rl = RateLimiter(rpm=100)
        now = 1000.0
        monkeypatch.setattr(time, "monotonic", lambda: now)
        rl.allow("old")  # timestamp 1000.0
        monkeypatch.setattr(time, "monotonic", lambda: now + 30)
        rl.allow("fresh")  # timestamp 1030.0

        monkeypatch.setattr(time, "monotonic", lambda: now + 61)
        # cutoff = 1061 - 60 = 1001. "old" at 1000 is stale, "fresh" at 1030 is active.
        removed = rl.prune_stale()
        assert removed == 1
        assert "old" not in rl.hits
        assert "fresh" in rl.hits

    def test_cleanup_is_alias_for_prune_stale(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """cleanup() delegates to prune_stale()."""
        rl = RateLimiter(rpm=100)
        now = 1000.0
        monkeypatch.setattr(time, "monotonic", lambda: now)
        rl.allow("x")
        monkeypatch.setattr(time, "monotonic", lambda: now + 61)
        removed = rl.cleanup()
        assert removed == 1
        assert len(rl.hits) == 0

    def test_time_based_gc_triggers_periodically(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Periodic GC runs when _PRUNE_INTERVAL_S has elapsed."""
        from a2a_mcp_bridge import rate_limit as _rl

        monkeypatch.setattr(_rl, "_MAX_HITS_KEYS", 999_999)
        monkeypatch.setattr(_rl, "_PRUNE_INTERVAL_S", 20.0)

        rl = RateLimiter(rpm=100)
        now = 1000.0
        monkeypatch.setattr(time, "monotonic", lambda: now)
        rl.allow("stale-key")
        # After first allow(), time-based GC already ran (1000 - 0 >= 20),
        # so _last_prune should be set to now.
        assert rl._last_prune == 1000.0

        # Advance just 5s — not enough for time-based GC.
        monkeypatch.setattr(time, "monotonic", lambda: now + 5)
        rl.allow("another-key")
        assert rl._last_prune == 1000.0  # unchanged

        # Advance past 60s relative to stale-key (1000 → now+64 = 1064),
        # but keep another-key (1005) inside the 60s window (59s old).
        monkeypatch.setattr(time, "monotonic", lambda: now + 64)
        rl.allow("third-key")
        # Time-based GC should have triggered (64s elapsed since last prune).
        assert rl._last_prune == now + 64
        assert "stale-key" not in rl.hits
        assert "another-key" in rl.hits
        assert "third-key" in rl.hits


# ---------------------------------------------------------------------------
# build_limiters tests
# ---------------------------------------------------------------------------


class TestBuildLimiters:
    """Test that limiters are built from env with correct RPMs."""

    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("A2A_RATE_LIMIT_GLOBAL", "A2A_RATE_LIMIT_SEND", "A2A_RATE_LIMIT_INBOX", "A2A_RATE_LIMIT_REGISTER"):
            monkeypatch.delenv(var, raising=False)

        limiters = build_limiters()
        assert limiters.global_.rpm == 200
        assert limiters.send.rpm == 60
        assert limiters.inbox.rpm == 120
        assert limiters.register.rpm == 10

    def test_custom_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("A2A_RATE_LIMIT_GLOBAL", "50")
        monkeypatch.setenv("A2A_RATE_LIMIT_SEND", "10")
        monkeypatch.setenv("A2A_RATE_LIMIT_INBOX", "30")
        monkeypatch.setenv("A2A_RATE_LIMIT_REGISTER", "5")

        limiters = build_limiters()
        assert limiters.global_.rpm == 50
        assert limiters.send.rpm == 10
        assert limiters.inbox.rpm == 30
        assert limiters.register.rpm == 5


# ---------------------------------------------------------------------------
# FacadeRateLimiters tests
# ---------------------------------------------------------------------------


class TestFacadeRateLimiters:
    def test_for_route_returns_correct_limiter(self) -> None:
        limiters = FacadeRateLimiters(
            global_=RateLimiter(100),
            send=RateLimiter(10),
            inbox=RateLimiter(20),
            register=RateLimiter(5),
        )
        assert limiters.for_route("/send") is limiters.send
        assert limiters.for_route("/inbox") is limiters.inbox
        assert limiters.for_route("/inbox_peek") is limiters.inbox
        assert limiters.for_route("/register") is limiters.register
        assert limiters.for_route("/health") is None
        assert limiters.for_route("/ping") is None
        assert limiters.for_route("/subscribe") is None

    def test_for_route_strips_trailing_slash(self) -> None:
        """for_route handles routes with trailing slashes."""
        limiters = FacadeRateLimiters(
            global_=RateLimiter(100),
            send=RateLimiter(10),
            inbox=RateLimiter(20),
            register=RateLimiter(5),
        )
        assert limiters.for_route("/send/") is limiters.send
        assert limiters.for_route("/inbox/") is limiters.inbox
        assert limiters.for_route("/register/") is limiters.register
        # /health is not mapped by for_route, but it should still return None
        assert limiters.for_route("/health/") is None

    def test_disabled_factory_all_zero(self) -> None:
        dl = FacadeRateLimiters.disabled()
        assert dl.global_.rpm == 0
        assert dl.send.rpm == 0
        assert dl.inbox.rpm == 0
        assert dl.register.rpm == 0
        assert dl.global_.enabled is False

    def test_disabled_always_allows(self) -> None:
        dl = FacadeRateLimiters.disabled()
        for _ in range(100):
            assert dl.global_.allow("any") is True
            assert dl.send.allow("any") is True

    def test_prune_stale_delegates_to_all_limiters(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """FacadeRateLimiters.prune_stale() calls prune_stale on each sub-limiter."""
        import time as _time_module

        limiters = FacadeRateLimiters(
            global_=RateLimiter(100),
            send=RateLimiter(100),
            inbox=RateLimiter(100),
            register=RateLimiter(100),
        )
        now = 1000.0
        monkeypatch.setattr(_time_module, "monotonic", lambda: now)
        limiters.global_.allow("ip1")
        limiters.send.allow("ip2")
        limiters.inbox.allow("ip3")

        # Expire all timestamps.
        monkeypatch.setattr(_time_module, "monotonic", lambda: now + 61)
        total = limiters.prune_stale()
        assert total == 3
        assert len(limiters.global_.hits) == 0
        assert len(limiters.send.hits) == 0
        assert len(limiters.inbox.hits) == 0


# ---------------------------------------------------------------------------
# _should_skip_rate_limit tests
# ---------------------------------------------------------------------------


class TestShouldSkipRateLimit:
    def test_health_and_ping_exempted(self) -> None:
        assert _should_skip_rate_limit("/health") is True
        assert _should_skip_rate_limit("/ping") is True

    def test_other_routes_not_exempted(self) -> None:
        assert _should_skip_rate_limit("/send") is False
        assert _should_skip_rate_limit("/inbox") is False
        assert _should_skip_rate_limit("/register") is False
        assert _should_skip_rate_limit("/subscribe") is False
        assert _should_skip_rate_limit("/") is False

    def test_trailing_slash_exempted(self) -> None:
        """Trailing slashes on exempt routes are still exempted."""
        assert _should_skip_rate_limit("/health/") is True
        assert _should_skip_rate_limit("/ping/") is True

    def test_trailing_slash_not_exempted_for_other_routes(self) -> None:
        """Trailing slashes don't make non-exempt routes exempt."""
        assert _should_skip_rate_limit("/send/") is False
        assert _should_skip_rate_limit("/inbox/") is False


# ---------------------------------------------------------------------------
# Middleware integration tests
# ---------------------------------------------------------------------------


class TestRatelimitMiddleware:
    """Test the ASGI middleware dispatch function."""

    @staticmethod
    def _make_request(ip: str, path: str) -> MagicMock:
        req = MagicMock()
        req.client.host = ip
        req.url.path = path
        return req

    async def test_allow_when_under_limit(self) -> None:
        limiters = FacadeRateLimiters(
            global_=RateLimiter(10),
            send=RateLimiter(5),
            inbox=RateLimiter(10),
            register=RateLimiter(3),
        )
        dispatch = ratelimit_middleware_factory(limiters)

        req = self._make_request("192.168.1.1", "/send")
        call_next = AsyncMock()

        resp = await dispatch(req, call_next)
        assert call_next.called
        assert resp is call_next.return_value

    async def test_block_when_global_limit_exceeded(self) -> None:
        limiters = FacadeRateLimiters(
            global_=RateLimiter(2),
            send=RateLimiter(100),
            inbox=RateLimiter(100),
            register=RateLimiter(100),
        )
        dispatch = ratelimit_middleware_factory(limiters)

        # Use /subscribe — not exempted, has no per-route limiter (uses global only).
        req = self._make_request("10.0.0.1", "/subscribe")
        call_next = AsyncMock()

        await dispatch(req, call_next)
        await dispatch(req, call_next)

        resp = await dispatch(req, call_next)
        assert resp.status_code == 429
        data = json.loads(resp.body)
        assert data["error"]["code"] == "RATE_LIMITED"
        assert resp.headers["Retry-After"] == "60"

    async def test_block_when_route_limit_exceeded(self) -> None:
        limiters = FacadeRateLimiters(
            global_=RateLimiter(100),
            send=RateLimiter(2),
            inbox=RateLimiter(100),
            register=RateLimiter(100),
        )
        dispatch = ratelimit_middleware_factory(limiters)

        req = self._make_request("10.0.0.2", "/send")
        call_next = AsyncMock()

        await dispatch(req, call_next)
        await dispatch(req, call_next)

        resp = await dispatch(req, call_next)
        assert resp.status_code == 429

    async def test_different_ips_independent(self) -> None:
        limiters = FacadeRateLimiters(
            global_=RateLimiter(1),
            send=RateLimiter(100),
            inbox=RateLimiter(100),
            register=RateLimiter(100),
        )
        dispatch = ratelimit_middleware_factory(limiters)

        req_a = self._make_request("1.1.1.1", "/send")
        req_b = self._make_request("2.2.2.2", "/send")
        call_next = AsyncMock()

        await dispatch(req_a, call_next)
        resp = await dispatch(req_a, call_next)
        assert resp.status_code == 429

        resp_b = await dispatch(req_b, call_next)
        assert resp_b.status_code != 429

    async def test_health_never_blocked_by_global_limit(self) -> None:
        """Health/ping bypass rate limiting even when global limit is exceeded."""
        limiters = FacadeRateLimiters(
            global_=RateLimiter(1),
            send=RateLimiter(100),
            inbox=RateLimiter(100),
            register=RateLimiter(100),
        )
        dispatch = ratelimit_middleware_factory(limiters)
        call_next = AsyncMock()

        # Exhaust global limit with /send
        req_send = self._make_request("10.0.0.1", "/send")
        await dispatch(req_send, call_next)
        resp = await dispatch(req_send, call_next)
        assert resp.status_code == 429  # blocked

        # /health still passes
        req_health = self._make_request("10.0.0.1", "/health")
        resp_health = await dispatch(req_health, call_next)
        assert resp_health.status_code != 429

        # /ping still passes
        req_ping = self._make_request("10.0.0.1", "/ping")
        resp_ping = await dispatch(req_ping, call_next)
        assert resp_ping.status_code != 429

    async def test_health_with_trailing_slash_exempted(self) -> None:
        """/health/ (trailing slash) also bypasses rate limiting."""
        limiters = FacadeRateLimiters(
            global_=RateLimiter(1),
            send=RateLimiter(100),
            inbox=RateLimiter(100),
            register=RateLimiter(100),
        )
        dispatch = ratelimit_middleware_factory(limiters)
        call_next = AsyncMock()

        # Exhaust global limit
        req_send = self._make_request("10.0.0.1", "/send")
        await dispatch(req_send, call_next)
        resp = await dispatch(req_send, call_next)
        assert resp.status_code == 429

        # /health/ with trailing slash still passes
        req_health_slash = self._make_request("10.0.0.1", "/health/")
        resp_health = await dispatch(req_health_slash, call_next)
        assert resp_health.status_code != 429
