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
