"""Integration tests for the file-transfer HTTP endpoints (facade.py).

Covers POST /transfers/upload, GET /transfers/{transfer_id},
and DELETE /transfers/{transfer_id} — including ACL, size limits,
quota, TTL, and path-traversal sanitisation.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from a2a_mcp_bridge.facade import create_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

AUTH_HEADERS = {"Authorization": "Bearer test-key"}


def _make_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    **env_extra: str,
) -> TestClient:
    """Create a TestClient with ``A2A_TRANSFER_DIR`` pointed at *tmp_path*/xfer.

    Extra keyword arguments are set as environment variables **before**
    ``create_app`` is called so that limit constants (max size, max pending,
    max TTL) are picked up from the closure.
    """
    xfer_dir = str(tmp_path / "xfer")
    monkeypatch.setenv("A2A_TRANSFER_DIR", xfer_dir)
    for key, value in env_extra.items():
        monkeypatch.setenv(key, value)
    app = create_app(db_path=str(tmp_path / "bus.db"), api_key="test-key")
    return TestClient(app)


def _upload(
    client: TestClient,
    filename: str = "hello.txt",
    content: bytes = b"hello world",
    sender: str = "alice",
    recipient: str = "bob",
    ttl_hours: int = 24,
):
    """POST a file to /transfers/upload and return the response."""
    return client.post(
        "/transfers/upload",
        files={"file": (filename, content, "application/octet-stream")},
        data={
            "sender": sender,
            "recipient": recipient,
            "ttl_hours": str(ttl_hours),
        },
        headers=AUTH_HEADERS,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTransferEndpoints:

    # -- 1. upload happy path ------------------------------------------------

    def test_upload_happy_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = _make_client(tmp_path, monkeypatch)
        content = b"hello world"
        resp = _upload(client, content=content)

        assert resp.status_code == 200
        data = resp.json()
        assert "transfer_id" in data
        assert data["sha256"] == hashlib.sha256(content).hexdigest()
        assert data["size"] == len(content)
        assert data["filename"] == "hello.txt"
        assert "expires_at" in data

    # -- 2. download happy path ----------------------------------------------

    def test_download_happy_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = _make_client(tmp_path, monkeypatch)
        content = b"download me"
        upload_resp = _upload(client, content=content)
        transfer_id = upload_resp.json()["transfer_id"]

        download_resp = client.get(
            f"/transfers/{transfer_id}",
            headers={**AUTH_HEADERS, "X-Agent-Id": "bob"},
        )

        assert download_resp.status_code == 200
        assert download_resp.content == content

    # -- 3. download ACL denied ----------------------------------------------

    def test_download_acl_denied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = _make_client(tmp_path, monkeypatch)
        upload_resp = _upload(client)
        transfer_id = upload_resp.json()["transfer_id"]

        resp = client.get(
            f"/transfers/{transfer_id}",
            headers={**AUTH_HEADERS, "X-Agent-Id": "eve"},
        )

        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"

    # -- 4. download not found -----------------------------------------------

    def test_download_not_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = _make_client(tmp_path, monkeypatch)

        resp = client.get(
            "/transfers/nonexistent",
            headers={**AUTH_HEADERS, "X-Agent-Id": "bob"},
        )

        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "NOT_FOUND"

    # -- 5. delete by sender -------------------------------------------------

    def test_delete_by_sender(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = _make_client(tmp_path, monkeypatch)
        upload_resp = _upload(client)
        transfer_id = upload_resp.json()["transfer_id"]

        resp = client.delete(
            f"/transfers/{transfer_id}",
            headers={**AUTH_HEADERS, "X-Agent-Id": "alice"},
        )

        assert resp.status_code == 204

    # -- 6. delete by recipient ----------------------------------------------

    def test_delete_by_recipient(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = _make_client(tmp_path, monkeypatch)
        upload_resp = _upload(client)
        transfer_id = upload_resp.json()["transfer_id"]

        resp = client.delete(
            f"/transfers/{transfer_id}",
            headers={**AUTH_HEADERS, "X-Agent-Id": "bob"},
        )

        assert resp.status_code == 204

    # -- 7. delete ACL denied ------------------------------------------------

    def test_delete_acl_denied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = _make_client(tmp_path, monkeypatch)
        upload_resp = _upload(client)
        transfer_id = upload_resp.json()["transfer_id"]

        resp = client.delete(
            f"/transfers/{transfer_id}",
            headers={**AUTH_HEADERS, "X-Agent-Id": "eve"},
        )

        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"

    # -- 8. file too large ---------------------------------------------------

    def test_file_too_large(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A2A_TRANSFER_MAX_SIZE_BYTES=0 → any content exceeds it.
        client = _make_client(tmp_path, monkeypatch, A2A_TRANSFER_MAX_SIZE_BYTES="0")

        resp = _upload(client, content=b"x")

        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"

    # -- 9. quota exceeded ---------------------------------------------------

    def test_quota_exceeded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = _make_client(
            tmp_path, monkeypatch, A2A_TRANSFER_MAX_PENDING_PER_AGENT="1",
        )

        # First upload succeeds.
        first = _upload(client, filename="file1.txt", content=b"a")
        assert first.status_code == 200

        # Second upload from the same sender exceeds the pending quota.
        second = _upload(client, filename="file2.txt", content=b"b")
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "TOO_MANY_PENDING"

    # -- 10. TTL exceeded ----------------------------------------------------

    def test_ttl_exceeded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Set max TTL to 1 hour (3600 seconds), then request 999 hours.
        client = _make_client(
            tmp_path, monkeypatch, A2A_TRANSFER_MAX_TTL_SECONDS="3600",
        )

        resp = _upload(client, ttl_hours=999)

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "TTL_EXCEEDED"

    # -- 11. path-traversal filename sanitised -------------------------------

    def test_path_traversal_filename(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = _make_client(tmp_path, monkeypatch)
        content = b"evil data"

        resp = _upload(client, filename="../../etc/passwd", content=content)

        assert resp.status_code == 200
        # os.path.basename("../../etc/passwd") == "passwd"
        assert resp.json()["filename"] == "passwd"
