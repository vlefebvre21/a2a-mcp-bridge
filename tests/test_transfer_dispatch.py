"""Tests for v0.7.2 transfer dispatch: A2A_BUS_URL overrides store type.

Bug: agents using Store (direct SQLite) with A2A_BUS_URL set would stage
files locally (Phase A) — unreachable by remote (NAT'd) recipients.

Fix: when A2A_BUS_URL is in the environment, agent_send_file and
agent_fetch_file always use the façade HTTP endpoint, regardless of
whether the store is Store or HttpBusStore.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from a2a_mcp_bridge.bus_store import HttpBusStore
from a2a_mcp_bridge.store import Store
from a2a_mcp_bridge.tools import (
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
    mock.upsert_agent = MagicMock()
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
# 1. test_send_file_store_with_bus_url → must call _facade_upload
# ---------------------------------------------------------------------------


def test_send_file_store_with_bus_url(
    tmp_path: Path,
    local_store: Store,
    transfer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Store + A2A_BUS_URL set → _facade_upload is used, NOT stage_file.

    This is the core bug fix: VPS agents using Store (direct SQLite)
    must upload via the façade when A2A_BUS_URL is defined.
    """
    monkeypatch.setenv("A2A_BUS_URL", "http://bus.test:8080")
    monkeypatch.setenv("A2A_FACADE_API_KEY", "test-key-123")

    src = tmp_path / "report.md"
    src.write_text("# Report\n")

    upload_response = {
        "transfer_id": "facade-tid-001",
        "filename": "report.md",
        "size": 10,
        "sha256": "b" * 64,
        "expires_at": time.time() + 86400,
    }

    with patch("a2a_mcp_bridge.tools._facade_upload") as mock_upload:
        mock_upload.return_value = {
            **upload_response,
            "locator": {
                "scheme": "http",
                "url": "http://bus.test:8080/transfers/facade-tid-001",
            },
        }

        result = tool_agent_send_file(
            local_store,
            caller_id="alice",
            target="bob",
            file_path=str(src),
            description="via facade",
        )

        # _facade_upload was called (not stage_file)
        mock_upload.assert_called_once()
        call_kwargs = mock_upload.call_args.kwargs
        assert call_kwargs["bus_url"] == "http://bus.test:8080"
        assert call_kwargs["api_key"] == "test-key-123"
        assert call_kwargs["sender"] == "alice"
        assert call_kwargs["recipient"] == "bob"

    # Result has facade transfer_id, NOT a local one
    assert "error" not in result
    assert result["transfer_id"] == "facade-tid-001"
    assert result["sha256"] == "b" * 64

    # The message body in the inbox must have locator.scheme == "http"
    msgs = local_store.read_inbox("bob")
    assert len(msgs) == 1
    body = json.loads(msgs[0].body)
    assert body["locator"]["scheme"] == "http"
    assert body["locator"]["url"] == "http://bus.test:8080/transfers/facade-tid-001"


# ---------------------------------------------------------------------------
# 2. test_send_file_store_without_bus_url → must stage locally (Phase A)
# ---------------------------------------------------------------------------


def test_send_file_store_without_bus_url(
    tmp_path: Path,
    local_store: Store,
    transfer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Store + A2A_BUS_URL NOT set → local stage_file (Phase A).

    This is the unchanged behaviour: same-VPS agents without a façade
    continue to use local staging.
    """
    # Ensure A2A_BUS_URL is NOT set
    monkeypatch.delenv("A2A_BUS_URL", raising=False)

    src = tmp_path / "local.txt"
    src.write_text("local content")

    result = tool_agent_send_file(
        local_store,
        caller_id="alice",
        target="bob",
        file_path=str(src),
    )

    assert "error" not in result
    assert result["transfer_id"]
    assert result["size"] > 0

    # Locator must be file scheme
    msgs = local_store.read_inbox("bob")
    assert len(msgs) == 1
    body = json.loads(msgs[0].body)
    assert body["locator"]["scheme"] == "file"
    assert "path" in body["locator"]


# ---------------------------------------------------------------------------
# 3. test_send_file_http_store → must call _facade_upload when A2A_BUS_URL set
# ---------------------------------------------------------------------------


def test_send_file_http_store_with_bus_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HttpBusStore + A2A_BUS_URL set → _facade_upload is used (priority 1).

    Even when the store is HttpBusStore, A2A_BUS_URL takes precedence
    over isinstance(store, HttpBusStore) — ensuring a single code path.
    """
    monkeypatch.setenv("A2A_BUS_URL", "http://bus.test:8080")
    monkeypatch.setenv("A2A_FACADE_API_KEY", "test-key-456")

    mock_store = _make_mock_http_store()

    src = tmp_path / "data.bin"
    src.write_bytes(b"\x00\x01\x02")

    with patch("a2a_mcp_bridge.tools._facade_upload") as mock_upload:
        mock_upload.return_value = {
            "transfer_id": "facade-tid-002",
            "filename": "data.bin",
            "size": 3,
            "sha256": "c" * 64,
            "expires_at": time.time() + 86400,
            "locator": {
                "scheme": "http",
                "url": "http://bus.test:8080/transfers/facade-tid-002",
            },
        }

        result = tool_agent_send_file(
            mock_store,
            caller_id="alice",
            target="bob",
            file_path=str(src),
        )

        # _facade_upload was called, NOT store.upload_transfer
        mock_upload.assert_called_once()
        mock_store.upload_transfer.assert_not_called()

    assert "error" not in result
    assert result["transfer_id"] == "facade-tid-002"


# ---------------------------------------------------------------------------
# 4. test_fetch_file_http_locator_with_bus_url → must call _facade_download
# ---------------------------------------------------------------------------


def test_fetch_file_http_locator_with_bus_url(
    tmp_path: Path,
    local_store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Store + A2A_BUS_URL set → _facade_download is used for fetch.

    The receiving agent (e.g. macqwen36 behind NAT) uses Store for
    messages but must download the file from the façade via HTTP.
    """
    monkeypatch.setenv("A2A_BUS_URL", "http://bus.test:8080")
    monkeypatch.setenv("A2A_FACADE_API_KEY", "test-key-789")

    tid = "remote-fetch-tid"
    content = b"remote file content"
    expected_sha = hashlib.sha256(content).hexdigest()

    with patch("a2a_mcp_bridge.tools._facade_download") as mock_download:
        # _facade_download writes the file and returns sha256
        def _fake_download(url: str, api_key: str, dest: Path) -> str:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
            return expected_sha

        mock_download.side_effect = _fake_download

        result = tool_agent_fetch_file(
            local_store,
            caller_id="bob",
            transfer_id=tid,
            verify=True,
        )

        # _facade_download was called
        mock_download.assert_called_once()
        call_kwargs = mock_download.call_args.kwargs
        assert call_kwargs["url"] == "http://bus.test:8080/transfers/remote-fetch-tid"
        assert call_kwargs["api_key"] == "test-key-789"

    assert "error" not in result
    assert result["transfer_id"] == tid
    assert result["sha256"] == expected_sha
    assert result["size"] == len(content)
    # File exists on disk
    assert Path(result["path"]).exists()
    assert Path(result["path"]).read_bytes() == content


# ---------------------------------------------------------------------------
# 5. test_fetch_file_file_locator_without_bus_url → must read locally
# ---------------------------------------------------------------------------


def test_fetch_file_file_locator_without_bus_url(
    tmp_path: Path,
    local_store: Store,
    transfer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Store + A2A_BUS_URL NOT set → Phase A local fetch (manifest on disk).

    Non-regression: same-machine transfers continue to use the local
    staging directory.
    """
    monkeypatch.delenv("A2A_BUS_URL", raising=False)

    src = tmp_path / "local_data.txt"
    src.write_text("local data here")

    # Send first to stage the file
    sent = tool_agent_send_file(
        local_store,
        caller_id="alice",
        target="bob",
        file_path=str(src),
    )
    assert "error" not in sent

    # Fetch must use local manifest (Phase A)
    with patch("a2a_mcp_bridge.tools._facade_download") as mock_download:
        result = tool_agent_fetch_file(
            local_store,
            caller_id="bob",
            transfer_id=sent["transfer_id"],
        )
        # _facade_download must NOT be called
        mock_download.assert_not_called()

    assert "error" not in result
    assert result["filename"] == "local_data.txt"
    assert Path(result["path"]).read_text() == "local data here"
