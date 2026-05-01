# ADR-007 Option A — Implementation Plan (Same-Machine File Transfer)

> **For the implementing agent (vlbeau-glm51 via A2A):**
>
> This plan is self-contained. Read it fully before touching code.
> Stop and report back on the A2A bus (target=`vlbeau-opus`,
> intent=`question`) if **anything** is ambiguous or doesn't match
> the codebase — do not improvise. Each task is 2-5 min of focused
> work, in TDD order. Commit after each green task.

**Goal:** Ship the Option A (same-machine staging dir) half of
[ADR-007](../adr/ADR-007-file-transfer-primitive.md) — three new MCP
tools (`agent_send_file`, `agent_fetch_file`, `agent_delete_file`)
plus the wire protocol and security primitives. One file per
transfer, local-filesystem only. Cross-machine HTTP transport is
Option C, out of scope here.

**Architecture:** All transfer logic lives in a new module
`src/a2a_mcp_bridge/transfers.py` that operates on a directory
resolved from `A2A_TRANSFER_DIR` (defaults to `~/.a2a-transfers/`).
A transfer reference travels over the existing A2A message bus as
a JSON body with `kind="file_transfer"` — no new SQLite table in
this PR (that arrives with Option C). The three new tools are
registered in `server.py` alongside the existing six.

**Tech stack:**
- Python 3.11+, stdlib only (no new runtime deps — keep the core
  lightweight, cf. ADR-006.1 §Optional dependencies).
- pytest for tests (existing suite).
- Follows the `RateLimiter.prune_stale` pattern (`rate_limit.py`)
  for the background sweeper.
- Follows the `SignalDir` resolution pattern (`signals.py`) for
  `A2A_TRANSFER_DIR`.

---

## Ground truth (verified 2026-05-01)

Before you start, here are the **exact shapes** of the code you will
be touching. If any of this drifts from reality, stop and report.

### Existing MCP tools (`src/a2a_mcp_bridge/server.py`)

Registered via `@mcp.tool()` decorator. Six of them today:

- `agent_send(target, message, metadata=None, intent=None)` — line 248
- `agent_inbox(...)` — line 299
- `agent_inbox_peek(...)` — line 335
- `agent_list(...)` — line 380
- `agent_subscribe(...)` — line 406
- `agent_ping()` — line 448

Every tool delegates to a `tool_*` function in `src/a2a_mcp_bridge/tools.py`.

### `tool_agent_send` signature (`tools.py:23`)

```python
def tool_agent_send(
    store: BusStore,
    caller_id: str,
    target: str,
    message: str,
    metadata: dict[str, Any] | None = None,
    signal_dir: SignalDir | None = None,
    waker: WebhookWaker | None = None,
    intent: str | None = None,
) -> dict[str, Any]:
    ...
```

You will call this directly from `tool_agent_send_file` (not
reimplement wake / signal logic).

### `BusStore` Protocol (`bus_store.py:23`)

Runtime-checkable Protocol implemented by both `Store` (SQLite) and
`HttpBusStore` (remote). Methods relevant to you: none you need to
change. File transfers ride on top of `send_message` via
`tool_agent_send`.

### Schema (`src/a2a_mcp_bridge/schema.sql`)

**No migration in this PR.** The transfer reference is carried in
`messages.body` as JSON. Option C (later) will add a `transfers`
table.

### `A2A_SIGNAL_DIR` resolution pattern (`signals.py`)

`signal_path_for(signal_dir: Path, agent_id: str) -> Path` returns
`signal_dir / f"{agent_id}.notify"`. Your `A2A_TRANSFER_DIR` follows
the same env-var-with-default pattern.

### Rate-limiter sweeper pattern (`rate_limit.py:95`)

```python
def prune_stale(self) -> int:
    """Remove entries whose timestamps have all expired."""
    now = time.monotonic()
    cutoff = now - 60.0
    stale = [k for k, ts in self.hits.items() if ts and not any(t > cutoff for t in ts)]
    for k in stale:
        del self.hits[k]
    return len(stale)
```

Your `_transfer_sweep` follows the same shape (find expired,
delete, log count, return count).

### Version

`pyproject.toml` currently at `0.6.2`. Bump to `0.7.0` in the final
task. `src/a2a_mcp_bridge/__init__.py::__version__` also needs
updating (GLM51 already fixed the 0.6.2 sync earlier today).

---

## Wire protocol (frozen — do not deviate)

As specified in ADR-007 §4.1, the message body carrying a transfer
reference is a JSON object:

```json
{
  "kind": "file_transfer",
  "version": 1,
  "transfer_id": "<uuid4>",
  "filename": "transcript.md",
  "size": 28845,
  "sha256": "7b2c9f...",
  "description": "YouTube summary for magent",
  "expires_at": "2026-05-02T19:42:41Z",
  "locator": {
    "scheme": "file",
    "path": "/home/vince/.a2a-transfers/<uuid>/<sha256[:16]>_transcript.md"
  }
}
```

For Option A, `locator.scheme` is **always** `"file"` and `locator.url`
is **absent**. Option C (later) will add the `"http"` variant.

---

## Security model (frozen — enforce in code)

From ADR-007 §4.2. Your code must honour:

1. **Path allow-list.** On write: `os.path.realpath(dest).startswith(realpath(transfer_dir))`. On read: same check AND `caller_id == transfer.recipient_id` (or sender for deletion).
2. **File mode.** Staged files created `0o600`; transfer dirs `0o700`.
3. **TTL.** Default 86400 s (24 h). Configurable per-transfer up to `A2A_TRANSFER_MAX_TTL_SECONDS` (default 604800 s = 7 d).
4. **Size cap.** `A2A_TRANSFER_MAX_SIZE_BYTES` (default 100 * 1024 * 1024 = 100 MB). Upload beyond → raise `ValueError("TRANSFER_TOO_LARGE: ...")`.
5. **Quota per agent.** `A2A_TRANSFER_MAX_PENDING_PER_AGENT` (default 50). Over quota → raise `ValueError("TRANSFER_QUOTA_EXCEEDED: ...")`.
6. **Integrity.** Writer writes to `<uuid>/.tmp.<filename>`, calls `os.fsync()`, then `os.rename()` to `<uuid>/<sha256[:16]>_<filename>`.

All env-var reads go through a `_env_int(name, default)` helper that
mirrors `rate_limit._env_int` — copy the pattern, don't import.

---

## Task sequence

### Task 1 — Create `transfers.py` skeleton and env resolution

**Objective:** New module with constants, env resolution, and
`resolve_transfer_dir()` helper.

**Files:**
- Create: `src/a2a_mcp_bridge/transfers.py`
- Test: `tests/test_transfers.py` (create)

**Step 1.1 — Write the failing test.**

Create `tests/test_transfers.py`:

```python
"""Tests for transfers.py — ADR-007 Option A (same-machine file transfer)."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_resolve_transfer_dir_defaults_to_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("A2A_TRANSFER_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    from a2a_mcp_bridge.transfers import resolve_transfer_dir

    result = resolve_transfer_dir()
    assert result == tmp_path / ".a2a-transfers"
    assert result.is_dir()
    assert oct(result.stat().st_mode)[-3:] == "700"


def test_resolve_transfer_dir_honours_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "custom"
    monkeypatch.setenv("A2A_TRANSFER_DIR", str(target))

    from a2a_mcp_bridge.transfers import resolve_transfer_dir

    result = resolve_transfer_dir()
    assert result == target
    assert result.is_dir()
```

**Step 1.2 — Run to see it fail.**

```bash
.venv/bin/python -m pytest tests/test_transfers.py -v
```
Expected: `ModuleNotFoundError: No module named 'a2a_mcp_bridge.transfers'`.

**Step 1.3 — Implement minimal `transfers.py`.**

```python
"""File transfer primitive (ADR-007 Option A) — same-machine staging dir.

Implements the ``agent_send_file`` / ``agent_fetch_file`` / ``agent_delete_file``
flow described in ADR-007 §4. Wire protocol and security model are
frozen by that ADR — do not change without amending it.
"""
from __future__ import annotations

import logging
import os
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
    if override:
        path = Path(override)
    else:
        path = Path.home() / ".a2a-transfers"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path
```

**Step 1.4 — Run tests, both green.**

```bash
.venv/bin/python -m pytest tests/test_transfers.py -v
```
Expected: `2 passed`.

**Step 1.5 — Ruff.**

```bash
.venv/bin/ruff check src/a2a_mcp_bridge/transfers.py tests/test_transfers.py
```
Expected: `All checks passed!`.

**Step 1.6 — Commit.**

```bash
git add src/a2a_mcp_bridge/transfers.py tests/test_transfers.py
git commit -m "feat(transfers): add transfer dir resolution (ADR-007 §4)"
```

---

### Task 2 — Transfer ID allocation + path safety

**Objective:** Helper functions `new_transfer_id()`, `transfer_path()`,
`is_safe_path()` with tests covering the path-traversal case.

**Files:**
- Modify: `src/a2a_mcp_bridge/transfers.py`
- Modify: `tests/test_transfers.py`

**Step 2.1 — Failing tests.**

Append to `tests/test_transfers.py`:

```python
def test_new_transfer_id_is_uuid4(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path))
    from a2a_mcp_bridge.transfers import new_transfer_id
    import uuid as _uuid

    tid = new_transfer_id()
    assert _uuid.UUID(tid).version == 4


def test_transfer_path_includes_sha_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path))
    from a2a_mcp_bridge.transfers import transfer_path

    p = transfer_path("11111111-2222-3333-4444-555555555555", "abcdef0123456789" + "0" * 48, "report.md")
    assert p.name == "abcdef0123456789_report.md"
    assert p.parent.name == "11111111-2222-3333-4444-555555555555"
    assert p.parent.parent == tmp_path


def test_is_safe_path_rejects_traversal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path))
    from a2a_mcp_bridge.transfers import is_safe_path

    assert is_safe_path(tmp_path / "abc" / "file.md") is True
    assert is_safe_path(Path("/etc/passwd")) is False
    assert is_safe_path(tmp_path / ".." / "file.md") is False  # normalises up
```

**Step 2.2 — Run to fail.** Expected: `ImportError` for each name.

**Step 2.3 — Implement.** Append to `transfers.py`:

```python
import uuid


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
```

**Step 2.4 — Tests green + ruff clean.**

```bash
.venv/bin/python -m pytest tests/test_transfers.py -v
.venv/bin/ruff check src/a2a_mcp_bridge/transfers.py tests/test_transfers.py
```

**Step 2.5 — Commit.**

```bash
git add src/a2a_mcp_bridge/transfers.py tests/test_transfers.py
git commit -m "feat(transfers): add id allocation + path-traversal guard"
```

---

### Task 3 — `stage_file()` — atomic write with sha256 + size cap

**Objective:** `stage_file(source_path, sender_id, filename) -> TransferRecord`
copies the source into the staging dir atomically and returns a
dataclass carrying the metadata.

**Files:**
- Modify: `src/a2a_mcp_bridge/transfers.py`
- Modify: `tests/test_transfers.py`

**Step 3.1 — Failing tests.**

```python
def test_stage_file_creates_staged_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path))
    src = tmp_path / "source.md"
    src.write_text("hello world\n")

    from a2a_mcp_bridge.transfers import stage_file

    rec = stage_file(src, sender_id="alice", filename="source.md")
    assert rec.size == len(b"hello world\n")
    assert rec.filename == "source.md"
    assert Path(rec.locator_path).is_file()
    assert Path(rec.locator_path).read_bytes() == b"hello world\n"
    # File mode 0o600
    assert oct(Path(rec.locator_path).stat().st_mode)[-3:] == "600"


def test_stage_file_rejects_too_large(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path))
    monkeypatch.setenv("A2A_TRANSFER_MAX_SIZE_BYTES", "10")

    src = tmp_path / "big.bin"
    src.write_bytes(b"x" * 100)

    from a2a_mcp_bridge.transfers import stage_file

    with pytest.raises(ValueError, match="TRANSFER_TOO_LARGE"):
        stage_file(src, sender_id="alice", filename="big.bin")


def test_stage_file_rejects_source_outside_fs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path))
    from a2a_mcp_bridge.transfers import stage_file

    with pytest.raises(FileNotFoundError):
        stage_file(tmp_path / "ghost.md", sender_id="alice", filename="ghost.md")
```

**Step 3.2 — Run to fail.** `ImportError` on `stage_file`.

**Step 3.3 — Implement.**

```python
import hashlib
import shutil
from dataclasses import dataclass


@dataclass(frozen=True)
class TransferRecord:
    """Immutable record of a staged transfer (returned by stage_file)."""
    transfer_id: str
    sender_id: str
    filename: str
    size: int
    sha256: str
    locator_path: str   # absolute path, scheme=file
    created_at: float   # time.time() (wall clock, for expiry math)


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


def stage_file(source: Path, *, sender_id: str, filename: str) -> TransferRecord:
    """Copy *source* into the staging dir and return a :class:`TransferRecord`.

    Raises:
        FileNotFoundError: if *source* does not exist.
        ValueError: ``TRANSFER_TOO_LARGE: ...`` if the file exceeds
            ``A2A_TRANSFER_MAX_SIZE_BYTES``.
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

    tid = new_transfer_id()
    # Stage with a first pass to get sha256, then rename to canonical name.
    # Two-step because the canonical filename includes sha256[:16].
    staging_tmp = resolve_transfer_dir() / tid / f".staging_{filename}"
    sha, actual_size = _hash_and_copy(source, staging_tmp)
    final = transfer_path(tid, sha, filename)
    os.rename(staging_tmp, final)

    return TransferRecord(
        transfer_id=tid,
        sender_id=sender_id,
        filename=filename,
        size=actual_size,
        sha256=sha,
        locator_path=str(final),
        created_at=_time.time(),
    )
```

**Step 3.4 — Tests + ruff.**

```bash
.venv/bin/python -m pytest tests/test_transfers.py -v
.venv/bin/ruff check src/a2a_mcp_bridge/transfers.py
```
Expected: all green.

**Step 3.5 — Commit.**

```bash
git add src/a2a_mcp_bridge/transfers.py tests/test_transfers.py
git commit -m "feat(transfers): stage_file with atomic rename + sha256 + size cap"
```

---

### Task 4 — Manifest + per-agent quota check

**Objective:** Write `meta.json` alongside the staged file; enforce
`A2A_TRANSFER_MAX_PENDING_PER_AGENT`.

**Files:**
- Modify: `src/a2a_mcp_bridge/transfers.py`
- Modify: `tests/test_transfers.py`

**Step 4.1 — Failing tests.**

```python
def test_stage_file_writes_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path))
    src = tmp_path / "source.md"
    src.write_text("hi")

    from a2a_mcp_bridge.transfers import stage_file

    rec = stage_file(src, sender_id="alice", filename="source.md", recipient_id="bob", description="test")
    meta_path = Path(rec.locator_path).parent / "meta.json"
    assert meta_path.is_file()
    meta = json.loads(meta_path.read_text())
    assert meta["sender_id"] == "alice"
    assert meta["recipient_id"] == "bob"
    assert meta["description"] == "test"
    assert meta["sha256"] == rec.sha256


def test_stage_file_quota_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path))
    monkeypatch.setenv("A2A_TRANSFER_MAX_PENDING_PER_AGENT", "2")

    from a2a_mcp_bridge.transfers import stage_file

    src = tmp_path / "src.md"
    src.write_text("x")
    stage_file(src, sender_id="alice", filename="a.md", recipient_id="bob")
    stage_file(src, sender_id="alice", filename="b.md", recipient_id="bob")
    with pytest.raises(ValueError, match="TRANSFER_QUOTA_EXCEEDED"):
        stage_file(src, sender_id="alice", filename="c.md", recipient_id="bob")
```

**Step 4.2 — Run to fail.** `stage_file()` doesn't accept
`recipient_id` / `description` yet, and no manifest is written.

**Step 4.3 — Implement.**

Update `TransferRecord` to add `recipient_id: str`, `description: str`,
`expires_at: float`. Update `stage_file` signature to
`(source: Path, *, sender_id: str, recipient_id: str, filename: str,
description: str = "", expires_in: int | None = None)`.

Add a helper `_count_pending_for_sender(sender_id: str) -> int` that
iterates `resolve_transfer_dir() / <uuid> / meta.json` and counts
entries with matching `sender_id` and `expires_at > time.time()`.

In `stage_file`, after the size check and before staging, call
`_count_pending_for_sender(sender_id)` and raise
`ValueError("TRANSFER_QUOTA_EXCEEDED: ...")` if over
`A2A_TRANSFER_MAX_PENDING_PER_AGENT`.

After the rename, write `meta.json` (0o600) atomically:

```python
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
```

Use `json.dumps(manifest, indent=2, sort_keys=True)`. Write to
`meta.json.tmp` then `os.rename`. TTL clamping:

```python
ttl = expires_in if expires_in is not None else _env_int("A2A_TRANSFER_DEFAULT_TTL_SECONDS", _DEFAULT_TTL_SECONDS)
max_ttl = _env_int("A2A_TRANSFER_MAX_TTL_SECONDS", _DEFAULT_MAX_TTL_SECONDS)
ttl = min(ttl, max_ttl)
expires_at = rec_created_at + ttl
```

**Step 4.4 — Tests + ruff.** Green.

**Step 4.5 — Commit.**

```bash
git commit -am "feat(transfers): manifest + per-agent quota enforcement"
```

---

### Task 5 — `load_manifest()` + `resolve_locator_path()`

**Objective:** Read a manifest back from disk; resolve a transfer_id
to its on-disk path with ACL check.

**Files:**
- Modify: `src/a2a_mcp_bridge/transfers.py`
- Modify: `tests/test_transfers.py`

**Step 5.1 — Failing tests.**

```python
def test_load_manifest_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path))
    src = tmp_path / "s.md"
    src.write_text("data")

    from a2a_mcp_bridge.transfers import stage_file, load_manifest

    rec = stage_file(src, sender_id="alice", filename="s.md", recipient_id="bob")
    m = load_manifest(rec.transfer_id)
    assert m["sender_id"] == "alice"
    assert m["recipient_id"] == "bob"
    assert m["sha256"] == rec.sha256


def test_resolve_locator_path_enforces_acl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path))
    src = tmp_path / "s.md"
    src.write_text("secret")

    from a2a_mcp_bridge.transfers import stage_file, resolve_locator_path

    rec = stage_file(src, sender_id="alice", filename="s.md", recipient_id="bob")
    # Recipient can fetch
    assert resolve_locator_path(rec.transfer_id, caller_id="bob") == Path(rec.locator_path)
    # Sender can fetch (useful for resend/verify)
    assert resolve_locator_path(rec.transfer_id, caller_id="alice") == Path(rec.locator_path)
    # Random third party cannot
    with pytest.raises(PermissionError):
        resolve_locator_path(rec.transfer_id, caller_id="eve")


def test_resolve_locator_path_unknown_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path))
    from a2a_mcp_bridge.transfers import resolve_locator_path

    with pytest.raises(FileNotFoundError):
        resolve_locator_path("nonexistent", caller_id="alice")
```

**Step 5.2 — Run to fail.** `ImportError` / `AttributeError`.

**Step 5.3 — Implement.**

```python
import json


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
```

**Step 5.4 — Tests + ruff.** Green.

**Step 5.5 — Commit.**

```bash
git commit -am "feat(transfers): load_manifest + resolve_locator_path with ACL"
```

---

### Task 6 — `delete_transfer()` + `_transfer_sweep()`

**Objective:** Explicit deletion (scoped to sender or recipient) and
TTL-based background sweep.

**Files:**
- Modify: `src/a2a_mcp_bridge/transfers.py`
- Modify: `tests/test_transfers.py`

**Step 6.1 — Failing tests.**

```python
def test_delete_transfer_scoped_to_parties(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path))
    src = tmp_path / "s.md"
    src.write_text("bye")

    from a2a_mcp_bridge.transfers import stage_file, delete_transfer

    rec = stage_file(src, sender_id="alice", filename="s.md", recipient_id="bob")

    # Eve can't delete
    with pytest.raises(PermissionError):
        delete_transfer(rec.transfer_id, caller_id="eve")
    assert Path(rec.locator_path).is_file()

    # Bob can (recipient)
    delete_transfer(rec.transfer_id, caller_id="bob")
    assert not Path(rec.locator_path).exists()
    assert not (tmp_path / rec.transfer_id).exists()


def test_sweep_removes_expired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path))
    src = tmp_path / "s.md"
    src.write_text("data")

    from a2a_mcp_bridge.transfers import stage_file, _transfer_sweep

    # TTL in the past
    rec = stage_file(src, sender_id="alice", filename="s.md", recipient_id="bob", expires_in=-1)
    assert Path(rec.locator_path).is_file()

    removed = _transfer_sweep()
    assert removed == 1
    assert not Path(rec.locator_path).exists()


def test_sweep_keeps_fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path))
    src = tmp_path / "s.md"
    src.write_text("data")

    from a2a_mcp_bridge.transfers import stage_file, _transfer_sweep

    rec = stage_file(src, sender_id="alice", filename="s.md", recipient_id="bob", expires_in=3600)
    assert _transfer_sweep() == 0
    assert Path(rec.locator_path).is_file()
```

Note: allow negative `expires_in` in `stage_file` for test purposes (
wrap the clamp with `ttl = max(ttl, -1)` — just don't clamp to >=0
so tests can force expiry).

Actually cleaner: make the ttl clamp `min(ttl, max_ttl)` only,
no lower bound. Negative TTLs produce an already-expired transfer,
which is useful for sweep tests.

**Step 6.2 — Run to fail.**

**Step 6.3 — Implement.**

```python
import shutil
import time as _time


def delete_transfer(transfer_id: str, *, caller_id: str) -> None:
    """Delete a staged transfer + manifest. Scoped to sender or recipient.

    Raises:
        FileNotFoundError: unknown transfer_id.
        PermissionError: caller is neither sender nor recipient.
    """
    m = load_manifest(transfer_id)  # raises FileNotFoundError
    if caller_id not in (m.get("sender_id"), m.get("recipient_id")):
        raise PermissionError(f"TRANSFER_ACL_DENIED: {caller_id} cannot delete {transfer_id}")
    directory = resolve_transfer_dir() / transfer_id
    if is_safe_path(directory):
        shutil.rmtree(directory, ignore_errors=False)


def _transfer_sweep() -> int:
    """Delete every transfer whose manifest says it's expired.

    Returns:
        Number of transfers deleted.
    """
    base = resolve_transfer_dir()
    now = _time.time()
    removed = 0
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
            logger.warning("transfer_sweep: corrupt manifest at %s, removing", child)
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
            continue
        if float(m.get("expires_at", 0.0)) <= now:
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    if removed:
        logger.info("transfer_sweep removed %d expired transfer(s)", removed)
    return removed
```

**Step 6.4 — Tests + ruff.** Green.

**Step 6.5 — Commit.**

```bash
git commit -am "feat(transfers): delete_transfer + _transfer_sweep TTL reaper"
```

---

### Task 7 — Wire up tool layer: `tool_agent_send_file`, `tool_agent_fetch_file`, `tool_agent_delete_file`

**Objective:** Add thin adapters in `tools.py` that call `transfers.*`
and produce the ADR-007 body on `agent_send_file`.

**Files:**
- Modify: `src/a2a_mcp_bridge/tools.py`
- Create: `tests/test_tool_transfers.py`

**Step 7.1 — Failing tests.**

```python
"""Tests for tool_agent_send_file / fetch_file / delete_file."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from a2a_mcp_bridge.store import Store


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    s = Store(str(tmp_path / "bus.db"))
    s.init_schema()
    s.upsert_agent("alice")
    s.upsert_agent("bob")
    return s


def test_tool_agent_send_file_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: Store) -> None:
    monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path / "xfer"))
    src = tmp_path / "report.md"
    src.write_text("# Report\n" * 100)

    from a2a_mcp_bridge.tools import tool_agent_send_file

    result = tool_agent_send_file(
        store, caller_id="alice", target="bob",
        file_path=str(src), description="weekly report",
    )
    assert "error" not in result
    assert result["transfer_id"]
    assert result["size"] > 0
    assert result["sha256"]

    # Inbox message body is the ADR-007 JSON
    msgs = store.read_inbox("bob")
    assert len(msgs) == 1
    body = json.loads(msgs[0].body)
    assert body["kind"] == "file_transfer"
    assert body["version"] == 1
    assert body["transfer_id"] == result["transfer_id"]
    assert body["locator"]["scheme"] == "file"


def test_tool_agent_fetch_file_returns_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: Store) -> None:
    monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path / "xfer"))
    src = tmp_path / "r.md"
    src.write_text("hello")

    from a2a_mcp_bridge.tools import tool_agent_send_file, tool_agent_fetch_file

    sent = tool_agent_send_file(store, caller_id="alice", target="bob", file_path=str(src))
    got = tool_agent_fetch_file(store, caller_id="bob", transfer_id=sent["transfer_id"])
    assert got["filename"] == "r.md"
    assert Path(got["path"]).read_text() == "hello"


def test_tool_agent_delete_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: Store) -> None:
    monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path / "xfer"))
    src = tmp_path / "r.md"
    src.write_text("hi")

    from a2a_mcp_bridge.tools import tool_agent_send_file, tool_agent_delete_file, tool_agent_fetch_file

    sent = tool_agent_send_file(store, caller_id="alice", target="bob", file_path=str(src))
    tool_agent_delete_file(store, caller_id="bob", transfer_id=sent["transfer_id"])

    res = tool_agent_fetch_file(store, caller_id="bob", transfer_id=sent["transfer_id"])
    assert "error" in res
```

**Step 7.2 — Run to fail.**

**Step 7.3 — Implement in `tools.py`.**

Add imports at top:

```python
from .transfers import (
    delete_transfer,
    load_manifest,
    resolve_locator_path,
    stage_file,
)
```

Add three functions after the existing `tool_agent_send`:

```python
def tool_agent_send_file(
    store: BusStore,
    caller_id: str,
    target: str,
    file_path: str,
    description: str = "",
    expires_in: int | None = None,
    signal_dir: SignalDir | None = None,
    waker: WebhookWaker | None = None,
    intent: str | None = None,
) -> dict[str, Any]:
    """Stage a local file and send its reference to *target* via A2A.

    Wire protocol: ADR-007 §4.1. The file content never enters either
    LLM's context window — only the reference message does.

    Errors returned as ``{"error": {"code": ..., "message": ...}}``:
        TRANSFER_SOURCE_NOT_FOUND, TRANSFER_TOO_LARGE,
        TRANSFER_QUOTA_EXCEEDED, <delegated from agent_send>.
    """
    from pathlib import Path as _Path

    src = _Path(file_path)
    filename = src.name

    try:
        rec = stage_file(
            src,
            sender_id=caller_id,
            recipient_id=target,
            filename=filename,
            description=description,
            expires_in=expires_in,
        )
    except FileNotFoundError:
        return {"error": {"code": "TRANSFER_SOURCE_NOT_FOUND", "message": str(src)}}
    except ValueError as e:
        code, _, msg = str(e).partition(":")
        return {"error": {"code": code.strip(), "message": msg.strip()}}

    # Build ADR-007 body. expires_at in manifest is epoch; serialise as ISO.
    manifest = load_manifest(rec.transfer_id)
    body_obj = {
        "kind": "file_transfer",
        "version": 1,
        "transfer_id": rec.transfer_id,
        "filename": rec.filename,
        "size": rec.size,
        "sha256": rec.sha256,
        "description": description,
        "expires_at": _iso_utc(manifest["expires_at"]),
        "locator": {"scheme": "file", "path": rec.locator_path},
    }
    import json as _json
    send_result = tool_agent_send(
        store, caller_id, target, _json.dumps(body_obj),
        metadata=None,
        signal_dir=signal_dir,
        waker=waker,
        intent=intent,
    )
    if "error" in send_result:
        # The file is staged but the message failed — surface both.
        return {
            "error": send_result["error"],
            "transfer_id": rec.transfer_id,
            "hint": "file staged but notification failed; caller may retry agent_send",
        }
    return {
        "transfer_id": rec.transfer_id,
        "sha256": rec.sha256,
        "size": rec.size,
        "filename": rec.filename,
        "expires_at": body_obj["expires_at"],
        "message_id": send_result.get("message_id"),
    }


def tool_agent_fetch_file(
    store: BusStore,  # noqa: ARG001 — reserved for Option C
    caller_id: str,
    transfer_id: str,
    verify: bool = True,
) -> dict[str, Any]:
    """Resolve *transfer_id* to a local path for the caller.

    The path is returned verbatim — the LLM tool that actually reads
    the bytes (e.g. ``read_file``) is invoked separately. Validates
    sha256 by default (cost ~50 ms per 100 MB).
    """
    try:
        path = resolve_locator_path(transfer_id, caller_id=caller_id)
        m = load_manifest(transfer_id)
    except FileNotFoundError:
        return {"error": {"code": "TRANSFER_NOT_FOUND", "message": transfer_id}}
    except PermissionError as e:
        return {"error": {"code": "TRANSFER_ACL_DENIED", "message": str(e)}}

    if verify:
        import hashlib as _h
        h = _h.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(64 * 1024), b""):
                h.update(chunk)
        if h.hexdigest() != m["sha256"]:
            return {"error": {"code": "TRANSFER_HASH_MISMATCH", "message": transfer_id}}

    return {
        "transfer_id": transfer_id,
        "path": str(path),
        "size": m["size"],
        "sha256": m["sha256"],
        "filename": m["filename"],
        "description": m.get("description", ""),
        "expires_at": _iso_utc(m["expires_at"]),
    }


def tool_agent_delete_file(
    store: BusStore,  # noqa: ARG001
    caller_id: str,
    transfer_id: str,
) -> dict[str, Any]:
    """Explicit deletion. Caller must be sender or recipient."""
    try:
        delete_transfer(transfer_id, caller_id=caller_id)
    except FileNotFoundError:
        return {"error": {"code": "TRANSFER_NOT_FOUND", "message": transfer_id}}
    except PermissionError as e:
        return {"error": {"code": "TRANSFER_ACL_DENIED", "message": str(e)}}
    return {"deleted": True, "transfer_id": transfer_id}


def _iso_utc(epoch: float) -> str:
    """Return ``epoch`` as ISO-8601 Z string."""
    from datetime import UTC, datetime
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")
```

**Step 7.4 — Tests + ruff.** Green.

**Step 7.5 — Commit.**

```bash
git commit -am "feat(transfers): tool_agent_{send,fetch,delete}_file adapters"
```

---

### Task 8 — Register the three new MCP tools in `server.py`

**Objective:** Add `@mcp.tool()` wrappers so the tools are exposed
over MCP. Integration test via `FastMCP` is deferred — just wire them
up and rely on the `tool_*` test coverage.

**Files:**
- Modify: `src/a2a_mcp_bridge/server.py`

**Step 8.1 — Import the new helpers.**

In `server.py`, update the `from .tools import ...` line to include
`tool_agent_delete_file`, `tool_agent_fetch_file`, `tool_agent_send_file`.

**Step 8.2 — Register the tools.**

After the existing `agent_ping` registration (around line 448), add:

```python
@mcp.tool()
def agent_send_file(
    target: str,
    file_path: str,
    description: str = "",
    expires_in: int | None = None,
    intent: str | None = None,
) -> dict[str, Any]:
    """Send a local file to *target* without loading it into LLM context.

    See ADR-007 for the full protocol. The file is staged under
    ``A2A_TRANSFER_DIR`` and a JSON reference is sent over the A2A
    message bus. Recipient fetches via :func:`agent_fetch_file`.

    Args:
        target: recipient agent_id.
        file_path: absolute path to a local file.
        description: optional human-readable note.
        expires_in: seconds until TTL expiry (default 86400, hard
            cap A2A_TRANSFER_MAX_TTL_SECONDS).
        intent: ADR-002 wake intent (triage / execute / review /
            question / fyi).

    Returns:
        ``{"transfer_id", "sha256", "size", "filename", "expires_at",
        "message_id"}`` on success, ``{"error": {"code", "message"}}``
        otherwise.
    """
    return tool_agent_send_file(
        store, agent_id, target, file_path,
        description=description, expires_in=expires_in,
        signal_dir=signal_dir, waker=_load_waker_if_stale(), intent=intent,
    )


@mcp.tool()
def agent_fetch_file(transfer_id: str, verify: bool = True) -> dict[str, Any]:
    """Resolve *transfer_id* to a local path for this agent.

    Caller must be the declared recipient (or sender). When
    ``verify=True`` (default), sha256 is re-checked — ~50 ms per
    100 MB, negligible vs. silent-corruption risk.
    """
    return tool_agent_fetch_file(store, agent_id, transfer_id, verify=verify)


@mcp.tool()
def agent_delete_file(transfer_id: str) -> dict[str, Any]:
    """Delete a staged transfer. Caller must be sender or recipient."""
    return tool_agent_delete_file(store, agent_id, transfer_id)
```

**Step 8.3 — Run the full suite.**

```bash
.venv/bin/python -m pytest 2>&1 | tail -3
```
Expected: `XXX passed` (previous 255 plus the new transfer tests —
approximately ~275 depending on exact count).

**Step 8.4 — Ruff on everything touched.**

```bash
.venv/bin/ruff check src/a2a_mcp_bridge/ tests/
```
Expected: `All checks passed!`.

**Step 8.5 — Commit.**

```bash
git commit -am "feat(server): register agent_send_file / fetch_file / delete_file"
```

---

### Task 9 — README + version bump

**Objective:** Document the new env vars and tools in `README.md`;
bump version to `0.7.0` in both `pyproject.toml` and
`src/a2a_mcp_bridge/__init__.py`.

**Files:**
- Modify: `README.md`
- Modify: `pyproject.toml`
- Modify: `src/a2a_mcp_bridge/__init__.py`

**Step 9.1 — Env vars table.** In the env-vars table of
`README.md`, add four rows alongside the `A2A_RATE_LIMIT_*` rows:

```markdown
| `A2A_TRANSFER_DIR` | `~/.a2a-transfers` | Staging directory for file transfers (ADR-007). Created with mode 0o700 if missing. |
| `A2A_TRANSFER_DEFAULT_TTL_SECONDS` | `86400` | Default per-transfer TTL (24 h). |
| `A2A_TRANSFER_MAX_TTL_SECONDS` | `604800` | Hard cap on any transfer TTL (7 d). |
| `A2A_TRANSFER_MAX_SIZE_BYTES` | `104857600` | Max file size per transfer (100 MB). |
| `A2A_TRANSFER_MAX_PENDING_PER_AGENT` | `50` | Max un-expired transfers a single sender may have open. |
```

**Step 9.2 — New README section** after the Rate Limiting section:

```markdown
### File transfers (ADR-007)

Starting v0.7.0, agents can hand off arbitrary-size files without
loading their contents into either LLM's context window.

```python
# sender
agent_send_file(target="bob", file_path="/tmp/report.md", description="weekly")
# → {"transfer_id": "...", "sha256": "...", "size": 28845, ...}

# recipient (after the wake-up fires)
inbox = agent_inbox()                           # gets the reference
ref = json.loads(inbox[0]["body"])              # kind=file_transfer
got = agent_fetch_file(ref["transfer_id"])
# → {"path": "/home/vince/.a2a-transfers/<uuid>/<sha>_report.md", ...}

# recipient reads the file with its own read_file tool, then:
agent_delete_file(ref["transfer_id"])
```

Option A ships **same-machine only** — both sender and recipient
must share the filesystem under `A2A_TRANSFER_DIR`. Cross-machine
transfer via the HTTP façade lands in v0.7.1 (Option C).
```

**Step 9.3 — Version bump.**

`pyproject.toml`:
```toml
version = "0.7.0"
```

`src/a2a_mcp_bridge/__init__.py`:
```python
__version__ = "0.7.0"
```

**Step 9.4 — Final verification.**

```bash
.venv/bin/python -m pytest 2>&1 | tail -3
.venv/bin/ruff check src/ tests/
grep -r "0.7.0" pyproject.toml src/a2a_mcp_bridge/__init__.py
```

**Step 9.5 — Commit.**

```bash
git commit -am "docs: document ADR-007 Option A + bump to v0.7.0"
```

---

## Final handoff

When all 9 tasks are green, push the branch and report back on the A2A bus:

```bash
git push -u origin feat/file-transfer-option-a
```

Then A2A message to `vlbeau-opus` with intent `review`:

> PR ready for review: feat/file-transfer-option-a
> - 9 commits, ~300 LOC in src + ~250 LOC tests
> - All tests green (ruff + pytest)
> - Version bumped to 0.7.0
> - [link to branch on GitHub]

**Do not** open the PR yourself — I'll do it after review so the
PR body cites the right commits and reviewer context.

---

## Stop-and-ask triggers (report to vlbeau-opus)

Stop and message `vlbeau-opus` (intent=`question`) if:

- Any existing test breaks that's not related to your changes.
- The ground-truth §"existing" section doesn't match what you see.
- You discover an ambiguity in the spec (wire protocol, error codes,
  env-var names) — don't guess, ask.
- You feel tempted to add a feature not listed in the tasks.
- You need to touch `schema.sql` or `bus_store.py` (you shouldn't).
- A task takes you more than ~15 min — something's wrong.

---

## Out of scope (explicitly NOT part of this PR)

- HTTP transport (Option C) — future v0.7.1.
- `transfers` SQLite table — future v0.7.1.
- Streaming reads — future ADR-007bis.
- Multi-recipient — future.
- Auto-extend on recipient reappearance — open question in ADR-007 §5.3, deferred.
- CLI subcommand for manual sweep — add later if needed.

If you find yourself writing code for any of these, stop and ask.
