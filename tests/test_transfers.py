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
