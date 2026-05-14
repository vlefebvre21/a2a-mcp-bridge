"""Tests for ADR-008 capability registry centralization.

Covers:
  - Store.register_capability (insert & upsert)
  - Store.get_capabilities (no filter, keyword, cost, combined, ordering)
  - _migrate_legacy_registry (normal, idempotent, no-legacy-file)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from a2a_mcp_bridge.server import _migrate_legacy_registry
from a2a_mcp_bridge.signals import SignalDir
from a2a_mcp_bridge.store import Store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(tmp_path: Path) -> Store:
    """Create a Store with schema initialised and a SignalDir."""
    db_path = str(tmp_path / "a2a-bus.sqlite")
    sig_dir = SignalDir(str(tmp_path / "signals"))
    s = Store(db_path, signal_dir=sig_dir)
    s.init_schema()
    return s


def _count_capabilities(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM capabilities").fetchone()[0]


# ---------------------------------------------------------------------------
# register_capability
# ---------------------------------------------------------------------------

class TestRegisterCapability:
    """Tests for Store.register_capability()."""

    def test_register_capability_inserts_row(self, tmp_path: Path) -> None:
        """Register a capability and verify it appears in the DB."""
        store = _make_store(tmp_path)
        store.register_capability(
            agent_id="agent-a",
            skill_id="translate",
            domain="nlp",
            description="Translates text between languages",
            monetary_cost_usd=0.02,
            tokens_per_call=500,
        )

        rows = store.get_capabilities()
        assert len(rows) == 1
        row = rows[0]
        assert row["agent_id"] == "agent-a"
        assert row["skill_id"] == "translate"
        assert row["domain"] == "nlp"
        assert row["description"] == "Translates text between languages"
        assert row["monetary_cost_usd"] == pytest.approx(0.02)
        assert row["tokens_per_call"] == 500
        assert row["announced_at"] is not None

    def test_register_capability_upsert(self, tmp_path: Path) -> None:
        """Registering the same (agent_id, skill_id) twice uses REPLACE semantics:
        row count stays 1 and values are updated."""
        store = _make_store(tmp_path)

        # First insert
        store.register_capability(
            agent_id="agent-a",
            skill_id="translate",
            domain="nlp",
            description="old description",
            monetary_cost_usd=0.05,
            tokens_per_call=1000,
        )

        # Upsert — same (agent_id, skill_id), different values
        store.register_capability(
            agent_id="agent-a",
            skill_id="translate",
            domain="translation",
            description="new description",
            monetary_cost_usd=0.01,
            tokens_per_call=200,
        )

        rows = store.get_capabilities()
        assert len(rows) == 1, "REPLACE should keep row count at 1"
        row = rows[0]
        assert row["domain"] == "translation"
        assert row["description"] == "new description"
        assert row["monetary_cost_usd"] == pytest.approx(0.01)
        assert row["tokens_per_call"] == 200

    def test_register_capability_defaults(self, tmp_path: Path) -> None:
        """Defaults: domain='general', monetary_cost_usd=NULL, tokens_per_call=0."""
        store = _make_store(tmp_path)
        store.register_capability(agent_id="bob", skill_id="ping")

        rows = store.get_capabilities()
        assert len(rows) == 1
        row = rows[0]
        assert row["domain"] == "general"
        assert row["monetary_cost_usd"] is None
        assert row["tokens_per_call"] == 0
        assert row["description"] is None


# ---------------------------------------------------------------------------
# get_capabilities
# ---------------------------------------------------------------------------

class TestGetCapabilities:
    """Tests for Store.get_capabilities()."""

    @pytest.fixture()
    def _populated_store(self, tmp_path: Path) -> Store:
        """Return a store pre-loaded with several capabilities."""
        store = _make_store(tmp_path)
        store.register_capability("agent-a", "translate", "nlp", "Translate text", 0.02, 500)
        store.register_capability("agent-a", "summarize", "nlp", "Summarize documents", 0.05, 800)
        store.register_capability("agent-b", "image-gen", "vision", "Generate images", 0.10, 2000)
        store.register_capability("agent-b", "code-review", "dev", "Review source code", None, 100)
        store.register_capability("agent-c", "translate", "nlp", "Another translator", 0.01, 300)
        return store

    def test_get_capabilities_no_filter(self, tmp_path: Path, _populated_store: Store) -> None:
        """No filters → returns all registered capabilities."""
        rows = _populated_store.get_capabilities()
        assert len(rows) == 5

    def test_get_capabilities_keyword_filter_skill(self, tmp_path: Path, _populated_store: Store) -> None:
        """Keyword filter matches skill_id."""
        rows = _populated_store.get_capabilities(keyword="translate")
        assert len(rows) == 2
        assert all("translate" in r["skill_id"] for r in rows)

    def test_get_capabilities_keyword_filter_domain(self, tmp_path: Path, _populated_store: Store) -> None:
        """Keyword filter matches domain."""
        rows = _populated_store.get_capabilities(keyword="vision")
        assert len(rows) == 1
        assert rows[0]["skill_id"] == "image-gen"

    def test_get_capabilities_keyword_filter_description(self, tmp_path: Path, _populated_store: Store) -> None:
        """Keyword filter matches description."""
        rows = _populated_store.get_capabilities(keyword="documents")
        assert len(rows) == 1
        assert rows[0]["skill_id"] == "summarize"

    def test_get_capabilities_cost_filter(self, tmp_path: Path, _populated_store: Store) -> None:
        """max_cost_usd filters out rows above the ceiling; NULL cost is included."""
        rows = _populated_store.get_capabilities(max_cost_usd=0.03)
        # translate@0.02, translate@0.01, code-review@NULL → 3 rows
        assert len(rows) == 3
        for r in rows:
            assert r["monetary_cost_usd"] is None or r["monetary_cost_usd"] <= 0.03

    def test_get_capabilities_cost_filter_excludes_null_when_negative(
        self, tmp_path: Path, _populated_store: Store
    ) -> None:
        """max_cost_usd=-1 should include only NULL-cost rows
        (since any real cost >= 0 > -1, but NULL passes the IS NULL branch)."""
        rows = _populated_store.get_capabilities(max_cost_usd=-1)
        assert len(rows) == 1
        assert rows[0]["monetary_cost_usd"] is None

    def test_get_capabilities_combined_filters(self, tmp_path: Path, _populated_store: Store) -> None:
        """keyword + max_cost_usd applied together."""
        rows = _populated_store.get_capabilities(keyword="nlp", max_cost_usd=0.03)
        # translate@0.02 (agent-a), translate@0.01 (agent-c); summarize@0.05 excluded
        assert len(rows) == 2
        for r in rows:
            assert "nlp" in (r["skill_id"], r["domain"], r["description"] or "")
            assert r["monetary_cost_usd"] is None or r["monetary_cost_usd"] <= 0.03

    def test_get_capabilities_ordered(self, tmp_path: Path) -> None:
        """Results ordered by tokens_per_call ASC, announced_at DESC."""
        store = _make_store(tmp_path)
        # Insert with increasing tokens_per_call
        store.register_capability("a", "skill-low", "d1", "lo", 0.01, 100)
        store.register_capability("a", "skill-mid", "d2", "mi", 0.01, 500)
        store.register_capability("a", "skill-high", "d3", "hi", 0.01, 1000)

        rows = store.get_capabilities()
        assert len(rows) == 3
        tokens = [r["tokens_per_call"] for r in rows]
        assert tokens == sorted(tokens), "Should be ordered by tokens_per_call ASC"

    def test_get_capabilities_keyword_no_match(self, tmp_path: Path, _populated_store: Store) -> None:
        """Keyword that matches nothing returns empty list."""
        rows = _populated_store.get_capabilities(keyword="xyz-nonexistent")
        assert rows == []


# ---------------------------------------------------------------------------
# _migrate_legacy_registry
# ---------------------------------------------------------------------------

class TestMigrateLegacyRegistry:
    """Tests for the legacy .registry.db migration helper."""

    def _create_legacy_db(self, legacy_path: Path, rows: list[tuple]) -> None:
        """Create a legacy registry.db with the given rows."""
        conn = sqlite3.connect(str(legacy_path))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS capabilities (
                agent_id TEXT NOT NULL,
                skill_id TEXT NOT NULL,
                domain TEXT DEFAULT 'general',
                description TEXT,
                monetary_cost_usd FLOAT,
                tokens_per_call INTEGER DEFAULT 0,
                announced_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_skill ON capabilities(agent_id, skill_id)"
        )
        for row in rows:
            conn.execute(
                "INSERT OR IGNORE INTO capabilities "
                "(agent_id, skill_id, domain, description, monetary_cost_usd, tokens_per_call) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                row,
            )
        conn.commit()
        conn.close()

    def test_migrate_legacy_registry(self, tmp_path: Path) -> None:
        """Legacy .registry.db rows are copied to bus.sqlite; file renamed to .bak."""
        db_path = str(tmp_path / "a2a-bus.sqlite")
        legacy_path = Path(db_path).with_suffix("")  # strip .sqlite
        legacy_path = Path(str(legacy_path) + ".registry.db")

        self._create_legacy_db(
            legacy_path,
            [
                ("legacy-agent", "old-skill", "legacy", "Legacy capability", 0.03, 400),
                ("legacy-agent", "another-skill", "general", None, None, 0),
            ],
        )

        store = _make_store(tmp_path)
        # The _make_store already called init_schema, but let's use its db_path
        # Re-create store pointing to the right db_path for the migration function
        sig_dir = SignalDir(str(tmp_path / "signals"))
        store = Store(db_path, signal_dir=sig_dir)
        store.init_schema()

        _migrate_legacy_registry(store, db_path)

        # Rows should be in the shared DB now
        rows = store.get_capabilities()
        assert len(rows) == 2
        skill_ids = {r["skill_id"] for r in rows}
        assert skill_ids == {"old-skill", "another-skill"}

        # Legacy file should have been renamed to .bak
        assert not legacy_path.exists()
        assert legacy_path.with_suffix(".registry.db.bak").exists()

    def test_migrate_legacy_registry_idempotent(self, tmp_path: Path) -> None:
        """Running migration twice is safe — no duplicate rows, no errors."""
        db_path = str(tmp_path / "a2a-bus.sqlite")
        legacy_path = Path(db_path).with_suffix("")
        legacy_path = Path(str(legacy_path) + ".registry.db")

        self._create_legacy_db(
            legacy_path,
            [
                ("agent-x", "skill-y", "test", "Desc", 0.01, 100),
            ],
        )

        sig_dir = SignalDir(str(tmp_path / "signals"))
        store = Store(db_path, signal_dir=sig_dir)
        store.init_schema()

        # First migration
        _migrate_legacy_registry(store, db_path)
        # After first migration, legacy file is renamed to .bak, so second call is a no-op
        # at the file-existence check level. Let's verify.
        _migrate_legacy_registry(store, db_path)

        rows = store.get_capabilities()
        assert len(rows) == 1, "Should still have exactly 1 row after double migration"
        assert rows[0]["skill_id"] == "skill-y"

    def test_migrate_legacy_registry_no_legacy_file(self, tmp_path: Path) -> None:
        """If no legacy .registry.db exists, migration is a no-op."""
        db_path = str(tmp_path / "a2a-bus.sqlite")
        sig_dir = SignalDir(str(tmp_path / "signals"))
        store = Store(db_path, signal_dir=sig_dir)
        store.init_schema()

        # Should not raise
        _migrate_legacy_registry(store, db_path)

        rows = store.get_capabilities()
        assert rows == []
