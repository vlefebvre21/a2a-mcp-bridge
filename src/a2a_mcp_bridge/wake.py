"""Telegram wake-up layer for a2a-mcp-bridge (v0.3).

When an agent receives a message via ``agent_send``, we optionally send a short
Telegram notification to the recipient's bot so the recipient's gateway wakes
up and the agent gets a chance to process its inbox.

This is a **best-effort** optimisation, just like the signal files:

* Missing registry entry → no wake, return ``False``.
* Telegram API error / network error → log a warning, return ``False``.
* Unexpected exception → caught defensively, returned as ``False``.

The canonical record of a message is still the SQLite store. Failing to wake
the recipient must never prevent ``agent_send`` from storing the message.

Registry format (JSON file, default ``~/.a2a-wake-registry.json``)::

    {
        "vlbeau-main":  {"bot_token": "123:ABC", "chat_id": "1395012867"},
        "vlbeau-glm51": {"bot_token": "123:ABC", "chat_id": "1395012867"}
    }
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger("a2a_mcp_bridge.wake")

TELEGRAM_API_BASE = "https://api.telegram.org"
DEFAULT_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class WakeEntry:
    """One recipient's Telegram delivery details."""

    bot_token: str
    chat_id: str


def load_registry(path: str) -> dict[str, WakeEntry]:
    """Load the JSON wake registry from ``path``.

    Returns an empty dict if the file does not exist (lets the caller treat
    wake-up as opt-in without extra plumbing). Raises :class:`ValueError` if
    the file exists but is malformed.
    """
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        raw: Any = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in wake registry {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"wake registry {path} must be a JSON object at top level")

    registry: dict[str, WakeEntry] = {}
    for agent_id, entry in raw.items():
        if not isinstance(entry, dict):
            raise ValueError(f"wake registry entry for {agent_id!r} must be an object")
        token = entry.get("bot_token")
        chat_id = entry.get("chat_id")
        if not isinstance(token, str) or not token:
            raise ValueError(
                f"wake registry entry for {agent_id!r} is missing a string 'bot_token'"
            )
        if not isinstance(chat_id, str) or not chat_id:
            raise ValueError(f"wake registry entry for {agent_id!r} is missing a string 'chat_id'")
        registry[agent_id] = WakeEntry(bot_token=token, chat_id=chat_id)
    return registry


def _format_message(sender_id: str) -> str:
    """Short, direct Telegram wake-up message. Kept under 200 chars."""
    return (
        f"Message A2A de {sender_id} : consulte ton inbox avec agent_inbox "
        f"et réponds via agent_send."
    )


class TelegramWaker:
    """Best-effort Telegram notifier, keyed by recipient agent_id."""

    def __init__(
        self,
        registry: dict[str, WakeEntry],
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._registry = registry
        self._timeout = timeout_seconds

    def has(self, agent_id: str) -> bool:
        return agent_id in self._registry

    def __len__(self) -> int:
        return len(self._registry)

    def wake(self, agent_id: str, sender_id: str) -> bool:
        """Send a wake-up Telegram message to ``agent_id``'s registered bot.

        Returns ``True`` on HTTP 2xx, ``False`` on any other outcome (unknown
        agent, HTTP error, network error, unexpected exception). Never raises.
        """
        entry = self._registry.get(agent_id)
        if entry is None:
            logger.debug("wake skipped: %s not in registry", agent_id)
            return False

        url = f"{TELEGRAM_API_BASE}/bot{entry.bot_token}/sendMessage"
        payload = urlencode(
            {
                "chat_id": entry.chat_id,
                "text": _format_message(sender_id),
                "disable_notification": "false",
            }
        ).encode("utf-8")
        req = Request(
            url,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )

        try:
            with urlopen(req, timeout=self._timeout) as resp:
                status = getattr(resp, "status", 200)
                if 200 <= status < 300:
                    return True
                logger.warning(
                    "wake %s -> non-2xx status %s from Telegram",
                    agent_id,
                    status,
                )
                return False
        except HTTPError as exc:
            logger.warning("wake %s -> HTTPError %s: %s", agent_id, exc.code, exc.reason)
            return False
        except URLError as exc:
            logger.warning("wake %s -> network error: %s", agent_id, exc.reason)
            return False
        except Exception as exc:  # defensive: never propagate
            logger.warning("wake %s -> unexpected error: %s", agent_id, exc)
            return False
