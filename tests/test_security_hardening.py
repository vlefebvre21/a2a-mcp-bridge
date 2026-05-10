"""Regression tests for security hardenings C-01, C-02, C-04.

Each test name explicitly references its hardening ID so a future grep
for ``C-01`` / ``C-02`` / ``C-04`` lands on its guard. Do not delete
these without updating ``security_audit.md``.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from a2a_mcp_bridge.store import Store, _validate_column_type, _validate_sql_identifier
from a2a_mcp_bridge.transfers import is_safe_path, stage_file
from a2a_mcp_bridge.wake import _is_safe_url

# ---------------------------------------------------------------------------
# C-01 — null bytes & control chars rejected by is_safe_path
# ---------------------------------------------------------------------------


class TestC01PathValidation:
    """C-01: is_safe_path() rejects null bytes and ASCII control chars.

    Null bytes can truncate paths inside libc/sqlite/logging layers and
    bypass naive substring filters. Control chars (< 0x20) can break log
    parsers and terminal output. Both are rejected before any FS syscall.
    """

    def test_rejects_null_byte(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path))
        assert is_safe_path(f"{tmp_path}/foo\x00bar") is False

    def test_rejects_newline(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path))
        assert is_safe_path(f"{tmp_path}/foo\nbar") is False

    def test_rejects_carriage_return(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path))
        assert is_safe_path(f"{tmp_path}/foo\rbar") is False

    def test_rejects_tab(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path))
        # Tab is 0x09 — below 0x20, so rejected.
        assert is_safe_path(f"{tmp_path}/foo\tbar") is False

    def test_rejects_low_ascii_byte(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path))
        # Bell char (0x07) — should be rejected.
        assert is_safe_path(f"{tmp_path}/foo\x07bar") is False

    def test_accepts_normal_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sanity check: ordinary paths still work."""
        monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path))
        assert is_safe_path(tmp_path / "abc" / "file.md") is True

    def test_accepts_unicode_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sanity check: non-ASCII >= 0x20 must NOT be rejected."""
        monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path))
        assert is_safe_path(tmp_path / "café" / "résumé.md") is True


# ---------------------------------------------------------------------------
# C-02 — SQL identifier whitelist (defense-in-depth)
# ---------------------------------------------------------------------------


class TestC02SqlIdentifierValidation:
    """C-02: _validate_sql_identifier() enforces ``[a-zA-Z_][a-zA-Z0-9_]*``.

    Even though ``Store.ensure_column`` gates on ``_KNOWN_TABLES``, we
    re-validate the identifier through the regex before string-formatting
    it into a PRAGMA / ALTER statement. This catches future code paths
    that bypass the whitelist or are introduced by a regression.
    """

    @pytest.mark.parametrize(
        "good",
        ["agents", "messages", "_internal", "table1", "Foo_Bar_42", "_"],
    )
    def test_accepts_valid_identifier(self, good: str) -> None:
        # Should not raise.
        _validate_sql_identifier(good)

    @pytest.mark.parametrize(
        "bad",
        [
            "",                          # empty
            "1agents",                   # starts with digit
            "agents; DROP TABLE x",      # SQL injection
            "agents--",                  # SQL comment
            "agents'",                   # quote
            'agents"',                   # double quote
            "agents OR 1=1",             # space + SQL
            "agents.column",             # dot
            "agents`",                   # backtick
            "agents\x00",                # null byte
            "agents\n",                  # newline
            "agents/*evil*/",            # block comment
            "agents-bad",                # hyphen
        ],
    )
    def test_rejects_invalid_identifier(self, bad: str) -> None:
        with pytest.raises(ValueError, match="invalid"):
            _validate_sql_identifier(bad)

    def test_ensure_column_helper_exists(self, store: Store) -> None:
        """Smoke test: the validate_sql_identifier helper is wired in.

        We don't reach the regex through ``_add_column_if_missing`` because
        the ``_KNOWN_TABLES`` whitelist intercepts bad table names first —
        which is exactly the layered defense we want. This test just
        confirms the helper is importable and callable from the same module.
        """
        from a2a_mcp_bridge.store import _validate_sql_identifier as v

        v("agents")  # sanity
        with pytest.raises(ValueError):
            v("agents; DROP")


# ---------------------------------------------------------------------------
# C-04 — staged file & manifest hardened to 0o600
# ---------------------------------------------------------------------------


class TestC04FilePermissions:
    """C-04: stage_file() writes both payload and manifest with mode 0o600.

    Default umask on shared hosts can leave files world-readable. Forcing
    0o600 ensures only the staging user can read transferred payloads
    (which may contain secrets, screenshots, etc.).
    """

    def test_staged_payload_is_0o600(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path))
        src = tmp_path / "source.md"
        src.write_text("secret\n")

        rec = stage_file(src, sender_id="alice", recipient_id="bob", filename="source.md")

        mode = os.stat(rec.locator_path).st_mode & 0o777
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"

    def test_manifest_is_0o600(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path))
        src = tmp_path / "source.md"
        src.write_text("secret\n")

        rec = stage_file(src, sender_id="alice", recipient_id="bob", filename="source.md")

        manifest_path = tmp_path / rec.transfer_id / "meta.json"
        assert manifest_path.is_file()
        mode = os.stat(manifest_path).st_mode & 0o777
        assert mode == 0o600, f"expected 0o600 on manifest, got {oct(mode)}"

    def test_perms_resist_loose_umask(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with a lax umask (0o000 → world-writable default), final files are 0o600."""
        monkeypatch.setenv("A2A_TRANSFER_DIR", str(tmp_path))
        src = tmp_path / "source.md"
        src.write_text("secret\n")

        old_umask = os.umask(0o000)
        try:
            rec = stage_file(src, sender_id="alice", recipient_id="bob", filename="source.md")
        finally:
            os.umask(old_umask)

        payload_mode = os.stat(rec.locator_path).st_mode & 0o777
        manifest_mode = os.stat(tmp_path / rec.transfer_id / "meta.json").st_mode & 0o777
        assert payload_mode == 0o600, f"payload {oct(payload_mode)}"
        assert manifest_mode == 0o600, f"manifest {oct(manifest_mode)}"


# ---------------------------------------------------------------------------
# C-003 — SQL column_type validation (defense-in-depth for ALTER TABLE)
# ---------------------------------------------------------------------------


class TestC03ColumnTypeValidation:
    """C-003: _validate_column_type() blocks SQL injection in DDL clauses."""

    @pytest.mark.parametrize(
        "good",
        [
            "TEXT",
            "TEXT NOT NULL",
            "TEXT NOT NULL DEFAULT 'triage'",
            "INTEGER",
            "VARCHAR(255)",
            "REAL DEFAULT 0.0",
        ],
    )
    def test_accepts_valid_column_type(self, good: str) -> None:
        # Should not raise.
        _validate_column_type(good)

    @pytest.mark.parametrize(
        "bad",
        [
            "",                              # empty
            "TEXT; DROP TABLE agents",       # statement terminator
            "TEXT--",                        # line comment
            "TEXT/*evil*/",                  # block comment
            "TEXT\x00",                      # null byte
            "TEXT\n",                        # newline
            "TEXT\r",                        # carriage return
            "TEXT\"",                        # double quote
            "TEXT`",                         # backtick
            "TEXT = 1 OR 1=1",               # equals / boolean (not whitelisted)
        ],
    )
    def test_rejects_malicious_column_type(self, bad: str) -> None:
        with pytest.raises(ValueError, match="column_type"):
            _validate_column_type(bad)

    def test_wired_in_add_column_if_missing(self, store: Store) -> None:
        """Smoke test: _validate_column_type is enforced before ALTER TABLE."""
        # Normal usage must succeed (no-op because column already exists).
        store._add_column_if_missing(
            table="messages",
            column="intent",
            column_type="TEXT NOT NULL DEFAULT 'triage'",
        )

        # Malicious type must raise before any DB call.
        with pytest.raises(ValueError, match="invalid column_type"):
            store._add_column_if_missing(
                table="messages",
                column="evil_col",
                column_type="TEXT; DROP TABLE agents",
            )


# ---------------------------------------------------------------------------
# C-004 — SSRF protection on wake webhook URLs
# ---------------------------------------------------------------------------


class TestC04SsrfProtection:
    """C-004: _is_safe_url() blocks internal IPs and non-HTTP(S) schemes."""

    @pytest.mark.parametrize(
        "good",
        [
            "http://example.com/webhooks/wake",
            "https://bus.example.com/wake",
            "http://1.2.3.4:8080/wake",
        ],
    )
    def test_accepts_public_url(self, good: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("A2A_ALLOW_INTERNAL_WEBHOOKS", raising=False)
        assert _is_safe_url(good) is True

    @pytest.mark.parametrize(
        "bad",
        [
            "http://127.0.0.1:8080/wake",           # loopback
            "http://10.0.0.1/wake",                 # private
            "http://172.16.0.1/wake",               # private
            "http://192.168.1.1/wake",              # private
            "http://169.254.169.254/latest/meta-data",  # metadata endpoint
            "http://[::1]/wake",                    # IPv6 loopback
            "file:///etc/passwd",                   # wrong scheme
            "ftp://1.2.3.4/wake",                   # wrong scheme
            "",                                      # empty
            "gopher://1.2.3.4",                     # wrong scheme
        ],
    )
    def test_rejects_internal_or_bad_scheme(self, bad: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("A2A_ALLOW_INTERNAL_WEBHOOKS", raising=False)
        assert _is_safe_url(bad) is False

    def test_override_env_allows_internal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A2A_ALLOW_INTERNAL_WEBHOOKS=1 bypasses the restriction."""
        monkeypatch.setenv("A2A_ALLOW_INTERNAL_WEBHOOKS", "1")
        assert _is_safe_url("http://127.0.0.1:8080/wake") is True
        assert _is_safe_url("http://10.0.0.1/wake") is True
