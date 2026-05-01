"""File transfer primitive (ADR-007 Option A) — same-machine staging dir.

Implements the ``agent_send_file`` / ``agent_fetch_file`` / ``agent_delete_file``
flow described in ADR-007 §4. Wire protocol and security model are
frozen by that ADR — do not change without amending it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from dataclasses import dataclass
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


@dataclass(frozen=True)
class TransferRecord:
    """Immutable record of a staged transfer (returned by stage_file)."""
    transfer_id: str
    sender_id: str
    recipient_id: str
    filename: str
    size: int
    sha256: str
    locator_path: str   # absolute path, scheme=file
    description: str
    created_at: float   # time.time() (wall clock, for expiry math)
    expires_at: float   # epoch seconds


def _hash_and_copy(src: Path, dest: Path) -> tuple[str, int]:
    """Copy *src* to *dest* (atomic rename) while computing sha256 + size.

    Writes to ``<dest>.tmp`` then renames. Flushes and fsyncs before rename.
    Creates parent dirs with mode 0o700, the staged file with mode 0o600.
    """
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = dest.parent / f".tmp.{dest.name}"
    h = hashlib.sha256()
    size = 0
    with open(src, "rb") as fin, open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as fout:
        while True:
            chunk = fin.read(64 * 1024)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
            fout.write(chunk)
        fout.flush()
        os.fsync(fout.fileno())
    os.rename(tmp, dest)
    return h.hexdigest(), size


def _count_pending_for_sender(sender_id: str) -> int:
    """Count un-expired transfers owned by *sender_id*."""
    import time as _time

    base = resolve_transfer_dir()
    now = _time.time()
    count = 0
    if not base.is_dir():
        return 0
    for child in base.iterdir():
        if not child.is_dir():
            continue
        meta_path = child / "meta.json"
        if not meta_path.is_file():
            continue
        try:
            m = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if m.get("sender_id") == sender_id and float(m.get("expires_at", 0.0)) > now:
            count += 1
    return count


def stage_file(
    source: Path,
    *,
    sender_id: str,
    recipient_id: str,
    filename: str,
    description: str = "",
    expires_in: int | None = None,
) -> TransferRecord:
    """Copy *source* into the staging dir and return a :class:`TransferRecord`.

    Raises:
        FileNotFoundError: if *source* does not exist.
        ValueError: ``TRANSFER_TOO_LARGE: ...`` if the file exceeds
            ``A2A_TRANSFER_MAX_SIZE_BYTES``.
        ValueError: ``TRANSFER_QUOTA_EXCEEDED: ...`` if the sender has
            too many pending transfers.
    """
    import time as _time

    if not source.is_file():
        raise FileNotFoundError(source)

    max_size = _env_int("A2A_TRANSFER_MAX_SIZE_BYTES", _DEFAULT_MAX_SIZE_BYTES)
    src_size = source.stat().st_size
    if src_size > max_size:
        raise ValueError(
            f"TRANSFER_TOO_LARGE: {src_size} bytes exceeds limit {max_size}"
        )

    # Quota check before staging
    max_pending = _env_int("A2A_TRANSFER_MAX_PENDING_PER_AGENT", _DEFAULT_MAX_PENDING_PER_AGENT)
    pending = _count_pending_for_sender(sender_id)
    if pending >= max_pending:
        raise ValueError(
            f"TRANSFER_QUOTA_EXCEEDED: {pending} pending transfers for {sender_id}, limit {max_pending}"
        )

    tid = new_transfer_id()
    rec_created_at = _time.time()

    # TTL clamping: min(ttl, max_ttl), no lower bound (negative = already expired, useful for tests)
    ttl = expires_in if expires_in is not None else _env_int("A2A_TRANSFER_DEFAULT_TTL_SECONDS", _DEFAULT_TTL_SECONDS)
    max_ttl = _env_int("A2A_TRANSFER_MAX_TTL_SECONDS", _DEFAULT_MAX_TTL_SECONDS)
    ttl = min(ttl, max_ttl)
    expires_at = rec_created_at + ttl

    # Stage with a first pass to get sha256, then rename to canonical name.
    # Two-step because the canonical filename includes sha256[:16].
    staging_tmp = resolve_transfer_dir() / tid / f".staging_{filename}"
    sha, actual_size = _hash_and_copy(source, staging_tmp)
    final = transfer_path(tid, sha, filename)
    os.rename(staging_tmp, final)

    # Write meta.json atomically
    manifest = {
        "transfer_id": tid,
        "sender_id": sender_id,
        "recipient_id": recipient_id,
        "filename": filename,
        "size": actual_size,
        "sha256": sha,
        "description": description,
        "created_at": rec_created_at,
        "expires_at": expires_at,
        "locator": {"scheme": "file", "path": str(final)},
        "version": 1,
    }
    meta_dir = resolve_transfer_dir() / tid
    meta_tmp = meta_dir / "meta.json.tmp"
    meta_tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    os.rename(meta_tmp, meta_dir / "meta.json")

    return TransferRecord(
        transfer_id=tid,
        sender_id=sender_id,
        recipient_id=recipient_id,
        filename=filename,
        size=actual_size,
        sha256=sha,
        locator_path=str(final),
        description=description,
        created_at=rec_created_at,
        expires_at=expires_at,
    )


def load_manifest(transfer_id: str) -> dict:
    """Return the parsed meta.json for *transfer_id*.

    Raises:
        FileNotFoundError: transfer_id unknown.
        ValueError: manifest JSON malformed.
    """
    base = resolve_transfer_dir()
    meta_path = base / transfer_id / "meta.json"
    if not meta_path.is_file():
        raise FileNotFoundError(transfer_id)
    try:
        return json.loads(meta_path.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"TRANSFER_MANIFEST_CORRUPT: {e}") from e


def resolve_locator_path(transfer_id: str, *, caller_id: str) -> Path:
    """Return the on-disk path for *transfer_id* after ACL check.

    Only the sender or recipient declared in the manifest may resolve.
    The returned path is also re-checked with :func:`is_safe_path`.

    Raises:
        FileNotFoundError: unknown transfer_id.
        PermissionError: caller is neither sender nor recipient.
        ValueError: locator path escapes the transfer dir (corrupt manifest).
    """
    m = load_manifest(transfer_id)
    if caller_id not in (m.get("sender_id"), m.get("recipient_id")):
        raise PermissionError(f"TRANSFER_ACL_DENIED: {caller_id} not authorised for {transfer_id}")
    path = Path(m["locator"]["path"])
    if not is_safe_path(path):
        raise ValueError(f"TRANSFER_UNSAFE_PATH: {path}")
    return path
