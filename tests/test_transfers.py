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
