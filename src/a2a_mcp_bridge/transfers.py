"""File transfer primitive (ADR-007 Option A) — same-machine staging dir.

Implements the ``agent_send_file`` / ``agent_fetch_file`` / ``agent_delete_file``
flow described in ADR-007 §4. Wire protocol and security model are
frozen by that ADR — do not change without amending it.
"""
from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

# Default limits — can be overridden via env (see _env_int below).
_DEFAULT_TTL_SECONDS = 86_400           # 24 h
_DEFAULT_MAX_TTL_SECONDS = 604_800      # 7 d
_DEFAULT_MAX_SIZE_BYTES = 100 * 1024 * 1024  # 100 MB
_DEFAULT_MAX_PENDING_PER_AGENT = 50
_SWEEP_INTERVAL_S = 300.0                # 5 min


def _env_int(name: str, default: int) -> int:
    """Read an integer env var with graceful fallback (pattern from rate_limit.py)."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer, using default %d", name, raw, default)
        return default


def resolve_transfer_dir() -> Path:
    """Return the staging directory, creating it with mode 0o700 if missing.

    Resolution order:
      1. ``A2A_TRANSFER_DIR`` env var (absolute path).
      2. ``$HOME/.a2a-transfers``.

    Mirrors the pattern used by :mod:`a2a_mcp_bridge.signals` for
    ``A2A_SIGNAL_DIR``.
    """
    override = os.environ.get("A2A_TRANSFER_DIR", "").strip()
    path = Path(override) if override else Path.home() / ".a2a-transfers"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def new_transfer_id() -> str:
    """Return a fresh UUID4 string."""
    return str(uuid.uuid4())


def transfer_path(transfer_id: str, sha256_hex: str, filename: str) -> Path:
    """Return the canonical on-disk path for a transfer.

    Layout: ``<transfer_dir>/<transfer_id>/<sha256[:16]>_<filename>``.
    The caller is responsible for creating the parent directory.
    """
    base = resolve_transfer_dir()
    return base / transfer_id / f"{sha256_hex[:16]}_{filename}"


def is_safe_path(candidate: Path) -> bool:
    """Return True iff *candidate*'s realpath lives under the transfer dir.

    Defends against path-traversal (``../../etc/passwd``) and symlink
    escapes. Both sides of the check are resolved via :func:`os.path.realpath`.
    """
    base = os.path.realpath(resolve_transfer_dir())
    try:
        real = os.path.realpath(candidate)
    except OSError:
        return False
    base_sep = base if base.endswith(os.sep) else base + os.sep
    return real == base or real.startswith(base_sep)
