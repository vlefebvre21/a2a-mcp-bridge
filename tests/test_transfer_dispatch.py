"""Tests for v0.7.2 transfer dispatch: A2A_BUS_URL overrides store type.

Bug: agents using Store (direct SQLite) with A2A_BUS_URL set would stage
files locally (Phase A) -- unreachable by remote (NAT'd) recipients.

Fix: when A2A_BUS_URL is in the environment, agent_send_file and
agent_fetch_file always use the facade HTTP endpoint, regardless of
whether the store is Store or HttpBusStore.

C5 coverage: tests 6-13 exercise _facade_upload and _facade_download
directly by mocking urllib.request.urlopen (not the wrapper functions),
ensuring the actual HTTP helper code is covered.
"""
from __future__ import annotations

import hashlib
import io
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from a2a_mcp_bridge.bus_store import HttpBusStore
from a2a_mcp_bridge.store import Store
from a2a_mcp_bridge.tools import (
    _facade_download,
    _facade_upload,
    _FacadeDownloadResult,
    _parse_content_disposition,
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


def _fake_urlopen_response(
    data: bytes = b"",
    headers: dict[str, str] | None = None,
    status: int = 200,
) -> MagicMock:
    """Build a mock that looks like a urllib.response object."""
    resp = MagicMock()
    resp.read = MagicMock(return_value=data)
    resp.headers = MagicMock()
    if headers:
        resp.headers.get = MagicMock(
            side_effect=lambda key, default="", _h=headers: _h.get(key, default)
        )
    else:
        resp.headers.get = MagicMock(return_value="")
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.close = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# 1. test_send_file_store_with_bus_url -> must call _facade_upload
# ---------------------------------------------------------------------------


def test_send_file_store_with_bus_url(
    tmp_path: Path,
    local_store: Store,
    transfer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Store + A2A_BUS_URL set -> _facade_upload is used, NOT stage_file.

    This is the core bug fix: VPS agents using Store (direct SQLite)
    must upload via the facade when A2A_BUS_URL is defined.
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
# 2. test_send_file_store_without_bus_url -> must stage locally (Phase A)
# ---------------------------------------------------------------------------


def test_send_file_store_without_bus_url(
    tmp_path: Path,
    local_store: Store,
    transfer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Store + A2A_BUS_URL NOT set -> local stage_file (Phase A).

    This is the unchanged behaviour: same-VPS agents without a facade
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
# 3. test_send_file_http_store -> must call _facade_upload when A2A_BUS_URL set
# ---------------------------------------------------------------------------


def test_send_file_http_store_with_bus_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HttpBusStore + A2A_BUS_URL set -> _facade_upload is used (priority 1).

    Even when the store is HttpBusStore, A2A_BUS_URL takes precedence
    over isinstance(store, HttpBusStore) -- ensuring a single code path.
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
# 4. test_fetch_file_http_locator_with_bus_url -> must call _facade_download
# ---------------------------------------------------------------------------


def test_fetch_file_http_locator_with_bus_url(
    tmp_path: Path,
    local_store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Store + A2A_BUS_URL set -> _facade_download is used for fetch.

    The receiving agent (e.g. macqwen36 behind NAT) uses Store for
    messages but must download the file from the facade via HTTP.
    """
    monkeypatch.setenv("A2A_BUS_URL", "http://bus.test:8080")
    monkeypatch.setenv("A2A_FACADE_API_KEY", "test-key-789")

    tid = "remote-fetch-tid"
    content = b"remote file content"
    expected_sha = hashlib.sha256(content).hexdigest()

    # Create the file on disk so dl.path.stat() works in fetch_file
    fake_dir = tmp_path / "a2a_fetch_fake"
    fake_dir.mkdir()
    fake_path = fake_dir / "real_name.txt"
    fake_path.write_bytes(content)

    fake_result = _FacadeDownloadResult(
        path=fake_path,
        sha256=expected_sha,
        filename="real_name.txt",
        verified=True,
    )

    with patch("a2a_mcp_bridge.tools._facade_download") as mock_download:
        mock_download.return_value = fake_result

        result = tool_agent_fetch_file(
            local_store,
            caller_id="bob",
            transfer_id=tid,
            verify=True,
        )

        # _facade_download was called with correct args
        mock_download.assert_called_once()
        call_kwargs = mock_download.call_args.kwargs
        assert call_kwargs["url"] == "http://bus.test:8080/transfers/remote-fetch-tid"
        assert call_kwargs["api_key"] == "test-key-789"
        assert call_kwargs["verify"] is True
        # Issue #44: caller_id must be forwarded as agent_id so the façade
        # can enforce the recipient ACL on GET /transfers/<id>.
        assert call_kwargs["agent_id"] == "bob"

    assert "error" not in result
    assert result["transfer_id"] == tid
    assert result["sha256"] == expected_sha
    # C3 fix: filename comes from _FacadeDownloadResult, NOT transfer_id
    assert result["filename"] == "real_name.txt"


# ---------------------------------------------------------------------------
# 5. test_fetch_file_file_locator_without_bus_url -> must read locally
# ---------------------------------------------------------------------------


def test_fetch_file_file_locator_without_bus_url(
    tmp_path: Path,
    local_store: Store,
    transfer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Store + A2A_BUS_URL NOT set -> Phase A local fetch (manifest on disk).

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


# ===========================================================================
# C5: Direct unit tests for _facade_upload and _facade_download
# These mock urllib.request.urlopen (not the wrapper) to exercise the
# actual HTTP helper code and improve coverage.
# ===========================================================================


# ---------------------------------------------------------------------------
# 6. test_facade_upload_success -> POST multipart, return augmented dict
# ---------------------------------------------------------------------------


def test_facade_upload_success(tmp_path: Path) -> None:
    """_facade_upload sends a POST with multipart body and returns the
    facade response augmented with a locator dict."""
    src = tmp_path / "upload_test.txt"
    src.write_text("hello world")

    upload_resp = json.dumps({
        "transfer_id": "tid-upload-001",
        "filename": "upload_test.txt",
        "size": 11,
        "sha256": "d" * 64,
        "expires_at": time.time() + 86400,
    }).encode()

    mock_resp = _fake_urlopen_response(data=upload_resp)

    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = _facade_upload(
            bus_url="http://bus.test:8080",
            api_key="key-123",
            filepath=src,
            sender="alice",
            recipient="bob",
            ttl_hours=24,
        )

    assert result["transfer_id"] == "tid-upload-001"
    assert result["filename"] == "upload_test.txt"
    assert result["size"] == 11
    # Locator was augmented
    assert result["locator"]["scheme"] == "http"
    assert result["locator"]["url"] == "http://bus.test:8080/transfers/tid-upload-001"


# ---------------------------------------------------------------------------
# 7. test_facade_upload_http_errors -> 413, 429, 400, 404
# ---------------------------------------------------------------------------


def test_facade_upload_http_errors(tmp_path: Path) -> None:
    """_facade_upload maps HTTP errors to the correct exception types."""
    import urllib.error

    src = tmp_path / "err.txt"
    src.write_text("data")

    # 413 -> ValueError TRANSFER_TOO_LARGE
    err_413 = urllib.error.HTTPError(
        "http://x", 413, "Too Large", {}, io.BytesIO(b"too big")
    )
    with patch("urllib.request.urlopen", side_effect=err_413), \
         pytest.raises(ValueError, match="TRANSFER_TOO_LARGE"):
        _facade_upload("http://bus", "k", src, "a", "b", 24)

    # 429 -> ValueError TRANSFER_QUOTA_EXCEEDED
    err_429 = urllib.error.HTTPError(
        "http://x", 429, "Rate Limit", {}, io.BytesIO(b"slow down")
    )
    with patch("urllib.request.urlopen", side_effect=err_429), \
         pytest.raises(ValueError, match="TRANSFER_QUOTA_EXCEEDED"):
        _facade_upload("http://bus", "k", src, "a", "b", 24)

    # 400 -> ValueError TRANSFER_BAD_REQUEST
    err_400 = urllib.error.HTTPError(
        "http://x", 400, "Bad Request", {}, io.BytesIO(b"invalid")
    )
    with patch("urllib.request.urlopen", side_effect=err_400), \
         pytest.raises(ValueError, match="TRANSFER_BAD_REQUEST"):
        _facade_upload("http://bus", "k", src, "a", "b", 24)

    # 404 -> FileNotFoundError
    err_404 = urllib.error.HTTPError(
        "http://x", 404, "Not Found", {}, io.BytesIO(b"nope")
    )
    with patch("urllib.request.urlopen", side_effect=err_404), \
         pytest.raises(FileNotFoundError):
        _facade_upload("http://bus", "k", src, "a", "b", 24)


# ---------------------------------------------------------------------------
# 8. test_facade_download_success -> streams file, extracts filename, verifies sha
# ---------------------------------------------------------------------------


def test_facade_download_success(tmp_path: Path) -> None:
    """_facade_download streams to disk, parses Content-Disposition, and
    verifies sha256 against X-Transfer-SHA256 header."""
    content = b"downloaded content"
    expected_sha = hashlib.sha256(content).hexdigest()

    # Simulate a response that streams in chunks
    chunks = [content[:10], content[10:]]

    resp = MagicMock()
    resp.headers = MagicMock()
    resp.headers.get = MagicMock(
        side_effect=lambda key, default="": {
            "Content-Disposition": 'attachment; filename="report.csv"',
            "X-Transfer-SHA256": expected_sha,
        }.get(key, default)
    )
    read_iter = iter([*chunks, b""])

    def _read(size: int = -1) -> bytes:
        try:
            return next(read_iter)
        except StopIteration:
            return b""

    resp.read = _read
    resp.close = MagicMock()

    dest_dir = str(tmp_path / "download")

    with patch("urllib.request.urlopen", return_value=resp):
        result = _facade_download(
            url="http://bus.test:8080/transfers/tid-123",
            api_key="key-456",
            dest_dir=dest_dir,
            verify=True,
        )

    assert isinstance(result, _FacadeDownloadResult)
    assert result.filename == "report.csv"  # C3: real filename from Content-Disposition
    assert result.sha256 == expected_sha
    assert result.verified is True
    # File on disk matches
    assert result.path.read_bytes() == content
    assert result.path.name == "report.csv"


# ---------------------------------------------------------------------------
# 9. test_facade_download_sha_mismatch -> corrupt file rejected
# ---------------------------------------------------------------------------


def test_facade_download_sha_mismatch(tmp_path: Path) -> None:
    """C4: _facade_download raises ValueError when sha256 mismatches
    X-Transfer-SHA256 header. Corrupt data in transit is rejected."""
    content = b"corrupted content"

    resp = MagicMock()
    resp.headers = MagicMock()
    resp.headers.get = MagicMock(
        side_effect=lambda key, default="": {
            "Content-Disposition": 'attachment; filename="bad.bin"',
            "X-Transfer-SHA256": "0" * 64,  # wrong hash
        }.get(key, default)
    )
    resp.read = MagicMock(side_effect=[content, b""])
    resp.close = MagicMock()

    dest_dir = str(tmp_path / "download_bad")

    with patch("urllib.request.urlopen", return_value=resp), \
         pytest.raises(ValueError, match="SHA-256 mismatch"):
        _facade_download(
            url="http://bus.test:8080/transfers/tid-bad",
            api_key="k",
            dest_dir=dest_dir,
            verify=True,
        )


# ---------------------------------------------------------------------------
# 10. test_facade_download_verify_false -> no sha mismatch raised
# ---------------------------------------------------------------------------


def test_facade_download_verify_false(tmp_path: Path) -> None:
    """M1: verify=False skips the sha256 comparison even when header
    is present and wrong."""
    content = b"some data"

    resp = MagicMock()
    resp.headers = MagicMock()
    resp.headers.get = MagicMock(
        side_effect=lambda key, default="": {
            "Content-Disposition": 'attachment; filename="skip.bin"',
            "X-Transfer-SHA256": "0" * 64,  # wrong hash
        }.get(key, default)
    )
    resp.read = MagicMock(side_effect=[content, b""])
    resp.close = MagicMock()

    dest_dir = str(tmp_path / "download_skip")

    with patch("urllib.request.urlopen", return_value=resp):
        result = _facade_download(
            url="http://bus.test:8080/transfers/tid-skip",
            api_key="k",
            dest_dir=dest_dir,
            verify=False,
        )

    # No ValueError raised despite wrong hash
    assert result.filename == "skip.bin"
    assert result.verified is False
    assert result.path.read_bytes() == content


# ---------------------------------------------------------------------------
# 11. test_facade_download_http_errors -> 404, 403, 429
# ---------------------------------------------------------------------------


def test_facade_download_http_errors() -> None:
    """M3: _facade_download maps HTTP errors to specific exception types."""
    import urllib.error

    # 404 -> FileNotFoundError
    err_404 = urllib.error.HTTPError(
        "http://x", 404, "Not Found", {}, io.BytesIO(b"nope")
    )
    with patch("urllib.request.urlopen", side_effect=err_404), \
         pytest.raises(FileNotFoundError):
        _facade_download("http://bus/xfrs/t1", "k", "/tmp/d")

    # 403 -> PermissionError
    err_403 = urllib.error.HTTPError(
        "http://x", 403, "Forbidden", {}, io.BytesIO(b"denied")
    )
    with patch("urllib.request.urlopen", side_effect=err_403), \
         pytest.raises(PermissionError):
        _facade_download("http://bus/xfrs/t1", "k", "/tmp/d")

    # 429 -> ValueError TRANSFER_QUOTA_EXCEEDED
    err_429 = urllib.error.HTTPError(
        "http://x", 429, "Rate Limit", {}, io.BytesIO(b"slow")
    )
    with patch("urllib.request.urlopen", side_effect=err_429), \
         pytest.raises(ValueError, match="TRANSFER_QUOTA_EXCEEDED"):
        _facade_download("http://bus/xfrs/t1", "k", "/tmp/d")


# ---------------------------------------------------------------------------
# 12. test_parse_content_disposition -> various header formats
# ---------------------------------------------------------------------------


def test_parse_content_disposition() -> None:
    """_parse_content_disposition extracts filename from various formats."""
    assert _parse_content_disposition('attachment; filename="report.csv"') == "report.csv"
    assert _parse_content_disposition("attachment; filename=data.bin") == "data.bin"
    assert _parse_content_disposition("attachment") == ""
    assert _parse_content_disposition("") == ""
    assert _parse_content_disposition('inline; filename="my file.txt"') == "my file.txt"


# ---------------------------------------------------------------------------
# 13. test_facade_download_no_content_disposition -> fallback to URL slug
# ---------------------------------------------------------------------------


def test_facade_download_no_content_disposition(tmp_path: Path) -> None:
    """When no Content-Disposition header, filename falls back to URL slug."""
    content = b"no cd header"

    resp = MagicMock()
    resp.headers = MagicMock()
    resp.headers.get = MagicMock(return_value="")  # no headers
    resp.read = MagicMock(side_effect=[content, b""])
    resp.close = MagicMock()

    dest_dir = str(tmp_path / "download_nocd")

    with patch("urllib.request.urlopen", return_value=resp):
        result = _facade_download(
            url="http://bus.test:8080/transfers/tid-nocd",
            api_key="k",
            dest_dir=dest_dir,
            verify=False,
        )

    # Filename fallback from URL slug
    assert result.filename == "tid-nocd"
    assert result.path.name == "tid-nocd"


# ---------------------------------------------------------------------------
# C5 coverage gap fillers
# ---------------------------------------------------------------------------


def test_facade_upload_generic_http_error(tmp_path: Path) -> None:
    """Cover line 404: _facade_upload raises ValueError for unhandled HTTP codes."""
    import urllib.error

    src = tmp_path / "data.bin"
    src.write_bytes(b"x")

    err_500 = urllib.error.HTTPError(
        "http://x", 500, "Internal Server Error", {}, io.BytesIO(b"boom")
    )
    with patch("urllib.request.urlopen", side_effect=err_500), \
         pytest.raises(ValueError, match="upload_transfer HTTP 500"):
        _facade_upload("http://bus", "k", src, "a", "b", 24)


def test_facade_upload_urlerror(tmp_path: Path) -> None:
    """Cover line 405-406: _facade_upload raises ValueError on URLError."""
    import urllib.error

    src = tmp_path / "data.bin"
    src.write_bytes(b"x")

    err = urllib.error.URLError("connection refused")
    with patch("urllib.request.urlopen", side_effect=err), \
         pytest.raises(ValueError, match="upload_transfer network error"):
        _facade_upload("http://bus", "k", src, "a", "b", 24)


def test_facade_download_http_400() -> None:
    """Cover lines 491-492: _facade_download maps 400 to ValueError."""
    import urllib.error

    err_400 = urllib.error.HTTPError(
        "http://x", 400, "Bad Request", {}, io.BytesIO(b"invalid")
    )
    with patch("urllib.request.urlopen", side_effect=err_400), \
         pytest.raises(ValueError, match="TRANSFER_BAD_REQUEST"):
        _facade_download("http://bus/xfrs/t1", "k", "/tmp/d")


def test_facade_download_generic_http_error() -> None:
    """Cover line 496: _facade_download raises ValueError for unhandled HTTP codes."""
    import urllib.error

    err_500 = urllib.error.HTTPError(
        "http://x", 500, "Internal Server Error", {}, io.BytesIO(b"oops")
    )
    with patch("urllib.request.urlopen", side_effect=err_500), \
         pytest.raises(ValueError, match="download HTTP 500"):
        _facade_download("http://bus/xfrs/t1", "k", "/tmp/d")


def test_facade_download_urlerror() -> None:
    """Cover lines 497-498: _facade_download raises ValueError on URLError."""
    import urllib.error

    err = urllib.error.URLError("connection refused")
    with patch("urllib.request.urlopen", side_effect=err), \
         pytest.raises(ValueError, match="download network error"):
        _facade_download("http://bus/xfrs/t1", "k", "/tmp/d")


def test_facade_download_cleanup_on_write_error(tmp_path: Path) -> None:
    """Cover lines 526-532: BaseException during download cleans up temp files."""
    content = b"will fail"

    resp = MagicMock()
    resp.headers = MagicMock()
    resp.headers.get = MagicMock(
        side_effect=lambda key, default="": {
            "Content-Disposition": 'attachment; filename="fail.bin"',
        }.get(key, default)
    )
    resp.read = MagicMock(side_effect=[content, OSError("disk full")])
    resp.close = MagicMock()

    dest_dir = str(tmp_path / "cleanup_test")
    with patch("urllib.request.urlopen", return_value=resp), \
         pytest.raises(OSError, match="disk full"):
        _facade_download(
            url="http://bus.test:8080/transfers/tid-fail",
            api_key="k",
            dest_dir=dest_dir,
            verify=False,
        )


def test_send_file_facade_upload_filenotfound(
    tmp_path: Path, local_store: Store, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover lines 607-608: facade upload FileNotFoundError in send_file."""
    monkeypatch.setenv("A2A_BUS_URL", "http://bus.test:8080")
    monkeypatch.setenv("A2A_FACADE_API_KEY", "k")

    src = tmp_path / "gone.txt"
    src.write_text("data")

    with patch("a2a_mcp_bridge.tools._facade_upload", side_effect=FileNotFoundError("nope")):
        result = tool_agent_send_file(local_store, "alice", "bob", str(src))

    assert result["error"]["code"] == "TRANSFER_SOURCE_NOT_FOUND"


def test_send_file_facade_upload_valueerror(
    tmp_path: Path, local_store: Store, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover lines 609-611: facade upload ValueError in send_file."""
    monkeypatch.setenv("A2A_BUS_URL", "http://bus.test:8080")
    monkeypatch.setenv("A2A_FACADE_API_KEY", "k")

    src = tmp_path / "big.txt"
    src.write_text("data")

    with patch("a2a_mcp_bridge.tools._facade_upload", side_effect=ValueError("TRANSFER_TOO_LARGE: file too big")):
        result = tool_agent_send_file(local_store, "alice", "bob", str(src))

    assert result["error"]["code"] == "TRANSFER_TOO_LARGE"


def test_send_file_facade_send_error(
    tmp_path: Path, local_store: Store, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover line 633: send_result has error after successful facade upload."""
    monkeypatch.setenv("A2A_BUS_URL", "http://bus.test:8080")
    monkeypatch.setenv("A2A_FACADE_API_KEY", "k")

    src = tmp_path / "msg.txt"
    src.write_text("data")

    upload_result = {
        "transfer_id": "tid-se", "filename": "msg.txt", "size": 4,
        "sha256": "a" * 64, "expires_at": time.time() + 86400,
        "locator": {"scheme": "http", "url": "http://bus/transfers/tid-se"},
    }

    with patch("a2a_mcp_bridge.tools._facade_upload", return_value=upload_result), \
         patch("a2a_mcp_bridge.tools.tool_agent_send", return_value={"error": {"code": "TARGET_NOT_FOUND", "message": "nope"}}):
        result = tool_agent_send_file(local_store, "alice", "bob", str(src))

    assert result["error"]["code"] == "TARGET_NOT_FOUND"
    assert result["transfer_id"] == "tid-se"
    assert "hint" in result


def test_send_file_http_store_upload_filenotfound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover lines 657-658: HttpBusStore upload FileNotFoundError."""
    monkeypatch.delenv("A2A_BUS_URL", raising=False)

    mock_store = _make_mock_http_store()
    mock_store.upload_transfer = MagicMock(side_effect=FileNotFoundError("nope"))

    src = tmp_path / "data.bin"
    src.write_bytes(b"x")

    result = tool_agent_send_file(mock_store, "alice", "bob", str(src))
    assert result["error"]["code"] == "TRANSFER_SOURCE_NOT_FOUND"


def test_send_file_http_store_upload_valueerror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover lines 659-661: HttpBusStore upload ValueError."""
    monkeypatch.delenv("A2A_BUS_URL", raising=False)

    mock_store = _make_mock_http_store()
    mock_store.upload_transfer = MagicMock(side_effect=ValueError("TRANSFER_TOO_LARGE: big"))

    src = tmp_path / "data.bin"
    src.write_bytes(b"x")

    result = tool_agent_send_file(mock_store, "alice", "bob", str(src))
    assert result["error"]["code"] == "TRANSFER_TOO_LARGE"


def test_send_file_http_store_send_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover line 683: HttpBusStore send_result error after upload."""
    monkeypatch.delenv("A2A_BUS_URL", raising=False)

    mock_store = _make_mock_http_store()
    mock_store.upload_transfer = MagicMock(return_value={
        "transfer_id": "tid-hb", "filename": "f.bin", "size": 1,
        "sha256": "c" * 64, "expires_at": time.time() + 86400,
        "locator": {"scheme": "http", "url": "http://bus/transfers/tid-hb"},
    })

    src = tmp_path / "data.bin"
    src.write_bytes(b"x")

    with patch("a2a_mcp_bridge.tools.tool_agent_send", return_value={"error": {"code": "TARGET_NOT_FOUND", "message": "nope"}}):
        result = tool_agent_send_file(mock_store, "alice", "bob", str(src))

    assert result["error"]["code"] == "TARGET_NOT_FOUND"
    assert result["transfer_id"] == "tid-hb"


def test_fetch_file_facade_download_notfound(
    tmp_path: Path, local_store: Store, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover line 799-800: facade download FileNotFoundError in fetch."""
    monkeypatch.setenv("A2A_BUS_URL", "http://bus.test:8080")
    monkeypatch.setenv("A2A_FACADE_API_KEY", "k")

    with patch("a2a_mcp_bridge.tools._facade_download", side_effect=FileNotFoundError("nope")):
        result = tool_agent_fetch_file(local_store, "alice", "tid-missing")

    assert result["error"]["code"] == "TRANSFER_NOT_FOUND"


def test_fetch_file_facade_download_permission(
    tmp_path: Path, local_store: Store, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover lines 801-802: facade download PermissionError in fetch."""
    monkeypatch.setenv("A2A_BUS_URL", "http://bus.test:8080")
    monkeypatch.setenv("A2A_FACADE_API_KEY", "k")

    with patch("a2a_mcp_bridge.tools._facade_download", side_effect=PermissionError("denied")):
        result = tool_agent_fetch_file(local_store, "alice", "tid-denied")

    assert result["error"]["code"] == "TRANSFER_ACL_DENIED"


def test_fetch_file_facade_download_hash_mismatch(
    tmp_path: Path, local_store: Store, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover lines 803-804: facade download ValueError (hash mismatch) in fetch."""
    monkeypatch.setenv("A2A_BUS_URL", "http://bus.test:8080")
    monkeypatch.setenv("A2A_FACADE_API_KEY", "k")

    with patch("a2a_mcp_bridge.tools._facade_download", side_effect=ValueError("SHA-256 mismatch")):
        result = tool_agent_fetch_file(local_store, "alice", "tid-bad")

    assert result["error"]["code"] == "TRANSFER_HASH_MISMATCH"


def test_fetch_file_http_store_notfound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover lines 824-825: HttpBusStore download FileNotFoundError."""
    monkeypatch.delenv("A2A_BUS_URL", raising=False)

    mock_store = _make_mock_http_store()
    mock_store.download_transfer = MagicMock(side_effect=FileNotFoundError("nope"))

    result = tool_agent_fetch_file(mock_store, "alice", "tid-gone")
    assert result["error"]["code"] == "TRANSFER_NOT_FOUND"


def test_fetch_file_http_store_permission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover lines 826-827: HttpBusStore download PermissionError."""
    monkeypatch.delenv("A2A_BUS_URL", raising=False)

    mock_store = _make_mock_http_store()
    mock_store.download_transfer = MagicMock(side_effect=PermissionError("denied"))

    result = tool_agent_fetch_file(mock_store, "alice", "tid-denied")
    assert result["error"]["code"] == "TRANSFER_ACL_DENIED"


def test_fetch_file_http_store_hash_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover lines 828-830: HttpBusStore download ValueError (hash mismatch)."""
    monkeypatch.delenv("A2A_BUS_URL", raising=False)

    mock_store = _make_mock_http_store()
    mock_store.download_transfer = MagicMock(side_effect=ValueError("SHA-256 mismatch"))

    result = tool_agent_fetch_file(mock_store, "alice", "tid-bad")
    assert result["error"]["code"] == "TRANSFER_HASH_MISMATCH"


# ---------------------------------------------------------------------------
# Issue #42: facade returns ISO string for expires_at, not epoch float
# ---------------------------------------------------------------------------


def test_send_file_facade_iso_expires_at(
    tmp_path: Path, local_store: Store, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Facade returns expires_at as ISO string — must not crash _iso_utc."""
    monkeypatch.setenv("A2A_BUS_URL", "http://bus.test:8080")
    monkeypatch.setenv("A2A_FACADE_API_KEY", "k")

    src = tmp_path / "doc.txt"
    src.write_text("hello")

    upload_result = {
        "transfer_id": "tid-iso", "filename": "doc.txt", "size": 5,
        "sha256": "a" * 64,
        "expires_at": "2026-05-03T00:00:00+00:00",  # ISO string from facade
        "locator": {"scheme": "http", "url": "http://bus/transfers/tid-iso"},
    }

    with patch("a2a_mcp_bridge.tools._facade_upload", return_value=upload_result):
        result = tool_agent_send_file(local_store, "alice", "bob", str(src))

    assert "error" not in result
    assert result["expires_at"] == "2026-05-03T00:00:00Z"


def test_iso_utc_accepts_string() -> None:
    """_iso_utc passes through ISO strings with +00:00 → Z normalisation."""
    from a2a_mcp_bridge.tools import _iso_utc

    assert _iso_utc("2026-05-03T00:00:00+00:00") == "2026-05-03T00:00:00Z"
    assert _iso_utc("2026-05-03T00:00:00Z") == "2026-05-03T00:00:00Z"


def test_iso_utc_accepts_float() -> None:
    """_iso_utc converts epoch float to ISO Z string."""
    from a2a_mcp_bridge.tools import _iso_utc

    result = _iso_utc(1746230400.0)
    assert result.endswith("Z")
    assert "2025" in result or "2026" in result


# ---------------------------------------------------------------------------
# Issue #42 companion: _rewrite_transfer_url for cross-machine locator
# ---------------------------------------------------------------------------


def test_rewrite_transfer_url_localhost_to_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """127.0.0.1 locator rewritten to A2A_BUS_URL host."""
    from a2a_mcp_bridge.tools import _rewrite_transfer_url

    monkeypatch.setenv("A2A_BUS_URL", "http://46.224.117.9:8080")
    result = _rewrite_transfer_url("http://127.0.0.1:8080/transfers/abc-123")
    assert result == "http://46.224.117.9:8080/transfers/abc-123"


def test_rewrite_transfer_url_same_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Locator already points to A2A_BUS_URL host → unchanged."""
    from a2a_mcp_bridge.tools import _rewrite_transfer_url

    monkeypatch.setenv("A2A_BUS_URL", "http://46.224.117.9:8080")
    result = _rewrite_transfer_url("http://46.224.117.9:8080/transfers/abc-123")
    assert result == "http://46.224.117.9:8080/transfers/abc-123"


def test_rewrite_transfer_url_file_scheme_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """file:// locator is never rewritten, even with A2A_BUS_URL set."""
    from a2a_mcp_bridge.tools import _rewrite_transfer_url

    monkeypatch.setenv("A2A_BUS_URL", "http://46.224.117.9:8080")
    result = _rewrite_transfer_url("file:///tmp/a2a/transfers/abc-123/data.bin")
    assert result == "file:///tmp/a2a/transfers/abc-123/data.bin"


def test_rewrite_transfer_url_no_bus_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """No A2A_BUS_URL → locator returned unchanged."""
    from a2a_mcp_bridge.tools import _rewrite_transfer_url

    monkeypatch.delenv("A2A_BUS_URL", raising=False)
    result = _rewrite_transfer_url("http://127.0.0.1:8080/transfers/abc-123")
    assert result == "http://127.0.0.1:8080/transfers/abc-123"


# ---------------------------------------------------------------------------
# Issue #44: _facade_download must send X-Agent-Id header so the façade
# can enforce the recipient ACL on GET /transfers/<id>.
# ---------------------------------------------------------------------------


def test_facade_download_sends_x_agent_id_header(tmp_path: Path) -> None:
    """When agent_id is provided, _facade_download sets the X-Agent-Id header
    on the urllib Request, so the façade can match it against recipient_id.
    """
    content = b"tiny"
    expected_sha = hashlib.sha256(content).hexdigest()

    resp = MagicMock()
    resp.headers = MagicMock()
    resp.headers.get = MagicMock(
        side_effect=lambda key, default="": {
            "Content-Disposition": 'attachment; filename="f.txt"',
            "X-Transfer-SHA256": expected_sha,
        }.get(key, default)
    )
    read_iter = iter([content, b""])
    resp.read = lambda size=-1: next(read_iter, b"")
    resp.close = MagicMock()

    captured: dict = {}

    def _fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.headers)
        return resp

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        _facade_download(
            url="http://bus.test:8080/transfers/tid-xyz",
            api_key="key-abc",
            dest_dir=str(tmp_path / "dl"),
            verify=True,
            agent_id="vlbeau-macqwen36",
        )

    # urllib normalises header keys to Title-Case.
    assert captured["headers"].get("Authorization") == "Bearer key-abc"
    assert captured["headers"].get("X-agent-id") == "vlbeau-macqwen36"


def test_facade_download_omits_x_agent_id_when_empty(tmp_path: Path) -> None:
    """When agent_id='' (default), the X-Agent-Id header is NOT emitted.

    Preserves backward compatibility for any caller that relied on the
    old signature (no recipient-ACL transfers).
    """
    content = b"tiny"
    expected_sha = hashlib.sha256(content).hexdigest()

    resp = MagicMock()
    resp.headers = MagicMock()
    resp.headers.get = MagicMock(
        side_effect=lambda key, default="": {
            "Content-Disposition": 'attachment; filename="f.txt"',
            "X-Transfer-SHA256": expected_sha,
        }.get(key, default)
    )
    read_iter = iter([content, b""])
    resp.read = lambda size=-1: next(read_iter, b"")
    resp.close = MagicMock()

    captured: dict = {}

    def _fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.headers)
        return resp

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        _facade_download(
            url="http://bus.test:8080/transfers/tid-xyz",
            api_key="key-abc",
            dest_dir=str(tmp_path / "dl"),
            verify=True,
            # agent_id omitted -> default ""
        )

    assert captured["headers"].get("Authorization") == "Bearer key-abc"
    assert "X-agent-id" not in captured["headers"]
