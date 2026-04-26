"""BusStore Protocol — abstract interface for bus storage backends.

ADR-006 Step 1: when ``--bus-url`` is set, ``HttpBusStore`` replaces
``Store`` as the backend, routing all operations through the HTTP façade.
Both implementations satisfy this Protocol so ``tools.py`` can remain
agnostic to the transport layer.

The Protocol is ``@runtime_checkable`` so that ``isinstance(x, BusStore)``
works for diagnostic assertions, but callers should prefer static typing
over runtime checks.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from .models import AgentRecord, Message, SendResult


@runtime_checkable
class BusStore(Protocol):
    """Interface for bus storage backends.

    Implementations:

    * ``Store`` — Direct SQLite access (local, mono-VPS).
      Accepts an optional ``SignalDir`` at construction; ``subscribe()``
      uses it for filesystem-based long-poll.

    * ``HttpBusStore`` — HTTP client to the bus façade (remote nodes).
      Routes all operations through the façade's REST API; ``subscribe()``
      uses HTTP long-poll.
    """

    # -- agents ------------------------------------------------------------

    def upsert_agent(
        self, agent_id: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """Register or touch an agent on the bus."""
        ...

    # -- messaging ---------------------------------------------------------

    def send_message(
        self,
        sender: str,
        recipient: str,
        body: str,
        metadata: dict[str, Any] | None = None,
        intent: str = "triage",
    ) -> SendResult:
        """Persist a message from *sender* to *recipient*.

        Returns a ``SendResult`` with ``message_id``, ``sent_at``, and
        ``recipient``.  Raises ``ValueError`` on validation failures
        (self-send, body too large, unknown target, invalid intent).
        """
        ...

    def read_inbox(
        self,
        agent_id: str,
        limit: int = 10,
        unread_only: bool = True,
    ) -> list[Message]:
        """Read messages for *agent_id*.

        When ``unread_only=True`` the read is **atomic mark-as-read**
        (ADR-001): the returned messages have their ``read_at`` set
        inside a single ``BEGIN IMMEDIATE`` transaction.
        """
        ...

    def peek_inbox(
        self,
        agent_id: str,
        since_ts: str | None = None,
        limit: int = 50,
    ) -> list[Message]:
        """Read-only inbox view — no mark-as-read side-effect.

        When ``since_ts`` is provided, returns messages with
        ``created_at >= since_ts`` sorted **ASC** (replay order).
        When ``None``, returns the ``limit`` most recent messages
        sorted **DESC**.
        """
        ...

    def list_agents(self, active_within_days: int = 7) -> list[AgentRecord]:
        """List agents seen on the bus within the active window."""
        ...

    # -- real-time ---------------------------------------------------------

    def subscribe(
        self,
        agent_id: str,
        timeout_seconds: float = 30.0,
        limit: int = 10,
    ) -> tuple[list[Message], bool]:
        """Long-poll for new messages.

        Returns ``(messages, timed_out)``.  When messages are already
        pending, returns them immediately (fast path).  Otherwise blocks
        up to ``timeout_seconds`` (capped at 55 s by implementations)
        waiting for a signal.

        Implementations:

        * ``Store`` — uses ``SignalDir.wait()`` for filesystem-based
          notification.  Raises ``RuntimeError`` if no ``SignalDir`` was
          provided at construction.
        * ``HttpBusStore`` — HTTP POST to the façade's ``/bus/subscribe``
          endpoint, which long-polls on the server side.
        """
        ...


# ---------------------------------------------------------------------------
# Façade response parsers
# ---------------------------------------------------------------------------

log = logging.getLogger(__name__)


def _parse_iso(value: str | datetime | None) -> datetime | None:
    """Parse an ISO-8601 string into a datetime, pass through existing datetimes."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _parse_message(data: dict[str, Any]) -> Message:
    """Convert a JSON dict from the façade into a Message model instance.

    Field mapping: id→id, sender→sender_id, body→body, metadata→metadata,
    sent_at→created_at, read_at→read_at, sender_session_id→sender_session_id,
    intent→intent.
    """
    created_at = data.get("sent_at") or data.get("created_at")
    return Message(
        id=data["id"],
        sender_id=data.get("sender", data.get("sender_id", "")),
        recipient_id=data.get("recipient", data.get("recipient_id", "")),
        body=data["body"],
        metadata=data.get("metadata"),
        created_at=_parse_iso(created_at),  # type: ignore[arg-type]
        read_at=_parse_iso(data.get("read_at")),
        sender_session_id=data.get("sender_session_id"),
        intent=data.get("intent", "triage"),
    )


def _parse_agent_record(data: dict[str, Any]) -> AgentRecord:
    """Convert a JSON dict from the façade into an AgentRecord model instance."""
    return AgentRecord(
        agent_id=data["agent_id"],
        first_seen_at=_parse_iso(data["first_seen_at"]),  # type: ignore[arg-type]
        last_seen_at=_parse_iso(data["last_seen_at"]),  # type: ignore[arg-type]
        online=data.get("online", False),
        metadata=data.get("metadata"),
    )


# ---------------------------------------------------------------------------
# HttpBusStore — HTTP client backend
# ---------------------------------------------------------------------------


class HttpBusStore:
    """HTTP client backend for ADR-006 Step 1 — routes all bus operations
    through the remote façade server."""

    def __init__(self, base_url: str, agent_id: str, timeout: float = 65.0) -> None:
        try:
            import httpx as _httpx
        except ImportError as exc:
            raise ImportError(
                "httpx is required for HttpBusStore (remote façade mode). "
                "Install it with: pip install a2a-mcp-bridge[remote]"
            ) from exc

        self._httpx = _httpx
        self._base_url = base_url.rstrip("/")
        self._agent_id = agent_id
        self._timeout = timeout
        self._client = _httpx.Client(
            timeout=_httpx.Timeout(timeout),
            headers={"X-Agent-Id": agent_id},
        )

    # -- helpers ------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    # -- agents ------------------------------------------------------------

    def upsert_agent(
        self, agent_id: str, metadata: dict[str, Any] | None = None
    ) -> None:
        payload: dict[str, Any] = {"agent_id": agent_id}
        if metadata is not None:
            payload["metadata"] = metadata
        try:
            resp = self._client.post(self._url("/register"), json=payload)
            resp.raise_for_status()
        except Exception as exc:
            log.warning("upsert_agent failed (best-effort): %s", exc)

    # -- messaging ---------------------------------------------------------

    def send_message(
        self,
        sender: str,
        recipient: str,
        body: str,
        metadata: dict[str, Any] | None = None,
        intent: str = "triage",
    ) -> SendResult:
        payload: dict[str, Any] = {
            "sender": sender,
            "recipient": recipient,
            "body": body,
            "intent": intent,
        }
        if metadata is not None:
            payload["metadata"] = metadata

        try:
            resp = self._client.post(self._url("/send"), json=payload)
        except self._httpx.HTTPError as exc:
            log.warning("send_message network error: %s", exc)
            raise ValueError(str(exc)) from exc

        data = resp.json()

        # Check for structured error in response body
        if "error" in data:
            err = data["error"]
            code = err.get("code", "UNKNOWN")
            message = err.get("message", "unknown error")
            raise ValueError(f"{code}: {message}")

        # Also guard against non-2xx without a structured error
        try:
            resp.raise_for_status()
        except self._httpx.HTTPStatusError as exc:
            log.warning("send_message HTTP %s: %s", exc.response.status_code, exc)
            raise ValueError(f"HTTP {exc.response.status_code}") from exc

        sent_at = data.get("sent_at")
        if isinstance(sent_at, str):
            sent_at = datetime.fromisoformat(sent_at)
        return SendResult(
            message_id=data["message_id"],
            sent_at=sent_at,  # type: ignore[arg-type]
            recipient=data["recipient"],
        )

    def read_inbox(
        self,
        agent_id: str,
        limit: int = 10,
        unread_only: bool = True,
    ) -> list[Message]:
        try:
            resp = self._client.post(
                self._url("/inbox"),
                json={"agent_id": agent_id, "limit": limit, "unread_only": unread_only},
            )
            resp.raise_for_status()
            data = resp.json()
            return [_parse_message(m) for m in data.get("messages", [])]
        except self._httpx.HTTPStatusError as exc:
            log.warning("read_inbox HTTP %s: %s", exc.response.status_code, exc)
            return []
        except self._httpx.HTTPError as exc:
            log.warning("read_inbox network error: %s", exc)
            return []

    def peek_inbox(
        self,
        agent_id: str,
        since_ts: str | None = None,
        limit: int = 50,
    ) -> list[Message]:
        try:
            payload: dict[str, Any] = {"agent_id": agent_id, "limit": limit}
            if since_ts is not None:
                payload["since_ts"] = since_ts
            resp = self._client.post(self._url("/inbox_peek"), json=payload)
            resp.raise_for_status()
            data = resp.json()
            return [_parse_message(m) for m in data.get("messages", [])]
        except self._httpx.HTTPStatusError as exc:
            log.warning("peek_inbox HTTP %s: %s", exc.response.status_code, exc)
            return []
        except self._httpx.HTTPError as exc:
            log.warning("peek_inbox network error: %s", exc)
            return []

    def list_agents(self, active_within_days: int = 7) -> list[AgentRecord]:
        try:
            resp = self._client.post(
                self._url("/list"),
                json={"active_within_days": active_within_days},
            )
            resp.raise_for_status()
            data = resp.json()
            return [_parse_agent_record(a) for a in data.get("agents", [])]
        except self._httpx.HTTPStatusError as exc:
            log.warning("list_agents HTTP %s: %s", exc.response.status_code, exc)
            return []
        except self._httpx.HTTPError as exc:
            log.warning("list_agents network error: %s", exc)
            return []

    # -- real-time ---------------------------------------------------------

    _MAX_SUBSCRIBE_TIMEOUT = 55.0

    def subscribe(
        self,
        agent_id: str,
        timeout_seconds: float = 30.0,
        limit: int = 10,
    ) -> tuple[list[Message], bool]:
        """Long-poll for new messages via HTTP POST to the façade.

        Network errors return ``([], True)`` (timed out) rather than raising,
        so callers can treat a failed long-poll identically to a timeout.

        ``timeout_seconds`` is capped at 55 s to match the server-side limit
        (``MAX_SUBSCRIBE_TIMEOUT_SECONDS`` in the local Store).
        """
        capped_timeout = min(timeout_seconds, self._MAX_SUBSCRIBE_TIMEOUT)
        try:
            resp = self._client.post(
                self._url("/subscribe"),
                json={
                    "agent_id": agent_id,
                    "timeout_seconds": capped_timeout,
                    "limit": limit,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            messages = [_parse_message(m) for m in data.get("messages", [])]
            timed_out = data.get("timed_out", False)
            return messages, timed_out
        except self._httpx.HTTPError as exc:
            log.warning("subscribe HTTP/network error: %s", exc)
            return [], True

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Close the underlying httpx.Client."""
        self._client.close()
