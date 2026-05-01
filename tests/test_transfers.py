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


def test_new_transfer_id_is_uuid4(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path))
    import uuid as _uuid

    from a2a_mcp_bridge.transfers import new_transfer_id

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


def test_stage_file_creates_staged_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path))
    src = tmp_path / "source.md"
    src.write_text("hello world\n")

    from a2a_mcp_bridge.transfers import stage_file

    rec = stage_file(src, sender_id="alice", recipient_id="bob", filename="source.md")
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
        stage_file(src, sender_id="alice", recipient_id="bob", filename="big.bin")


def test_stage_file_rejects_source_outside_fs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path))
    from a2a_mcp_bridge.transfers import stage_file

    with pytest.raises(FileNotFoundError):
        stage_file(tmp_path / "ghost.md", sender_id="alice", recipient_id="bob", filename="ghost.md")


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
