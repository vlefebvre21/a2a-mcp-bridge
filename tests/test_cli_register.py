"""Tests for the `register` CLI command (v0.2 Feature 1)."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from a2a_mcp_bridge.cli import app
from a2a_mcp_bridge.store import Store

runner = CliRunner()


def test_register_single_agent_inserts_row(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite"
    runner.invoke(app, ["init", "--db", str(db)])

    result = runner.invoke(
        app,
        ["register", "--agent-id", "alice", "--db", str(db)],
    )
    assert result.exit_code == 0, result.stdout
    assert "alice" in result.stdout

    store = Store(str(db))
    agents = store.list_agents(7)
    assert any(a.agent_id == "alice" for a in agents)


def test_register_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite"
    runner.invoke(app, ["init", "--db", str(db)])
    runner.invoke(app, ["register", "--agent-id", "alice", "--db", str(db)])
    result = runner.invoke(app, ["register", "--agent-id", "alice", "--db", str(db)])
    assert result.exit_code == 0

    store = Store(str(db))
    ids = [a.agent_id for a in store.list_agents(7)]
    assert ids.count("alice") == 1


def test_register_rejects_invalid_agent_id(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite"
    runner.invoke(app, ["init", "--db", str(db)])
    result = runner.invoke(app, ["register", "--agent-id", "BAD ID!", "--db", str(db)])
    assert result.exit_code != 0


def test_register_all_reads_hermes_profiles(tmp_path: Path, monkeypatch) -> None:
    """register --all must discover profiles under a Hermes root and register each as vlbeau-<profile>."""
    hermes_root = tmp_path / ".hermes" / "profiles"
    hermes_root.mkdir(parents=True)
    for name in ["main", "glm51", "qwen36"]:
        (hermes_root / name).mkdir()
    # Noise: a file should be ignored
    (hermes_root / "README.md").write_text("noise")

    db = tmp_path / "bus.sqlite"
    runner.invoke(app, ["init", "--db", str(db)])

    result = runner.invoke(
        app,
        [
            "register",
            "--all",
            "--db",
            str(db),
            "--hermes-profiles",
            str(hermes_root),
        ],
    )
    assert result.exit_code == 0, result.stdout

    store = Store(str(db))
    ids = {a.agent_id for a in store.list_agents(7)}
    assert {"vlbeau-main", "vlbeau-glm51", "vlbeau-qwen36"} <= ids


def test_register_all_missing_hermes_root(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite"
    runner.invoke(app, ["init", "--db", str(db)])
    result = runner.invoke(
        app,
        [
            "register",
            "--all",
            "--db",
            str(db),
            "--hermes-profiles",
            str(tmp_path / "nope"),
        ],
    )
    assert result.exit_code != 0


def test_register_requires_agent_id_or_all(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite"
    runner.invoke(app, ["init", "--db", str(db)])
    result = runner.invoke(app, ["register", "--db", str(db)])
    assert result.exit_code != 0


def test_register_mutually_exclusive(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite"
    runner.invoke(app, ["init", "--db", str(db)])
    result = runner.invoke(
        app,
        [
            "register",
            "--agent-id",
            "alice",
            "--all",
            "--db",
            str(db),
        ],
    )
    assert result.exit_code != 0
