"""Wake-up layer for a2a-mcp-bridge.

When an agent receives a message via ``agent_send``, we optionally POST a
wake-up notification to the recipient so the recipient's gateway wakes up
and the agent gets a chance to process its inbox.

This is a **best-effort** optimisation. The canonical record of a message
is still the SQLite store: failing to wake the recipient must never prevent
``agent_send`` from storing the message. Callers (``server.py``) persist
first and wake second; wake errors are logged at WARNING but never raised.

Registry format (JSON file, default ``~/.a2a-wake-registry.json``).

**Current format (v0.4.4+) — shared webhook secret**::

    {
        "wake_webhook_secret": "<64-hex HMAC secret>",
        "agents": {
            "vlbeau-main":  {"wake_webhook_url": "http://127.0.0.1:8651/webhooks/wake"},
            "vlbeau-glm51": {"wake_webhook_url": "http://127.0.0.1:8653/webhooks/wake"}
        }
    }

Each agent's Hermes gateway exposes a local HTTP webhook endpoint that
triggers a real agent session on POST. We sign the body with HMAC-SHA256
using the shared secret and POST to the agent's ``wake_webhook_url``. The
gateway validates the signature, spawns a session, and the agent reads its
inbox. This replaces the Telegram-based wake-up because a bot never
receives its own messages in a Telegram supergroup with forum topics, and
routing via a shared "crier" bot still left the recipient's gateway deaf
(it polls its own bot, not the crier's). HTTP webhook POSTs bypass
Telegram entirely and always reach the intended gateway.

**Legacy formats (v0.3 - v0.4.3.1) — Telegram-based**::

    # v0.4.3+ shared-wake-bot
    {"wake_bot_token": "...", "agents": {"X": {"chat_id": "...", "thread_id": 5}}}

    # v0.3 - v0.4.2 per-agent token
    {"X": {"bot_token": "...", "chat_id": "..."}}

These are detected and **rejected with a WARNING**: wake-up is disabled,
but the registry is not parsed and ``agent_send`` continues to persist
messages (per the best-effort contract). Operators must regenerate the
registry with ``a2a-mcp-bridge wake-registry init`` under v0.4.4+ to
re-enable wake-up.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger("a2a_mcp_bridge.wake")

DEFAULT_TIMEOUT_SECONDS = 5.0
# Rough upper bound on webhook URL length to catch typos / accidental
# garbage in the registry before we try to open a socket.
_MAX_URL_LENGTH = 2048


@dataclass(frozen=True)
class WakeEntry:
    """One recipient's webhook wake-up URL.

    ``wake_webhook_url`` is the full URL (scheme + host + port + path) of
    the recipient gateway's wake endpoint. The bridge POSTs an HMAC-signed
    JSON payload to this URL; the gateway's webhook adapter validates the
    signature and spawns a real agent session.
    """

    wake_webhook_url: str


def _parse_entry(agent_id: str, entry: Any) -> WakeEntry:
    """Validate and coerce a single registry entry into a :class:`WakeEntry`."""
    if not isinstance(entry, dict):
        raise ValueError(f"wake registry entry for {agent_id!r} must be an object")

    url = entry.get("wake_webhook_url")
    if not isinstance(url, str) or not url:
        raise ValueError(
            f"wake registry entry for {agent_id!r} is missing a string "
            f"'wake_webhook_url'"
        )
    if len(url) > _MAX_URL_LENGTH:
        raise ValueError(
            f"wake registry entry for {agent_id!r} has a 'wake_webhook_url' "
            f"longer than {_MAX_URL_LENGTH} characters"
        )
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError(
            f"wake registry entry for {agent_id!r} has a 'wake_webhook_url' "
            f"that does not start with http:// or https://"
        )

    return WakeEntry(wake_webhook_url=url)


def _has_legacy_keys(raw: dict[str, Any]) -> bool:
    """Detect v0.3 - v0.4.3.1 Telegram-based registry formats.

    Returns True if the raw dict has either:

    * top-level ``wake_bot_token`` (v0.4.3 shared-wake-bot), or
    * per-agent entries with a ``bot_token`` field (v0.3 - v0.4.2).
    """
    if "wake_bot_token" in raw:
        return True
    return any(
        isinstance(value, dict) and "bot_token" in value for value in raw.values()
    )


def load_registry(path: str) -> tuple[str | None, dict[str, WakeEntry]]:
    """Load the wake registry from ``path``.

    Returns a ``(shared_secret, entries)`` tuple:

    * ``shared_secret`` is the HMAC secret used to sign wake-up POSTs, or
      ``None`` when the registry is empty/missing/legacy.
    * ``entries`` maps ``agent_id`` to its :class:`WakeEntry`.

    A missing file returns ``(None, {})`` so callers can treat wake-up as
    opt-in. A legacy (Telegram) registry is detected, a WARNING is logged
    to prompt operator migration, and ``(None, {})`` is returned so that
    wake-up is **disabled** (not silently falling back). A malformed file
    raises :class:`ValueError`.
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

    # Current format (v0.4.4+): shared webhook secret + agents dict with URLs.
    shared_secret = raw.get("wake_webhook_secret")
    agents_raw = raw.get("agents")

    if shared_secret is not None or (
        isinstance(agents_raw, dict)
        and any(
            isinstance(v, dict) and "wake_webhook_url" in v
            for v in agents_raw.values()
        )
    ):
        if not isinstance(shared_secret, str) or not shared_secret:
            raise ValueError(
                f"wake registry {path}: 'wake_webhook_secret' must be a "
                f"non-empty string"
            )
        if not isinstance(agents_raw, dict):
            raise ValueError(
                f"wake registry {path}: 'agents' must be an object when "
                f"'wake_webhook_secret' is set"
            )
        entries: dict[str, WakeEntry] = {}
        for agent_id, entry in agents_raw.items():
            entries[agent_id] = _parse_entry(agent_id, entry)
        return shared_secret, entries

    # Legacy Telegram-based format (v0.3 - v0.4.3.1).
    # Detected and refused: wake-up disabled, migration WARNING logged.
    if _has_legacy_keys(raw):
        logger.warning(
            "wake registry %s uses a legacy Telegram-based format "
            "(v0.3 - v0.4.3.1). Wake-up is disabled until you migrate to "
            "the v0.4.4+ webhook format. Run "
            "`a2a-mcp-bridge wake-registry init` to regenerate.",
            path,
        )
        return None, {}

    # Empty or unrecognised: return empty so wake-up is a no-op.
    if not raw:
        return None, {}
    raise ValueError(
        f"wake registry {path}: unrecognised structure "
        f"(no 'wake_webhook_secret', no 'wake_bot_token', no per-agent 'bot_token')"
    )


def _sign_body(body: bytes, secret: str) -> str:
    """Compute the HMAC-SHA256 hex digest of ``body`` under ``secret``."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


class WebhookWaker:
    """Best-effort HTTP-webhook notifier, keyed by recipient agent_id.

    Each wake-up is an HMAC-signed JSON POST to the recipient gateway's
    ``wake_webhook_url``. The gateway's webhook adapter validates the
    signature (via the shared ``wake_webhook_secret``) and spawns a real
    agent session that will read the A2A inbox.

    Self-wake (``agent_id == sender_id``) is skipped unconditionally: an
    agent sending a message to itself would otherwise trigger a wake-up
    loop on the same gateway.
    """

    def __init__(
        self,
        registry: dict[str, WakeEntry],
        shared_secret: str | None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._registry = registry
        self._shared_secret = shared_secret
        self._timeout = timeout_seconds

    def has(self, agent_id: str) -> bool:
        return agent_id in self._registry

    def __len__(self) -> int:
        return len(self._registry)

    @property
    def configured(self) -> bool:
        """Whether the waker has everything it needs to POST wake-ups.

        A waker with no secret or no entries is a no-op: every call to
        :meth:`wake` returns ``False`` immediately.
        """
        return bool(self._shared_secret) and bool(self._registry)

    def wake(self, agent_id: str, sender_id: str) -> bool:
        """POST a wake-up webhook to ``agent_id``'s registered endpoint.

        Returns ``True`` on HTTP 2xx, ``False`` on any other outcome (unknown
        agent, self-wake skip, missing shared secret, HTTP error, network
        error, unexpected exception). Never raises.
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

        if not self._shared_secret:
            logger.debug(
                "wake skipped: %s has no shared webhook secret configured",
                agent_id,
            )
            return False

        # Compact, deterministic payload. The gateway's webhook adapter
        # treats this as an opaque event body; we keep fields stable so
        # future versions can extend (e.g. message_id for idempotency)
        # without breaking signatures.
        body = json.dumps(
            {"sender": sender_id, "target": agent_id, "source": "a2a-mcp-bridge"},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        signature = _sign_body(body, self._shared_secret)

        req = Request(
            entry.wake_webhook_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Signature": signature,
            },
            method="POST",
        )

        try:
            with urlopen(req, timeout=self._timeout) as resp:
                status = getattr(resp, "status", 200)
                if 200 <= status < 300:
                    return True
                logger.warning(
                    "wake %s -> non-2xx status %s from %s",
                    agent_id,
                    status,
                    entry.wake_webhook_url,
                )
                return False
        except HTTPError as exc:
            logger.warning(
                "wake %s -> HTTPError %s %s from %s",
                agent_id,
                exc.code,
                exc.reason,
                entry.wake_webhook_url,
            )
            return False
        except URLError as exc:
            logger.warning(
                "wake %s -> network error from %s: %s",
                agent_id,
                entry.wake_webhook_url,
                exc.reason,
            )
            return False
        except Exception as exc:  # defensive: never propagate
            logger.warning(
                "wake %s -> unexpected error from %s: %s",
                agent_id,
                entry.wake_webhook_url,
                exc,
            )
            return False
