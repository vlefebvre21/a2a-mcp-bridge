"""Telegram wake-up layer for a2a-mcp-bridge.

When an agent receives a message via ``agent_send``, we optionally send a short
Telegram notification to the recipient so the recipient's gateway wakes up and
the agent gets a chance to process its inbox.

This is a **best-effort** optimisation, just like the signal files:

* Missing registry entry → no wake, return ``False``.
* Telegram API error / network error → log a warning, return ``False``.
* Unexpected exception → caught defensively, returned as ``False``.

The canonical record of a message is still the SQLite store. Failing to wake
the recipient must never prevent ``agent_send`` from storing the message.

Registry format (JSON file, default ``~/.a2a-wake-registry.json``).

**Preferred format (v0.4.3+) — single shared bot**::

    {
        "wake_bot_token": "123:ABC",
        "agents": {
            "vlbeau-main":  {"chat_id": "-1001234567890", "thread_id": 5},
            "vlbeau-glm51": {"chat_id": "-1001234567890", "thread_id": 7}
        }
    }

A **single "wake bot"** token is used to POST every wake-up. In a Telegram
supergroup with forum topics this is required: a bot does not receive its
own messages, so a self-posted wake-up never reaches the bot's gateway.
Posting via a neutral bot (typically ``@VLBeauBot`` / ``vlbeau-main``)
gives the recipient's bot an actual incoming Telegram update, which is
what the Hermes gateway listens for.

**Legacy format (v0.3 - v0.4.2) — per-agent token**::

    {
        "vlbeau-main":  {"bot_token": "111:...", "chat_id": "1395012867"},
        "vlbeau-glm51": {"bot_token": "222:...", "chat_id": "1395012867", "thread_id": 7}
    }

Still accepted transparently; each wake-up is POSTed via the recipient's
own ``bot_token``. A warning is logged to encourage migration.
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
    """One recipient's Telegram delivery details.

    ``bot_token`` is the legacy per-agent token (v0.3 - v0.4.2). In the
    v0.4.3+ shared-wake-bot format it is an empty string and the waker
    uses ``TelegramWaker.shared_token`` instead.

    ``thread_id`` is optional: when set, the wake-up is routed to the given
    forum topic via Telegram's ``message_thread_id`` parameter.
    """

    bot_token: str
    chat_id: str
    thread_id: int | None = None


def _parse_entry(agent_id: str, entry: Any, *, require_token: bool) -> WakeEntry:
    """Validate and coerce a single registry entry into a :class:`WakeEntry`."""
    if not isinstance(entry, dict):
        raise ValueError(f"wake registry entry for {agent_id!r} must be an object")

    if require_token:
        token = entry.get("bot_token")
        if not isinstance(token, str) or not token:
            raise ValueError(
                f"wake registry entry for {agent_id!r} is missing a string 'bot_token'"
            )
    else:
        # Shared-wake-bot format: per-agent token is not used.
        token = ""

    chat_id = entry.get("chat_id")
    if not isinstance(chat_id, str) or not chat_id:
        raise ValueError(
            f"wake registry entry for {agent_id!r} is missing a string 'chat_id'"
        )

    # thread_id lives under "thread_id" (preferred, shorter) but we also
    # accept "message_thread_id" because that is the Telegram Bot API name
    # and some operators will type it by reflex.
    thread_id = entry.get("thread_id")
    if thread_id is None:
        thread_id = entry.get("message_thread_id")
    if thread_id is not None and (
        not isinstance(thread_id, int) or isinstance(thread_id, bool)
    ):
        raise ValueError(
            f"wake registry entry for {agent_id!r} has a non-integer 'thread_id'"
        )

    return WakeEntry(bot_token=token, chat_id=chat_id, thread_id=thread_id)


def load_registry(path: str) -> tuple[str | None, dict[str, WakeEntry]]:
    """Load the wake registry from ``path``.

    Returns a ``(shared_token, entries)`` tuple:

    * ``shared_token`` is the shared wake-bot token (v0.4.3+ format) or
      ``None`` when the legacy per-agent-token format is in use.
    * ``entries`` maps ``agent_id`` to its :class:`WakeEntry`.

    A missing file returns ``(None, {})`` so callers can treat wake-up as
    opt-in. A malformed file raises :class:`ValueError`.
    """
    p = Path(path)
    if not p.is_file():
        return None, {}

    try:
        raw: Any = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in wake registry {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"wake registry {path} must be a JSON object at top level")

    entries: dict[str, WakeEntry] = {}

    # Detect format: v0.4.3+ has top-level "wake_bot_token" + "agents" dict.
    shared_token = raw.get("wake_bot_token")
    if shared_token is not None:
        if not isinstance(shared_token, str) or not shared_token:
            raise ValueError(
                f"wake registry {path}: 'wake_bot_token' must be a non-empty string"
            )
        agents_raw = raw.get("agents")
        if not isinstance(agents_raw, dict):
            raise ValueError(
                f"wake registry {path}: 'agents' must be an object when "
                f"'wake_bot_token' is set"
            )
        for agent_id, entry in agents_raw.items():
            entries[agent_id] = _parse_entry(
                agent_id, entry, require_token=False
            )
        return shared_token, entries

    # Legacy format (v0.3 - v0.4.2): each entry carries its own bot_token.
    # Accepted for backward compatibility but logged as deprecated.
    legacy_keys_present = any(
        isinstance(v, dict) and "bot_token" in v for v in raw.values()
    )
    if legacy_keys_present:
        logger.warning(
            "wake registry %s uses the legacy per-agent bot_token format; "
            "consider migrating to the shared-wake-bot format (v0.4.3+) so "
            "that Telegram supergroups with forum topics work correctly. "
            "Run `a2a-mcp-bridge wake-registry init` to regenerate.",
            path,
        )
    for agent_id, entry in raw.items():
        entries[agent_id] = _parse_entry(agent_id, entry, require_token=True)
    return None, entries


def _format_message(sender_id: str) -> str:
    """Explicit wake-up Telegram message for LLM agents.

    The format names the reply-target explicitly so the receiving agent
    (an LLM reading the Telegram text) cannot confuse the A2A ``sender_id``
    with any surface-level Telegram identity (bot username, chat peer, etc.).
    """
    return (
        "Nouveau message A2A reçu.\n"
        f"- sender (reply-to) : {sender_id}\n"
        "- Pour lire          : agent_inbox()\n"
        f'- Pour répondre      : agent_send(target="{sender_id}", message="...")'
    )


class TelegramWaker:
    """Best-effort Telegram notifier, keyed by recipient agent_id.

    In the v0.4.3+ shared-wake-bot format, every wake-up is POSTed using
    ``shared_token``. In the legacy format, each recipient's
    ``WakeEntry.bot_token`` is used.

    Self-wake (``agent_id == sender_id``) is skipped unconditionally: an
    agent sending a message to itself would otherwise trigger a wake-up
    loop on the same gateway.
    """

    def __init__(
        self,
        registry: dict[str, WakeEntry],
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        *,
        shared_token: str | None = None,
    ) -> None:
        self._registry = registry
        self._timeout = timeout_seconds
        self._shared_token = shared_token

    def has(self, agent_id: str) -> bool:
        return agent_id in self._registry

    def __len__(self) -> int:
        return len(self._registry)

    @property
    def uses_shared_token(self) -> bool:
        """Whether this waker posts every wake-up via a single shared bot."""
        return self._shared_token is not None

    def wake(self, agent_id: str, sender_id: str) -> bool:
        """Send a wake-up Telegram message to ``agent_id``'s registered bot.

        Returns ``True`` on HTTP 2xx, ``False`` on any other outcome (unknown
        agent, self-wake skip, HTTP error, network error, unexpected
        exception). Never raises.
        """
        if agent_id == sender_id:
            # An agent waking itself is almost certainly a bug upstream — skip
            # silently rather than risk a wake-loop on the same gateway.
            logger.debug("wake skipped: sender == target (%s)", agent_id)
            return False

        entry = self._registry.get(agent_id)
        if entry is None:
            logger.debug("wake skipped: %s not in registry", agent_id)
            return False

        # Pick the token: shared (v0.4.3+) wins over per-agent (legacy).
        token = self._shared_token if self._shared_token else entry.bot_token
        if not token:
            logger.debug(
                "wake skipped: %s has no bot_token and no shared token is set",
                agent_id,
            )
            return False

        url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
        params: dict[str, str] = {
            "chat_id": entry.chat_id,
            "text": _format_message(sender_id),
            "disable_notification": "false",
        }
        if entry.thread_id is not None:
            # Forum topic routing. Telegram's Bot API field is
            # ``message_thread_id``; we keep ``thread_id`` in the registry
            # for ergonomics.
            params["message_thread_id"] = str(entry.thread_id)
        payload = urlencode(params).encode("utf-8")
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
