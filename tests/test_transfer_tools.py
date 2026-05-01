"""Tests for remote (Phase C) and local (Phase A) file transfer tools."""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from a2a_mcp_bridge.bus_store import HttpBusStore
from a2a_mcp_bridge.store import Store
from a2a_mcp_bridge.tools import (
    tool_agent_delete_file,
    tool_agent_fetch_file,
    tool_agent_send_file,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def local_store(tmp_path: Path) -> Store:
    """Create a real SQLite-backed Store with alice and bob registered."""
    s = Store(str(tmp_path / "bus.db"))
    s.init_schema()
    s.upsert_agent("alice")
    s.upsert_agent("bob")
    return s


@pytest.fixture()
def transfer_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set A2A_TRANSFER_DIR to a tmp directory and return it."""
    td = tmp_path / "xfer"
    monkeypatch.setenv("A2A_TRANSFER_DIR", str(td))
    return td


def _make_mock_http_store() -> MagicMock:
    """Create a MagicMock(spec=HttpBusStore) so isinstance checks pass."""
    mock = MagicMock(spec=HttpBusStore)
    # The tool calls store.upsert_agent — make it a no-op
    mock.upsert_agent = MagicMock()
    # send_message must return something that looks like SendResult for
    # tool_agent_send (called internally by tool_agent_send_file).
    # We patch it so the inner send succeeds.
    from a2a_mcp_bridge.models import SendResult
    from datetime import datetime, UTC

    mock.send_message = MagicMock(
        return_value=SendResult(
            message_id="msg-001",
            sent_at=datetime.now(UTC),
            recipient="bob",
        )
    )
    return mock


# ---------------------------------------------------------------------------
# 1. test_send_file_remote — Phase C (HttpBusStore)
# ---------------------------------------------------------------------------


def test_send_file_remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """tool_agent_send_file with HttpBusStore calls upload_transfer and
    produces an ADR-007 message body with locator.scheme == "http"."""
    mock_store = _make_mock_http_store()

    # upload_transfer returns a dict mimicking the façade response
    mock_store.upload_transfer.return_value = {
        "transfer_id": "remote-tid-123",
        "filename": "report.md",
        "size": 42,
        "sha256": "a" * 64,
        "expires_at": time.time() + 86400,
        "locator": {"url": "https://bus.example.com/transfers/remote-tid-123"},
    }

    # Create a source file
    src = tmp_path / "report.md"
    src.write_text("# Report\n")

    result = tool_agent_send_file(
        mock_store,
        caller_id="alice",
        target="bob",
        file_path=str(src),
        description="weekly report",
    )

    # upload_transfer was called with the right keyword args
    mock_store.upload_transfer.assert_called_once_with(
        file_path=str(src),
        sender_id="alice",
        recipient_id="bob",
        description="weekly report",
        expires_in=None,
    )

    # Result contains transfer metadata
    assert "error" not in result
    assert result["transfer_id"] == "remote-tid-123"
    assert result["sha256"] == "a" * 64
    assert result["size"] == 42

    # The inner send_message was called; inspect the body it received
    mock_store.send_message.assert_called_once()
    call_kwargs = mock_store.send_message.call_args
    body_str = call_kwargs[0][2] if call_kwargs[0] else call_kwargs.kwargs["body"]
    body = json.loads(body_str)
    assert body["kind"] == "file_transfer"
    assert body["version"] == 1
    assert body["locator"]["scheme"] == "http"
    assert body["locator"]["url"] == "https://bus.example.com/transfers/remote-tid-123"


# ---------------------------------------------------------------------------
# 2. test_send_file_local — Phase A (real Store)
# ---------------------------------------------------------------------------


def test_send_file_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_store: Store,
    transfer_dir: Path,
) -> None:
    """tool_agent_send_file with real Store uses stage_file (Phase A) and
    produces an ADR-007 message body with locator.scheme == "file"."""
    src = tmp_path / "report.md"
    src.write_text("# Report\n" * 100)

    result = tool_agent_send_file(
        local_store,
        caller_id="alice",
        target="bob",
        file_path=str(src),
        description="weekly report",
    )

    assert "error" not in result
    assert result["transfer_id"]
    assert result["size"] > 0
    assert result["sha256"]

    # Inbox message body is ADR-007 JSON with scheme=file
    msgs = local_store.read_inbox("bob")
    assert len(msgs) == 1
    body = json.loads(msgs[0].body)
    assert body["kind"] == "file_transfer"
    assert body["version"] == 1
    assert body["transfer_id"] == result["transfer_id"]
    assert body["locator"]["scheme"] == "file"
    assert "path" in body["locator"]


# ---------------------------------------------------------------------------
# 3. test_fetch_file_http_scheme — Phase C download via HttpBusStore
# ---------------------------------------------------------------------------


def test_fetch_file_http_scheme(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transfer_dir: Path,
) -> None:
    """tool_agent_fetch_file with an http-scheme manifest calls
    store.download_transfer."""
    mock_store = _make_mock_http_store()

    # Plant a manifest on disk with locator.scheme == "http"
    tid = "remote-fetch-tid"
    sha = "b" * 64
    manifest_dir = transfer_dir / tid
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "transfer_id": tid,
        "sender_id": "alice",
        "recipient_id": "bob",
        "filename": "data.bin",
        "size": 99,
        "sha256": sha,
        "description": "test fetch",
        "created_at": time.time(),
        "expires_at": time.time() + 86400,
        "locator": {
            "scheme": "http",
            "url": "https://bus.example.com/transfers/remote-fetch-tid",
        },
        "version": 1,
    }
    (manifest_dir / "meta.json").write_text(json.dumps(manifest))

    # download_transfer returns a local path
    downloaded_file = tmp_path / "fetched" / "data.bin"
    downloaded_file.parent.mkdir(parents=True, exist_ok=True)
    downloaded_file.write_bytes(b"x" * 99)
    # sha256 of 99 x's
    import hashlib

    real_sha = hashlib.sha256(b"x" * 99).hexdigest()
    manifest["sha256"] = real_sha
    (manifest_dir / "meta.json").write_text(json.dumps(manifest))

    mock_store.download_transfer.return_value = str(downloaded_file)

    result = tool_agent_fetch_file(
        mock_store,
        caller_id="bob",
        transfer_id=tid,
        verify=True,
    )

    # download_transfer was called
    mock_store.download_transfer.assert_called_once()
    call_args = mock_store.download_transfer.call_args
    assert call_args[0][0] == tid  # positional: transfer_id
    # dest_dir is a temp dir — just confirm it was passed
    assert "dest_dir" in call_args.kwargs or len(call_args[0]) > 1

    assert "error" not in result
    assert result["transfer_id"] == tid
    assert result["filename"] == "data.bin"
    assert result["path"] == str(downloaded_file)


# ---------------------------------------------------------------------------
# 4. test_fetch_file_file_scheme — Phase A local fetch
# ---------------------------------------------------------------------------


def test_fetch_file_file_scheme(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_store: Store,
    transfer_dir: Path,
) -> None:
    """tool_agent_fetch_file with a local (file-scheme) transfer returns the
    staged file path — existing Phase A behaviour."""
    src = tmp_path / "r.md"
    src.write_text("hello")

    sent = tool_agent_send_file(
        local_store,
        caller_id="alice",
        target="bob",
        file_path=str(src),
    )
    got = tool_agent_fetch_file(
        local_store,
        caller_id="bob",
        transfer_id=sent["transfer_id"],
    )
    assert got["filename"] == "r.md"
    assert Path(got["path"]).read_text() == "hello"
    assert "error" not in got


# ---------------------------------------------------------------------------
# 5. test_delete_file_remote — Phase C deletion via HttpBusStore
# ---------------------------------------------------------------------------


def test_delete_file_remote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tool_agent_delete_file with HttpBusStore calls delete_transfer."""
    mock_store = _make_mock_http_store()
    mock_store.delete_transfer.return_value = {
        "deleted": True,
        "transfer_id": "remote-del-tid",
    }

    result = tool_agent_delete_file(
        mock_store,
        caller_id="alice",
        transfer_id="remote-del-tid",
    )

    mock_store.delete_transfer.assert_called_once_with(
        "remote-del-tid",
        caller_id="alice",
    )
    assert result["deleted"] is True
    assert result["transfer_id"] == "remote-del-tid"


# ---------------------------------------------------------------------------
# 6. test_delete_file_local — Phase A local deletion
# ---------------------------------------------------------------------------


def test_delete_file_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_store: Store,
    transfer_dir: Path,
) -> None:
    """tool_agent_delete_file with real Store deletes the local transfer."""
    src = tmp_path / "r.md"
    src.write_text("hi")

    sent = tool_agent_send_file(
        local_store,
        caller_id="alice",
        target="bob",
        file_path=str(src),
    )
    tid = sent["transfer_id"]

    # Delete as bob (recipient)
    del_result = tool_agent_delete_file(
        local_store,
        caller_id="bob",
        transfer_id=tid,
    )
    assert del_result["deleted"] is True
    assert del_result["transfer_id"] == tid

    # Subsequent fetch should fail
    fetch_result = tool_agent_fetch_file(
        local_store,
        caller_id="bob",
        transfer_id=tid,
    )
    assert "error" in fetch_result
