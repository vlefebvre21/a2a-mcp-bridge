"""Signal-file notification layer for real-time delivery (v0.2).

Each recipient agent has a signal file at ``<signal_dir>/<agent_id>.notify``.
When ``agent_send`` stores a message, it also touches this file (updates mtime
and rewrites a small payload). Consumers can either:

* poll the file's mtime (what :class:`SignalDir.wait` does), or
* hook an external inotify/fswatch watcher on the directory and react.

The signal file is an advisory optimisation — the authoritative source of
messages remains the SQLite store. If a signal is missed, the next call to
``agent_inbox`` still returns the message.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("a2a_mcp_bridge.signals")

SIGNAL_SUFFIX = ".notify"


def signal_path_for(signal_dir: Path, agent_id: str) -> Path:
    """Return the filesystem path of the signal file for an agent."""
    return signal_dir / f"{agent_id}{SIGNAL_SUFFIX}"


class SignalDir:
    """A directory of per-agent notification files.

    The directory is created lazily on construction. Operations are best-effort
    — filesystem errors are logged but never raised to callers, so a signal
    failure cannot block the canonical SQLite write.
    """

    def __init__(self, path: str) -> None:
        self.path: Path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)

    def notify(self, agent_id: str) -> None:
        """Touch the signal file for ``agent_id`` to wake pending waiters."""
        target = signal_path_for(self.path, agent_id)
        try:
            # Write a timestamp so external watchers see content changes too.
            target.write_text(f"{time.time_ns()}\n", encoding="utf-8")
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("failed to write signal file %s: %s", target, exc)

    def wait(
        self,
        agent_id: str,
        timeout_seconds: float,
        poll_interval: float = 0.2,
    ) -> bool:
        """Block until the signal file for ``agent_id`` is updated or the timeout elapses.

        Returns ``True`` if a signal fired (either an already-present file that
        has not been consumed since this call, or a new write during the wait),
        ``False`` on timeout.

        The implementation uses mtime polling — portable across all platforms
        and containers (inotify is not always available). Poll interval defaults
        to 200 ms which gives low latency without measurable CPU cost.
        """
        target = signal_path_for(self.path, agent_id)

        # Fast path: a signal already exists that we haven't seen → consume it.
        if target.exists():
            with contextlib.suppress(OSError):
                os.remove(target)
            return True

        deadline = time.monotonic() + timeout_seconds
        poll_interval = max(0.01, min(poll_interval, 1.0))
        while time.monotonic() < deadline:
            if target.exists():
                with contextlib.suppress(OSError):
                    os.remove(target)
                return True
            time.sleep(poll_interval)
        return False
