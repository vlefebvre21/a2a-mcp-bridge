"""Tests for the CLI (uses Typer's test runner, no subprocess)."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from a2a_mcp_bridge.cli import app
from a2a_mcp_bridge.store import Store

runner = CliRunner()


def test_init_creates_db(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite"
    result = runner.invoke(app, ["init", "--db", str(db)])
    assert result.exit_code == 0
    assert "initialised" in result.stdout
    assert db.exists()


def test_agents_list_empty(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite"
    runner.invoke(app, ["init", "--db", str(db)])
    result = runner.invoke(app, ["agents", "list", "--db", str(db)])
    assert result.exit_code == 0
    assert "No active agents" in result.stdout


def test_agents_list_with_data(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite"
    store = Store(str(db))
    store.init_schema()
    store.upsert_agent("alice")
    store.upsert_agent("bob")
    store.close()

    result = runner.invoke(app, ["agents", "list", "--db", str(db)])
    assert result.exit_code == 0
    assert "alice" in result.stdout
    assert "bob" in result.stdout


def test_messages_tail_empty(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite"
    runner.invoke(app, ["init", "--db", str(db)])
    result = runner.invoke(app, ["messages", "tail", "--db", str(db)])
    assert result.exit_code == 0
    # Table header must render even when empty
    assert "messages" in result.stdout.lower()


def test_messages_tail_with_data(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite"
    store = Store(str(db))
    store.init_schema()
    store.upsert_agent("alice")
    store.upsert_agent("bob")
    store.send_message("alice", "bob", "hello-from-tail")
    store.close()

    result = runner.invoke(app, ["messages", "tail", "--db", str(db)])
    assert result.exit_code == 0
    assert "alice" in result.stdout
    assert "bob" in result.stdout


def test_help_shows_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "serve" in result.stdout
    assert "agents" in result.stdout
    assert "messages" in result.stdout


def test_messages_purge_dry_run(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite"
    store = Store(str(db))
    store.init_schema()
    store.upsert_agent("alice")
    store.upsert_agent("bob")

    # Insert old messages via raw SQL (2024 timestamps)
    for i in range(3):
        store._conn.execute(
            "INSERT INTO messages (id, sender_id, recipient_id, body, "
            "metadata, created_at, read_at, sender_session_id, intent) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"old_{i}", "alice", "bob", "old msg",
                "{}", "2024-01-01T00:00:00+00:00",
                "2024-01-01T01:00:00+00:00", None, "triage",
            ),
        )
    # Insert a recent message that should NOT be counted
    store.send_message("alice", "bob", "recent msg")
    store.close()

    result = runner.invoke(
        app,
        ["messages", "purge", "--db", str(db), "--dry-run", "--older-than-days", "30"],
    )
    assert result.exit_code == 0
    assert "DRY RUN" in result.stdout
    assert "3" in result.stdout  # 3 old messages would be deleted

    # Verify nothing was actually deleted
    store2 = Store(str(db))
    store2.init_schema()
    count = store2._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == 4  # 3 old + 1 recent
    store2.close()
