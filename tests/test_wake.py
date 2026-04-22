"""Tests for the Telegram wake-up layer."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

from a2a_mcp_bridge.wake import TelegramWaker, WakeEntry, load_registry

# --------------------------------------------------------------------------- #
# load_registry — legacy format (per-agent bot_token)
# --------------------------------------------------------------------------- #


class TestLoadRegistryLegacy:
    """The v0.3 - v0.4.2 format: one bot_token per entry, no top-level keys."""

    def test_loads_valid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "reg.json"
        path.write_text(
            json.dumps(
                {
                    "vlbeau-main": {"bot_token": "abc:def", "chat_id": "123"},
                    "vlbeau-glm51": {"bot_token": "xyz:uvw", "chat_id": "456"},
                }
            )
        )
        shared, reg = load_registry(str(path))
        # Legacy format: no shared token is returned.
        assert shared is None
        assert reg["vlbeau-main"].bot_token == "abc:def"
        assert reg["vlbeau-main"].chat_id == "123"
        assert reg["vlbeau-glm51"].chat_id == "456"

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        shared, reg = load_registry(str(tmp_path / "nope.json"))
        assert shared is None
        assert reg == {}

    def test_malformed_json_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("not json at all")
        with pytest.raises(ValueError):
            load_registry(str(path))

    def test_entry_missing_fields_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"vlbeau-main": {"bot_token": "abc"}}))
        with pytest.raises(ValueError, match="chat_id"):
            load_registry(str(path))

    def test_entry_non_string_fields_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"vlbeau-main": {"bot_token": "abc", "chat_id": 123}}))
        with pytest.raises(ValueError):
            load_registry(str(path))

    def test_top_level_non_dict_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(["not", "a", "dict"]))
        with pytest.raises(ValueError):
            load_registry(str(path))

    # ----- v0.4.2: optional thread_id in legacy entries --------------------- #

    def test_loads_entry_with_thread_id(self, tmp_path: Path) -> None:
        path = tmp_path / "reg.json"
        path.write_text(
            json.dumps(
                {
                    "vlbeau-main": {
                        "bot_token": "T:TOKEN",
                        "chat_id": "-1001234567890",
                        "thread_id": 5,
                    },
                }
            )
        )
        _, reg = load_registry(str(path))
        assert reg["vlbeau-main"].thread_id == 5

    def test_thread_id_is_optional_backwards_compatible(self, tmp_path: Path) -> None:
        """v0.4.1 registries (no ``thread_id``) must continue to work."""
        path = tmp_path / "reg.json"
        path.write_text(
            json.dumps({"vlbeau-main": {"bot_token": "T:TOKEN", "chat_id": "123"}})
        )
        _, reg = load_registry(str(path))
        assert reg["vlbeau-main"].thread_id is None

    def test_non_integer_thread_id_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text(
            json.dumps(
                {"vlbeau-main": {"bot_token": "T", "chat_id": "123", "thread_id": "5"}}
            )
        )
        with pytest.raises(ValueError, match="thread_id"):
            load_registry(str(path))

    def test_boolean_thread_id_rejected(self, tmp_path: Path) -> None:
        """``True`` is an ``int`` in Python — guard against that footgun."""
        path = tmp_path / "bad.json"
        path.write_text(
            json.dumps(
                {"vlbeau-main": {"bot_token": "T", "chat_id": "123", "thread_id": True}}
            )
        )
        with pytest.raises(ValueError, match="thread_id"):
            load_registry(str(path))

    def test_legacy_format_warns_about_migration(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Legacy format is accepted but logs a deprecation warning."""
        path = tmp_path / "reg.json"
        path.write_text(
            json.dumps({"vlbeau-main": {"bot_token": "T:TOK", "chat_id": "123"}})
        )
        with caplog.at_level("WARNING", logger="a2a_mcp_bridge.wake"):
            load_registry(str(path))
        assert any(
            "legacy per-agent bot_token format" in r.message for r in caplog.records
        )


# --------------------------------------------------------------------------- #
# load_registry — shared-wake-bot format (v0.4.3+)
# --------------------------------------------------------------------------- #


class TestLoadRegistrySharedBot:
    """The v0.4.3+ format: a top-level ``wake_bot_token`` + ``agents`` dict."""

    def test_loads_shared_bot_format(self, tmp_path: Path) -> None:
        path = tmp_path / "reg.json"
        path.write_text(
            json.dumps(
                {
                    "wake_bot_token": "SHARED:TOKEN",
                    "agents": {
                        "vlbeau-main": {"chat_id": "-100111", "thread_id": 5},
                        "vlbeau-glm51": {"chat_id": "-100111", "thread_id": 7},
                    },
                }
            )
        )
        shared, reg = load_registry(str(path))
        assert shared == "SHARED:TOKEN"
        # Each entry stores chat_id + thread_id; per-agent bot_token is empty.
        assert reg["vlbeau-main"].chat_id == "-100111"
        assert reg["vlbeau-main"].thread_id == 5
        assert reg["vlbeau-main"].bot_token == ""
        assert reg["vlbeau-glm51"].thread_id == 7

    def test_shared_bot_requires_non_empty_token(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text(
            json.dumps({"wake_bot_token": "", "agents": {}})
        )
        with pytest.raises(ValueError, match="wake_bot_token"):
            load_registry(str(path))

    def test_shared_bot_requires_agents_dict(self, tmp_path: Path) -> None:
        """If ``wake_bot_token`` is set, ``agents`` must be an object."""
        path = tmp_path / "bad.json"
        path.write_text(
            json.dumps({"wake_bot_token": "T:TOK", "agents": "not a dict"})
        )
        with pytest.raises(ValueError, match="agents"):
            load_registry(str(path))

    def test_shared_bot_entry_requires_chat_id(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text(
            json.dumps(
                {"wake_bot_token": "T:TOK", "agents": {"vlbeau-main": {"thread_id": 5}}}
            )
        )
        with pytest.raises(ValueError, match="chat_id"):
            load_registry(str(path))

    def test_shared_bot_entry_accepts_message_thread_id_alias(
        self, tmp_path: Path
    ) -> None:
        """Operators typing the Telegram-native field name should still work."""
        path = tmp_path / "reg.json"
        path.write_text(
            json.dumps(
                {
                    "wake_bot_token": "T:TOK",
                    "agents": {
                        "vlbeau-main": {
                            "chat_id": "-100111",
                            "message_thread_id": 42,
                        }
                    },
                }
            )
        )
        _, reg = load_registry(str(path))
        assert reg["vlbeau-main"].thread_id == 42

    def test_shared_bot_does_not_warn_about_legacy(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The new format must NOT trigger the legacy-migration warning."""
        path = tmp_path / "reg.json"
        path.write_text(
            json.dumps(
                {
                    "wake_bot_token": "T:TOK",
                    "agents": {"vlbeau-main": {"chat_id": "-100111"}},
                }
            )
        )
        with caplog.at_level("WARNING", logger="a2a_mcp_bridge.wake"):
            load_registry(str(path))
        assert not any("legacy" in r.message.lower() for r in caplog.records)


# --------------------------------------------------------------------------- #
# TelegramWaker.wake — legacy mode (per-agent token)
# --------------------------------------------------------------------------- #


@pytest.fixture
def waker_registry() -> dict[str, WakeEntry]:
    return {
        "vlbeau-main": WakeEntry(bot_token="T:TOKEN", chat_id="999"),
    }


class TestTelegramWakerLegacy:
    def test_wake_posts_to_sendmessage_endpoint(
        self, waker_registry: dict[str, WakeEntry]
    ) -> None:
        waker = TelegramWaker(waker_registry)
        fake_response = MagicMock()
        fake_response.__enter__.return_value = fake_response
        fake_response.read.return_value = b'{"ok":true}'
        fake_response.status = 200

        with patch("a2a_mcp_bridge.wake.urlopen", return_value=fake_response) as up:
            ok = waker.wake("vlbeau-main", sender_id="vlbeau-glm51")

        assert ok is True
        assert up.call_count == 1
        request_obj = up.call_args.args[0]
        # URL must target the legacy per-agent token
        assert "T:TOKEN" in request_obj.full_url
        assert request_obj.full_url.endswith("/sendMessage")
        body = request_obj.data.decode("utf-8")
        assert "chat_id=999" in body
        assert "vlbeau-glm51" in body  # sender name mentioned

    def test_wake_unknown_agent_returns_false_no_http(
        self, waker_registry: dict[str, WakeEntry]
    ) -> None:
        waker = TelegramWaker(waker_registry)
        with patch("a2a_mcp_bridge.wake.urlopen") as up:
            assert waker.wake("ghost-agent", sender_id="alice") is False
        up.assert_not_called()

    def test_wake_empty_registry_returns_false(self) -> None:
        waker = TelegramWaker({})
        with patch("a2a_mcp_bridge.wake.urlopen") as up:
            assert waker.wake("anything", sender_id="alice") is False
        up.assert_not_called()

    def test_wake_swallows_http_errors(
        self, waker_registry: dict[str, WakeEntry]
    ) -> None:
        waker = TelegramWaker(waker_registry)
        with patch(
            "a2a_mcp_bridge.wake.urlopen",
            side_effect=HTTPError(url="x", code=403, msg="Forbidden", hdrs=None, fp=None),
        ):
            assert waker.wake("vlbeau-main", sender_id="a") is False

    def test_wake_swallows_network_errors(
        self, waker_registry: dict[str, WakeEntry]
    ) -> None:
        waker = TelegramWaker(waker_registry)
        with patch("a2a_mcp_bridge.wake.urlopen", side_effect=URLError("network down")):
            assert waker.wake("vlbeau-main", sender_id="a") is False

    def test_wake_swallows_unexpected_exceptions(
        self, waker_registry: dict[str, WakeEntry]
    ) -> None:
        waker = TelegramWaker(waker_registry)
        with patch("a2a_mcp_bridge.wake.urlopen", side_effect=RuntimeError("boom")):
            assert waker.wake("vlbeau-main", sender_id="a") is False

    def test_message_contains_inbox_hint(
        self, waker_registry: dict[str, WakeEntry]
    ) -> None:
        waker = TelegramWaker(waker_registry)
        fake_response = MagicMock()
        fake_response.__enter__.return_value = fake_response
        fake_response.read.return_value = b"{}"
        fake_response.status = 200

        with patch("a2a_mcp_bridge.wake.urlopen", return_value=fake_response) as up:
            waker.wake("vlbeau-main", sender_id="vlbeau-glm51")

        body = up.call_args.args[0].data.decode("utf-8")
        assert "agent_inbox" in body

    def test_message_names_reply_target_explicitly(
        self, waker_registry: dict[str, WakeEntry]
    ) -> None:
        """Regression guard (v0.4.1): wake-up text must name the reply-to
        agent_id unambiguously."""
        waker = TelegramWaker(waker_registry)
        fake_response = MagicMock()
        fake_response.__enter__.return_value = fake_response
        fake_response.read.return_value = b"{}"
        fake_response.status = 200

        with patch("a2a_mcp_bridge.wake.urlopen", return_value=fake_response) as up:
            waker.wake("vlbeau-main", sender_id="vlbeau-glm51")

        import urllib.parse as _up

        encoded = up.call_args.args[0].data.decode("utf-8")
        parsed = dict(_up.parse_qsl(encoded))
        text = parsed["text"]

        assert "vlbeau-glm51" in text
        assert "agent_inbox" in text
        assert "agent_send" in text
        assert 'target="vlbeau-glm51"' in text
        assert "reply-to" in text.lower()

    # ----- v0.4.2: forum topic routing via message_thread_id ---------------- #

    def test_wake_posts_message_thread_id_when_set(self) -> None:
        registry = {
            "vlbeau-main": WakeEntry(
                bot_token="T:TOKEN",
                chat_id="-1001234567890",
                thread_id=5,
            ),
        }
        waker = TelegramWaker(registry)
        fake_response = MagicMock()
        fake_response.__enter__.return_value = fake_response
        fake_response.read.return_value = b"{}"
        fake_response.status = 200

        with patch("a2a_mcp_bridge.wake.urlopen", return_value=fake_response) as up:
            waker.wake("vlbeau-main", sender_id="vlbeau-glm51")

        body = up.call_args.args[0].data.decode("utf-8")
        assert "message_thread_id=5" in body
        assert "chat_id=" in body

    def test_wake_omits_message_thread_id_when_unset(
        self, waker_registry: dict[str, WakeEntry]
    ) -> None:
        waker = TelegramWaker(waker_registry)
        fake_response = MagicMock()
        fake_response.__enter__.return_value = fake_response
        fake_response.read.return_value = b"{}"
        fake_response.status = 200

        with patch("a2a_mcp_bridge.wake.urlopen", return_value=fake_response) as up:
            waker.wake("vlbeau-main", sender_id="vlbeau-glm51")

        body = up.call_args.args[0].data.decode("utf-8")
        assert "message_thread_id" not in body


# --------------------------------------------------------------------------- #
# TelegramWaker.wake — shared-bot mode (v0.4.3+)
# --------------------------------------------------------------------------- #


class TestTelegramWakerSharedBot:
    """Every wake-up is POSTed through ``shared_token`` regardless of target."""

    def test_wake_uses_shared_token_not_entry_token(self) -> None:
        """The shared token must be used even if the entry has a stale one."""
        registry = {
            "vlbeau-deepseek": WakeEntry(
                bot_token="STALE:IGNORE_ME",  # should NOT be used
                chat_id="-100111",
                thread_id=9,
            ),
        }
        waker = TelegramWaker(registry, shared_token="WAKE_BOT:TOKEN")
        fake = MagicMock()
        fake.__enter__.return_value = fake
        fake.status = 200
        with patch("a2a_mcp_bridge.wake.urlopen", return_value=fake) as up:
            ok = waker.wake("vlbeau-deepseek", sender_id="vlbeau-glm51")
        assert ok is True
        url = up.call_args.args[0].full_url
        assert "WAKE_BOT:TOKEN" in url
        assert "STALE:IGNORE_ME" not in url

    def test_wake_routes_to_correct_topic(self) -> None:
        """Shared bot + thread_id → POST includes message_thread_id."""
        registry = {
            "vlbeau-deepseek": WakeEntry(bot_token="", chat_id="-100111", thread_id=9),
        }
        waker = TelegramWaker(registry, shared_token="WAKE:TOK")
        fake = MagicMock()
        fake.__enter__.return_value = fake
        fake.status = 200
        with patch("a2a_mcp_bridge.wake.urlopen", return_value=fake) as up:
            waker.wake("vlbeau-deepseek", sender_id="vlbeau-glm51")
        body = up.call_args.args[0].data.decode("utf-8")
        assert "message_thread_id=9" in body
        assert "chat_id=-100111" in body

    def test_uses_shared_token_property_flag(self) -> None:
        legacy = TelegramWaker({})
        shared = TelegramWaker({}, shared_token="T:TOK")
        assert legacy.uses_shared_token is False
        assert shared.uses_shared_token is True

    def test_empty_entry_token_in_legacy_mode_returns_false(self) -> None:
        """Entries with no bot_token AND no shared_token cannot be waked."""
        registry = {
            "vlbeau-orphan": WakeEntry(bot_token="", chat_id="-100111", thread_id=3),
        }
        waker = TelegramWaker(registry)  # no shared_token
        with patch("a2a_mcp_bridge.wake.urlopen") as up:
            assert waker.wake("vlbeau-orphan", sender_id="a") is False
        up.assert_not_called()


# --------------------------------------------------------------------------- #
# Self-wake guard (v0.4.3) — sender == target must NEVER trigger a POST
# --------------------------------------------------------------------------- #


class TestSelfWakeGuard:
    def test_self_wake_is_silently_skipped_legacy(
        self, waker_registry: dict[str, WakeEntry]
    ) -> None:
        waker = TelegramWaker(waker_registry)
        with patch("a2a_mcp_bridge.wake.urlopen") as up:
            assert waker.wake("vlbeau-main", sender_id="vlbeau-main") is False
        up.assert_not_called()

    def test_self_wake_is_silently_skipped_shared(self) -> None:
        registry = {"vlbeau-main": WakeEntry(bot_token="", chat_id="-100111")}
        waker = TelegramWaker(registry, shared_token="T:TOK")
        with patch("a2a_mcp_bridge.wake.urlopen") as up:
            assert waker.wake("vlbeau-main", sender_id="vlbeau-main") is False
        up.assert_not_called()
