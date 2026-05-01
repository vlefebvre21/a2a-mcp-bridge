"""Integration tests for rate limiting against a real uvicorn server.

Verifies that the façade returns 429 + Retry-After when limits are
exceeded, using real HTTP requests (not mocked middleware).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import httpx
import pytest

_RATE_LIMIT_PORT = 18768  # high port to avoid collisions


@pytest.fixture(scope="module")
def facade_rate_limited() -> str:
    """Start uvicorn with A2A_RATE_LIMIT_SEND=3 for the test module."""
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "a2a_mcp_bridge.facade:create_app",
            "--host", "127.0.0.1",
            "--port", str(_RATE_LIMIT_PORT),
            "--factory",
        ],
        env={
            **os.environ,
            "PYTHONPATH": "src",
            "A2A_RATE_LIMIT_GLOBAL": "200",
            "A2A_RATE_LIMIT_SEND": "3",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    base_url = f"http://127.0.0.1:{_RATE_LIMIT_PORT}"
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{base_url}/health", timeout=1.0)
            if resp.status_code == 200:
                break
        except (httpx.ConnectError, httpx.ReadTimeout):
            pass
        time.sleep(0.15)
    else:
        proc.kill()
        _out, err = proc.communicate(timeout=5)
        pytest.fail(
            f"Rate-limited façade did not start on port {_RATE_LIMIT_PORT}:\n"
            f"stderr: {err.decode()}"
        )

    yield base_url

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


class TestRateLimitIntegration:
    """Real HTTP rate limiting against a live uvicorn server."""

    def test_send_limit_returns_429_with_retry_after(
        self, facade_rate_limited: str
    ) -> None:
        base = facade_rate_limited

        # Register agents so /send targets are valid.
        for agent_id in ("alice", "bob"):
            httpx.post(f"{base}/register", json={"agent_id": agent_id})

        payload = {"sender": "alice", "recipient": "bob", "body": "test"}

        # First 3 requests should pass (A2A_RATE_LIMIT_SEND=3).
        for _ in range(3):
            resp = httpx.post(f"{base}/send", json=payload)
            assert resp.status_code == 200, (
                f"Expected 200, got {resp.status_code}: {resp.json()}"
            )

        # 4th request should be blocked.
        resp = httpx.post(f"{base}/send", json=payload)
        assert resp.status_code == 429
        data = resp.json()
        assert data["error"]["code"] == "RATE_LIMITED"
        assert resp.headers.get("Retry-After") == "60"

        # 5th request also blocked.
        resp = httpx.post(f"{base}/send", json=payload)
        assert resp.status_code == 429

    def test_health_never_rate_limited(
        self, facade_rate_limited: str
    ) -> None:
        base = facade_rate_limited
        for _ in range(20):
            resp = httpx.get(f"{base}/health")
            assert resp.status_code == 200, (
                f"/health got {resp.status_code} — should never be rate-limited"
            )

    def test_ping_never_rate_limited(
        self, facade_rate_limited: str
    ) -> None:
        base = facade_rate_limited
        for _ in range(20):
            resp = httpx.get(f"{base}/ping")
            assert resp.status_code == 200, (
                f"/ping got {resp.status_code} — should never be rate-limited"
            )

    def test_health_trailing_slash_never_rate_limited(
        self, facade_rate_limited: str
    ) -> None:
        base = facade_rate_limited
        for _ in range(20):
            resp = httpx.get(f"{base}/health/", follow_redirects=True)
            assert resp.status_code == 200, (
                f"/health/ got {resp.status_code} — "
                f"should never be rate-limited"
            )

    def test_ping_trailing_slash_never_rate_limited(
        self, facade_rate_limited: str
    ) -> None:
        base = facade_rate_limited
        for _ in range(20):
            resp = httpx.get(f"{base}/ping/", follow_redirects=True)
            assert resp.status_code == 200, (
                f"/ping/ got {resp.status_code} — "
                f"should never be rate-limited"
            )


@pytest.fixture(scope="module")
def facade_register_limit() -> str:
    """Start uvicorn with a low register limit."""
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "a2a_mcp_bridge.facade:create_app",
            "--host", "127.0.0.1",
            "--port", str(_RATE_LIMIT_PORT + 2),
            "--factory",
        ],
        env={
            **os.environ,
            "PYTHONPATH": "src",
            "A2A_RATE_LIMIT_GLOBAL": "200",
            "A2A_RATE_LIMIT_REGISTER": "2",
            "A2A_RATE_LIMIT_SEND": "200",
            "A2A_RATE_LIMIT_INBOX": "200",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    base_url = f"http://127.0.0.1:{_RATE_LIMIT_PORT + 2}"
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{base_url}/health", timeout=1.0)
            if resp.status_code == 200:
                break
        except (httpx.ConnectError, httpx.ReadTimeout):
            pass
        time.sleep(0.15)
    else:
        proc.kill()
        _out, err = proc.communicate(timeout=5)
        pytest.fail(
            f"Register-limit façade did not start "
            f"on port {_RATE_LIMIT_PORT + 2}:\n"
            f"stderr: {err.decode()}"
        )

    yield base_url

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="module")
def facade_global_limit() -> str:
    """Start uvicorn with a low global limit."""
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "a2a_mcp_bridge.facade:create_app",
            "--host", "127.0.0.1",
            "--port", str(_RATE_LIMIT_PORT + 3),
            "--factory",
        ],
        env={
            **os.environ,
            "PYTHONPATH": "src",
            "A2A_RATE_LIMIT_GLOBAL": "4",
            "A2A_RATE_LIMIT_REGISTER": "200",
            "A2A_RATE_LIMIT_SEND": "200",
            "A2A_RATE_LIMIT_INBOX": "200",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    base_url = f"http://127.0.0.1:{_RATE_LIMIT_PORT + 3}"
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{base_url}/health", timeout=1.0)
            if resp.status_code == 200:
                break
        except (httpx.ConnectError, httpx.ReadTimeout):
            pass
        time.sleep(0.15)
    else:
        proc.kill()
        _out, err = proc.communicate(timeout=5)
        pytest.fail(
            f"Global-limit façade did not start "
            f"on port {_RATE_LIMIT_PORT + 3}:\n"
            f"stderr: {err.decode()}"
        )

    yield base_url

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


class TestRateLimitIntegrationRegister:
    """Per-route register rate limiting."""

    def test_register_limit_returns_429(
        self, facade_register_limit: str
    ) -> None:
        base = facade_register_limit
        payload = {"agent_id": "regtest"}

        # First 2 should pass (A2A_RATE_LIMIT_REGISTER=2).
        for _ in range(2):
            resp = httpx.post(f"{base}/register", json=payload)
            assert resp.status_code == 200, (
                f"Expected 200, got {resp.status_code}: {resp.json()}"
            )

        # 3rd should be blocked.
        resp = httpx.post(f"{base}/register", json=payload)
        assert resp.status_code == 429, (
            f"Expected 429, got {resp.status_code}: {resp.json()}"
        )
        data = resp.json()
        assert data["error"]["code"] == "RATE_LIMITED"


class TestRateLimitIntegrationGlobal:
    """Global rate limiting."""

    def test_global_limit_returns_429_on_list(
        self, facade_global_limit: str
    ) -> None:
        base = facade_global_limit
        payload = {"active_within_days": 7}

        # First 4 should pass (A2A_RATE_LIMIT_GLOBAL=4).
        for _ in range(4):
            resp = httpx.post(f"{base}/list", json=payload)
            assert resp.status_code == 200, (
                f"Expected 200, got {resp.status_code}: {resp.json()}"
            )

        # 5th should be blocked by global limit.
        resp = httpx.post(f"{base}/list", json=payload)
        assert resp.status_code == 429, (
            f"Expected 429, got {resp.status_code}: {resp.json()}"
        )
        data = resp.json()
        assert data["error"]["code"] == "RATE_LIMITED"
        assert resp.headers.get("Retry-After") == "60"
