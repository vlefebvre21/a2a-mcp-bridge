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
    from datetime import UTC, datetime

    from a2a_mcp_bridge.models import SendResult

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
) -> None:
    """tool_agent_fetch_file with HttpBusStore downloads via
    store.download_transfer — no local meta.json needed (Phase C).

    This test verifies the cross-host scenario: the receiving agent
    (on the Mac) does NOT have a local meta.json for the transfer.
    The HttpBusStore short-circuits load_manifest() and goes directly
    to download_transfer(), which queries the façade's TransferStore.

    The mock respects the real contract of download_transfer:
    it writes the file into dest_dir (not some external path),
    exactly as bus_store.py line 458 does:
        dest_path = Path(dest_dir) / filename
    """
    mock_store = _make_mock_http_store()
    tid = "remote-fetch-tid"
    content = b"x" * 99
    import hashlib

    real_sha = hashlib.sha256(content).hexdigest()
    filename = "data.bin"

    def _download(transfer_id: str, dest_dir: str = "") -> str:
        """Mock that respects the download_transfer contract:
        writes the file into dest_dir/filename and returns the path."""
        dest_path = Path(dest_dir) / filename
        dest_path.write_bytes(content)
        return str(dest_path)

    mock_store.download_transfer.side_effect = _download

    result = tool_agent_fetch_file(
        mock_store,
        caller_id="bob",
        transfer_id=tid,
        verify=True,
    )

    # download_transfer was called — no local meta.json was consulted
    mock_store.download_transfer.assert_called_once()
    call_args = mock_store.download_transfer.call_args
    assert call_args[0][0] == tid  # positional: transfer_id
    assert "dest_dir" in call_args.kwargs or len(call_args[0]) > 1

    assert "error" not in result
    assert result["transfer_id"] == tid
    assert result["filename"] == filename
    assert result["sha256"] == real_sha
    assert result["size"] == 99

    # C2 non-regression: the returned path must point to an existing file
    # (TemporaryDirectory would have deleted it; mkdtemp preserves it).
    assert Path(result["path"]).exists()
    assert Path(result["path"]).read_bytes() == content


# ---------------------------------------------------------------------------
# 3b. test_fetch_file_http_no_local_manifest — C1 non-regression
# ---------------------------------------------------------------------------


def test_fetch_file_http_no_local_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transfer_dir: Path,
) -> None:
    """C1 non-regression: HttpBusStore fetch must succeed even when no
    local meta.json exists for the transfer_id.

    Before the fix, tool_agent_fetch_file called load_manifest() FIRST,
    which reads the local meta.json.  On a cross-host receiver (e.g. Mac)
    there is no local staging dir, so load_manifest() raised
    FileNotFoundError → TRANSFER_NOT_FOUND.  The fix short-circuits:
    HttpBusStore → download_transfer() directly, no manifest lookup.

    The mock respects the real contract of download_transfer:
    it writes the file into dest_dir/filename (not an external path).
    """
    mock_store = _make_mock_http_store()
    tid = "cross-host-tid"

    # Intentionally do NOT create a meta.json in transfer_dir — this
    # is exactly the cross-host scenario (Mac has no local staging).
    assert not (transfer_dir / tid / "meta.json").exists()

    content = "a,b,c\n1,2,3\n"
    filename = "payload.csv"

    def _download(transfer_id: str, dest_dir: str = "") -> str:
        """Mock that respects the download_transfer contract:
        writes the file into dest_dir/filename and returns the path."""
        dest_path = Path(dest_dir) / filename
        dest_path.write_text(content)
        return str(dest_path)

    mock_store.download_transfer.side_effect = _download

    result = tool_agent_fetch_file(
        mock_store,
        caller_id="bob",
        transfer_id=tid,
        verify=False,
    )

    assert "error" not in result
    assert result["transfer_id"] == tid
    assert result["filename"] == filename

    # C2 non-regression: returned path must exist on disk
    assert Path(result["path"]).exists()
    assert Path(result["path"]).read_text() == content


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
