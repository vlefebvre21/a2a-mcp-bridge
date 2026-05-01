"""Tests for rate_limit.py."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from a2a_mcp_bridge.rate_limit import (
    FacadeRateLimiters,
    RateLimiter,
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

        # Mock time.monotonic to control the sliding window.
        now = 1000.0
        monkeypatch.setattr(time, "monotonic", lambda: now)

        # Fill up the window.
        rl.allow("k")
        monkeypatch.setattr(time, "monotonic", lambda: now + 1)
        rl.allow("k")
        monkeypatch.setattr(time, "monotonic", lambda: now + 2)
        assert rl.allow("k") is False  # still full

        # Jump 61 seconds forward — first two timestamps expire.
        monkeypatch.setattr(time, "monotonic", lambda: now + 61)
        assert rl.allow("k") is True  # window opened
        assert rl.allow("k") is True  # second slot
        assert rl.allow("k") is False  # full again

    def test_multiple_keys_independent(self) -> None:
        rl = RateLimiter(rpm=1)
        assert rl.allow("ip1") is True
        assert rl.allow("ip1") is False  # ip1 blocked
        assert rl.allow("ip2") is True   # ip2 unaffected
        assert rl.allow("ip2") is False

    def test_enabled_property(self) -> None:
        assert RateLimiter(rpm=0).enabled is False
        assert RateLimiter(rpm=1).enabled is True


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
# FacadeRateLimiters.for_route tests
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
        # Unknown routes return None
        assert limiters.for_route("/health") is None
        assert limiters.for_route("/ping") is None
        assert limiters.for_route("/subscribe") is None


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
        # Middleware calls call_next and returns its result when allowed.
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

        req = self._make_request("10.0.0.1", "/ping")
        call_next = AsyncMock()

        # First 2 requests pass.
        await dispatch(req, call_next)
        await dispatch(req, call_next)

        # Third request blocked by global limit.
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

        # First 2 /send requests pass.
        await dispatch(req, call_next)
        await dispatch(req, call_next)

        # Third /send request blocked by per-route limit.
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

        # IP A fills global quota.
        await dispatch(req_a, call_next)
        # IP A blocked.
        resp = await dispatch(req_a, call_next)
        assert resp.status_code == 429

        # IP B still allowed (different key).
        resp_b = await dispatch(req_b, call_next)
        assert resp_b.status_code != 429
