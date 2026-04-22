"""Tests for the v0.4.4 webhook-based wake-up layer."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

from a2a_mcp_bridge.wake import (
    WakeEntry,
    WebhookWaker,
    _sign_body,
    load_registry,
)

# --------------------------------------------------------------------------- #
# load_registry: v0.4.4 webhook format
# --------------------------------------------------------------------------- #


class TestLoadRegistryWebhook:
    """v0.4.4+ format: wake_webhook_secret + agents with wake_webhook_url."""

    def test_loads_valid_v044_format(self, tmp_path: Path) -> None:
        p = tmp_path / "wake.json"
        p.write_text(
            json.dumps(
                {
                    "wake_webhook_secret": "abcd1234" * 8,  # 64 chars
                    "agents": {
                        "vlbeau-main": {
                            "wake_webhook_url": "http://127.0.0.1:8651/webhooks/wake"
                        },
                        "vlbeau-glm51": {
                            "wake_webhook_url": "http://127.0.0.1:8653/webhooks/wake"
                        },
                    },
                }
            )
        )
        secret, entries = load_registry(str(p))
        assert secret == "abcd1234" * 8
        assert len(entries) == 2
        assert entries["vlbeau-main"].wake_webhook_url == (
            "http://127.0.0.1:8651/webhooks/wake"
        )
        assert entries["vlbeau-glm51"].wake_webhook_url == (
            "http://127.0.0.1:8653/webhooks/wake"
        )

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        """A missing registry is valid: wake-up is opt-in."""
        secret, entries = load_registry(str(tmp_path / "nope.json"))
        assert secret is None
        assert entries == {}

    def test_malformed_json_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "wake.json"
        p.write_text("{ not valid json")
        with pytest.raises(ValueError, match="invalid JSON"):
            load_registry(str(p))

    def test_top_level_non_dict_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "wake.json"
        p.write_text(json.dumps(["array", "at", "top"]))
        with pytest.raises(ValueError, match="JSON object"):
            load_registry(str(p))

    def test_empty_object_returns_empty(self, tmp_path: Path) -> None:
        """An empty {} is a valid "no wake configured" state, not an error."""
        p = tmp_path / "wake.json"
        p.write_text("{}")
        secret, entries = load_registry(str(p))
        assert secret is None
        assert entries == {}

    def test_non_string_secret_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "wake.json"
        p.write_text(
            json.dumps({"wake_webhook_secret": 12345, "agents": {}})
        )
        with pytest.raises(ValueError, match="non-empty string"):
            load_registry(str(p))

    def test_empty_secret_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "wake.json"
        p.write_text(json.dumps({"wake_webhook_secret": "", "agents": {}}))
        with pytest.raises(ValueError, match="non-empty string"):
            load_registry(str(p))

    def test_missing_agents_when_secret_set_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "wake.json"
        p.write_text(json.dumps({"wake_webhook_secret": "secret"}))
        with pytest.raises(ValueError, match="'agents' must be an object"):
            load_registry(str(p))

    def test_entry_missing_url_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "wake.json"
        p.write_text(
            json.dumps(
                {
                    "wake_webhook_secret": "secret",
                    "agents": {"vlbeau-x": {"port": 8650}},  # no URL
                }
            )
        )
        with pytest.raises(ValueError, match="'wake_webhook_url'"):
            load_registry(str(p))

    def test_entry_bad_scheme_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "wake.json"
        p.write_text(
            json.dumps(
                {
                    "wake_webhook_secret": "secret",
                    "agents": {
                        "vlbeau-x": {"wake_webhook_url": "ws://127.0.0.1/wake"}
                    },
                }
            )
        )
        with pytest.raises(ValueError, match="http://"):
            load_registry(str(p))

    def test_entry_oversized_url_raises(self, tmp_path: Path) -> None:
        huge = "http://" + ("x" * 3000)
        p = tmp_path / "wake.json"
        p.write_text(
            json.dumps(
                {
                    "wake_webhook_secret": "secret",
                    "agents": {"vlbeau-x": {"wake_webhook_url": huge}},
                }
            )
        )
        with pytest.raises(ValueError, match="longer than"):
            load_registry(str(p))

    def test_https_url_accepted(self, tmp_path: Path) -> None:
        """Non-loopback https webhooks are syntactically valid."""
        p = tmp_path / "wake.json"
        p.write_text(
            json.dumps(
                {
                    "wake_webhook_secret": "secret",
                    "agents": {
                        "vlbeau-x": {
                            "wake_webhook_url": "https://wake.example.com/webhooks/wake"
                        }
                    },
                }
            )
        )
        secret, entries = load_registry(str(p))
        assert secret == "secret"
        assert entries["vlbeau-x"].wake_webhook_url == (
            "https://wake.example.com/webhooks/wake"
        )


# --------------------------------------------------------------------------- #
# load_registry: legacy (pre-v0.4.4) formats → detected + disabled
# --------------------------------------------------------------------------- #


class TestLoadRegistryLegacyDetection:
    """Pre-v0.4.4 Telegram-based formats are refused with a WARNING.

    Wake-up is disabled (empty registry returned) until the operator
    regenerates with ``a2a-mcp-bridge wake-registry init`` under v0.4.4.
    """

    def test_v043_shared_bot_format_rejected_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        p = tmp_path / "wake.json"
        p.write_text(
            json.dumps(
                {
                    "wake_bot_token": "111:OLD",
                    "agents": {
                        "vlbeau-main": {
                            "chat_id": "-1001234567890",
                            "thread_id": 5,
                        }
                    },
                }
            )
        )
        with caplog.at_level("WARNING"):
            secret, entries = load_registry(str(p))
        assert secret is None
        assert entries == {}
        assert any(
            "legacy" in rec.message.lower()
            and "wake-up is disabled" in rec.message.lower()
            for rec in caplog.records
        )

    def test_v03_per_agent_token_format_rejected_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        p = tmp_path / "wake.json"
        p.write_text(
            json.dumps(
                {
                    "vlbeau-main": {"bot_token": "111:X", "chat_id": "1000"},
                    "vlbeau-glm51": {"bot_token": "222:Y", "chat_id": "2000"},
                }
            )
        )
        with caplog.at_level("WARNING"):
            secret, entries = load_registry(str(p))
        assert secret is None
        assert entries == {}
        assert any(
            "legacy" in rec.message.lower() for rec in caplog.records
        )

    def test_unrecognised_structure_raises(self, tmp_path: Path) -> None:
        """Random JSON that doesn't match any format raises ValueError."""
        p = tmp_path / "wake.json"
        p.write_text(json.dumps({"agents": {"foo": {"wrong": "shape"}}}))
        with pytest.raises(ValueError, match="unrecognised structure"):
            load_registry(str(p))


# --------------------------------------------------------------------------- #
# WebhookWaker core behavior
# --------------------------------------------------------------------------- #


SECRET = "test-secret-for-hmac-signing-x64x64x64x64x64x64x"


@pytest.fixture
def registry() -> dict[str, WakeEntry]:
    return {
        "vlbeau-main": WakeEntry(
            wake_webhook_url="http://127.0.0.1:8651/webhooks/wake"
        ),
        "vlbeau-glm51": WakeEntry(
            wake_webhook_url="http://127.0.0.1:8653/webhooks/wake"
        ),
    }


class TestWebhookWakerCore:
    """End-to-end behavior: POST HMAC-signed JSON, swallow errors, return bool."""

    def test_configured_property(
        self, registry: dict[str, WakeEntry]
    ) -> None:
        assert WebhookWaker(registry, shared_secret=SECRET).configured is True
        assert WebhookWaker(registry, shared_secret=None).configured is False
        assert WebhookWaker({}, shared_secret=SECRET).configured is False

    def test_has_and_len(self, registry: dict[str, WakeEntry]) -> None:
        waker = WebhookWaker(registry, shared_secret=SECRET)
        assert waker.has("vlbeau-main") is True
        assert waker.has("vlbeau-unknown") is False
        assert len(waker) == 2

    def test_wake_posts_to_configured_url(
        self, registry: dict[str, WakeEntry]
    ) -> None:
        waker = WebhookWaker(registry, shared_secret=SECRET)
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = 202
        with patch("a2a_mcp_bridge.wake.urlopen", return_value=mock_resp) as m:
            ok = waker.wake("vlbeau-main", sender_id="vlbeau-glm51")
        assert ok is True
        req = m.call_args[0][0]
        assert req.full_url == "http://127.0.0.1:8651/webhooks/wake"
        assert req.get_method() == "POST"

    def test_wake_sends_hmac_sha256_signature(
        self, registry: dict[str, WakeEntry]
    ) -> None:
        """The X-Webhook-Signature header must match HMAC(secret, body)."""
        waker = WebhookWaker(registry, shared_secret=SECRET)
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = 202
        with patch("a2a_mcp_bridge.wake.urlopen", return_value=mock_resp) as m:
            waker.wake("vlbeau-main", sender_id="vlbeau-glm51")
        req = m.call_args[0][0]
        body = req.data
        sig = req.headers["X-webhook-signature"]  # urllib lower-cases
        expected = hmac.new(
            SECRET.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        assert sig == expected

    def test_wake_posts_json_with_sender_and_target(
        self, registry: dict[str, WakeEntry]
    ) -> None:
        waker = WebhookWaker(registry, shared_secret=SECRET)
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = 202
        with patch("a2a_mcp_bridge.wake.urlopen", return_value=mock_resp) as m:
            waker.wake("vlbeau-main", sender_id="vlbeau-glm51")
        req = m.call_args[0][0]
        body = json.loads(req.data)
        assert body["sender"] == "vlbeau-glm51"
        assert body["target"] == "vlbeau-main"
        assert body["source"] == "a2a-mcp-bridge"
        assert req.headers["Content-type"] == "application/json"

    def test_wake_body_is_deterministic(
        self, registry: dict[str, WakeEntry]
    ) -> None:
        """Same (sender, target) pair must produce byte-identical payloads.

        Determinism matters because the HMAC signature depends on the body,
        and retries (by the client) should produce identical signatures —
        otherwise idempotency caches on the server wouldn't dedupe.
        """
        waker = WebhookWaker(registry, shared_secret=SECRET)
        captured: list[bytes] = []

        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = 202

        def fake_urlopen(req, timeout=None):  # type: ignore[no-untyped-def]
            captured.append(req.data)
            return mock_resp

        with patch("a2a_mcp_bridge.wake.urlopen", side_effect=fake_urlopen):
            waker.wake("vlbeau-main", sender_id="vlbeau-glm51")
            waker.wake("vlbeau-main", sender_id="vlbeau-glm51")
        assert captured[0] == captured[1]

    def test_wake_unknown_agent_no_http(
        self, registry: dict[str, WakeEntry]
    ) -> None:
        waker = WebhookWaker(registry, shared_secret=SECRET)
        with patch("a2a_mcp_bridge.wake.urlopen") as m:
            ok = waker.wake("vlbeau-nobody", sender_id="vlbeau-glm51")
        assert ok is False
        m.assert_not_called()

    def test_wake_without_secret_returns_false_no_http(
        self, registry: dict[str, WakeEntry]
    ) -> None:
        waker = WebhookWaker(registry, shared_secret=None)
        with patch("a2a_mcp_bridge.wake.urlopen") as m:
            ok = waker.wake("vlbeau-main", sender_id="vlbeau-glm51")
        assert ok is False
        m.assert_not_called()

    def test_wake_non_2xx_returns_false(
        self, registry: dict[str, WakeEntry]
    ) -> None:
        waker = WebhookWaker(registry, shared_secret=SECRET)
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = 500
        with patch("a2a_mcp_bridge.wake.urlopen", return_value=mock_resp):
            ok = waker.wake("vlbeau-main", sender_id="vlbeau-glm51")
        assert ok is False


class TestWebhookWakerErrorSwallowing:
    """The waker must never raise: all errors become ``False`` + logged."""

    @pytest.fixture
    def waker(self, registry: dict[str, WakeEntry]) -> WebhookWaker:
        return WebhookWaker(registry, shared_secret=SECRET)

    def test_http_error_returns_false(
        self, waker: WebhookWaker, caplog: pytest.LogCaptureFixture
    ) -> None:
        with patch(
            "a2a_mcp_bridge.wake.urlopen",
            side_effect=HTTPError(
                url="http://x", code=401, msg="Unauthorized", hdrs=None, fp=None
            ),
        ), caplog.at_level("WARNING"):
            ok = waker.wake("vlbeau-main", sender_id="vlbeau-glm51")
        assert ok is False
        assert any("HTTPError" in rec.message for rec in caplog.records)

    def test_network_error_returns_false(
        self, waker: WebhookWaker, caplog: pytest.LogCaptureFixture
    ) -> None:
        with patch(
            "a2a_mcp_bridge.wake.urlopen",
            side_effect=URLError("connection refused"),
        ), caplog.at_level("WARNING"):
            ok = waker.wake("vlbeau-main", sender_id="vlbeau-glm51")
        assert ok is False
        assert any("network error" in rec.message for rec in caplog.records)

    def test_unexpected_exception_returns_false(
        self, waker: WebhookWaker, caplog: pytest.LogCaptureFixture
    ) -> None:
        with patch(
            "a2a_mcp_bridge.wake.urlopen",
            side_effect=RuntimeError("boom"),
        ), caplog.at_level("WARNING"):
            ok = waker.wake("vlbeau-main", sender_id="vlbeau-glm51")
        assert ok is False
        assert any("unexpected error" in rec.message for rec in caplog.records)


class TestSelfWakeGuard:
    """An agent waking itself is silently skipped (no HTTP call)."""

    def test_self_wake_returns_false_no_http(
        self, registry: dict[str, WakeEntry]
    ) -> None:
        waker = WebhookWaker(registry, shared_secret=SECRET)
        with patch("a2a_mcp_bridge.wake.urlopen") as m:
            ok = waker.wake("vlbeau-main", sender_id="vlbeau-main")
        assert ok is False
        m.assert_not_called()


class TestSignBodyHelper:
    """``_sign_body`` is exposed for reuse; make sure it matches HMAC-SHA256 hex."""

    def test_matches_stdlib_hmac(self) -> None:
        body = b'{"hello": "world"}'
        sig = _sign_body(body, "secret")
        expected = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        assert sig == expected
