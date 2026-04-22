"""Tests for the Telegram wake-up layer (v0.3)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

from a2a_mcp_bridge.wake import TelegramWaker, WakeEntry, load_registry

# --------------------------------------------------------------------------- #
# load_registry
# --------------------------------------------------------------------------- #


class TestLoadRegistry:
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
        reg = load_registry(str(path))
        assert reg["vlbeau-main"].bot_token == "abc:def"
        assert reg["vlbeau-main"].chat_id == "123"
        assert reg["vlbeau-glm51"].chat_id == "456"

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert load_registry(str(tmp_path / "nope.json")) == {}

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

    # ----- v0.4.2: optional thread_id for forum-topic routing --------------- #

    def test_loads_entry_with_thread_id(self, tmp_path: Path) -> None:
        """Registry entries MAY carry a ``thread_id`` int for forum topics."""
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
        reg = load_registry(str(path))
        assert reg["vlbeau-main"].thread_id == 5

    def test_thread_id_is_optional_backwards_compatible(self, tmp_path: Path) -> None:
        """v0.4.1 registries (no ``thread_id``) must continue to work."""
        path = tmp_path / "reg.json"
        path.write_text(
            json.dumps({"vlbeau-main": {"bot_token": "T:TOKEN", "chat_id": "123"}})
        )
        reg = load_registry(str(path))
        assert reg["vlbeau-main"].thread_id is None

    def test_non_integer_thread_id_raises(self, tmp_path: Path) -> None:
        """``thread_id`` MUST be an int (or absent); anything else is a typo."""
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


# --------------------------------------------------------------------------- #
# TelegramWaker.wake
# --------------------------------------------------------------------------- #


@pytest.fixture
def waker_registry() -> dict[str, WakeEntry]:
    return {
        "vlbeau-main": WakeEntry(bot_token="T:TOKEN", chat_id="999"),
    }


class TestTelegramWakerWake:
    def test_wake_posts_to_sendmessage_endpoint(self, waker_registry: dict[str, WakeEntry]) -> None:
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
        # URL must target the correct bot token
        assert "T:TOKEN" in request_obj.full_url
        assert request_obj.full_url.endswith("/sendMessage")
        # Body is form-urlencoded with chat_id + text
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

    def test_wake_swallows_http_errors(self, waker_registry: dict[str, WakeEntry]) -> None:
        waker = TelegramWaker(waker_registry)
        with patch(
            "a2a_mcp_bridge.wake.urlopen",
            side_effect=HTTPError(url="x", code=403, msg="Forbidden", hdrs=None, fp=None),
        ):
            assert waker.wake("vlbeau-main", sender_id="a") is False

    def test_wake_swallows_network_errors(self, waker_registry: dict[str, WakeEntry]) -> None:
        waker = TelegramWaker(waker_registry)
        with patch("a2a_mcp_bridge.wake.urlopen", side_effect=URLError("network down")):
            assert waker.wake("vlbeau-main", sender_id="a") is False

    def test_wake_swallows_unexpected_exceptions(
        self, waker_registry: dict[str, WakeEntry]
    ) -> None:
        """Unexpected errors must not propagate and block agent_send."""
        waker = TelegramWaker(waker_registry)
        with patch("a2a_mcp_bridge.wake.urlopen", side_effect=RuntimeError("boom")):
            # best-effort layer: never raise to caller
            assert waker.wake("vlbeau-main", sender_id="a") is False

    def test_message_contains_inbox_hint(self, waker_registry: dict[str, WakeEntry]) -> None:
        waker = TelegramWaker(waker_registry)
        fake_response = MagicMock()
        fake_response.__enter__.return_value = fake_response
        fake_response.read.return_value = b"{}"
        fake_response.status = 200

        with patch("a2a_mcp_bridge.wake.urlopen", return_value=fake_response) as up:
            waker.wake("vlbeau-main", sender_id="vlbeau-glm51")

        body = up.call_args.args[0].data.decode("utf-8")
        # The prompt should hint at agent_inbox so the recipient knows what to do
        assert "agent_inbox" in body

    def test_message_names_reply_target_explicitly(
        self, waker_registry: dict[str, WakeEntry]
    ) -> None:
        """Regression guard (v0.4.1): the wake-up text must make the reply-to
        agent_id unambiguous so an LLM reading it cannot confuse it with a
        Telegram bot username or chat peer.
        """
        waker = TelegramWaker(waker_registry)
        fake_response = MagicMock()
        fake_response.__enter__.return_value = fake_response
        fake_response.read.return_value = b"{}"
        fake_response.status = 200

        with patch("a2a_mcp_bridge.wake.urlopen", return_value=fake_response) as up:
            waker.wake("vlbeau-main", sender_id="vlbeau-glm51")

        # The HTTP POST body is urlencoded form data; decode it to recover the text
        import urllib.parse as _up

        encoded = up.call_args.args[0].data.decode("utf-8")
        parsed = dict(_up.parse_qsl(encoded))
        text = parsed["text"]

        # Must name the sender as a copy-pasteable agent_id
        assert "vlbeau-glm51" in text
        # Must mention both tools the recipient will use
        assert "agent_inbox" in text
        assert "agent_send" in text
        # Must show the exact reply-to call signature
        assert 'target="vlbeau-glm51"' in text
        # Must label the sender as the reply target so LLMs don't guess
        assert "reply-to" in text.lower()

    # ----- v0.4.2: forum topic routing via message_thread_id ---------------- #

    def test_wake_posts_message_thread_id_when_set(self) -> None:
        """When the WakeEntry has a ``thread_id``, the POST must include
        ``message_thread_id`` so Telegram routes to the correct forum topic.
        """
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
        # Telegram field name in the API is message_thread_id
        assert "message_thread_id=5" in body
        # chat_id still present (not replaced by thread_id)
        assert "chat_id=" in body

    def test_wake_omits_message_thread_id_when_unset(
        self, waker_registry: dict[str, WakeEntry]
    ) -> None:
        """v0.4.1 behaviour preserved: no thread_id → no message_thread_id."""
        waker = TelegramWaker(waker_registry)
        fake_response = MagicMock()
        fake_response.__enter__.return_value = fake_response
        fake_response.read.return_value = b"{}"
        fake_response.status = 200

        with patch("a2a_mcp_bridge.wake.urlopen", return_value=fake_response) as up:
            waker.wake("vlbeau-main", sender_id="vlbeau-glm51")

        body = up.call_args.args[0].data.decode("utf-8")
        assert "message_thread_id" not in body
