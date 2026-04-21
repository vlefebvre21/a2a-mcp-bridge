"""Tests for the `wake-registry` CLI commands (v0.3)."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from a2a_mcp_bridge.cli import app

runner = CliRunner()


def _write_env(path: Path, **values: str) -> None:
    path.write_text(
        "\n".join(f"{k}={v}" for k, v in values.items()) + "\n",
        encoding="utf-8",
    )


def test_wake_registry_init_builds_registry_from_hermes(tmp_path: Path) -> None:
    """`wake-registry init` must scan profiles and produce a correct JSON map."""
    hermes = tmp_path / ".hermes"
    profiles = hermes / "profiles"
    (profiles / "main").mkdir(parents=True)
    (profiles / "glm51").mkdir()
    _write_env(
        profiles / "main" / ".env",
        TELEGRAM_BOT_TOKEN="111:MAIN",
        TELEGRAM_HOME_CHANNEL="1000",
    )
    _write_env(
        profiles / "glm51" / ".env",
        TELEGRAM_BOT_TOKEN="222:GLM",
        TELEGRAM_HOME_CHANNEL="2000",
    )
    # Root .env → vlbeau-opus (root profile convention)
    _write_env(
        hermes / ".env",
        TELEGRAM_BOT_TOKEN="333:OPUS",
        TELEGRAM_HOME_CHANNEL="3000",
    )

    out = tmp_path / "wake.json"
    result = runner.invoke(
        app,
        [
            "wake-registry",
            "init",
            "--hermes-profiles",
            str(profiles),
            "--hermes-root",
            str(hermes),
            "--output",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.stdout
    reg = json.loads(out.read_text())
    assert reg["vlbeau-main"] == {"bot_token": "111:MAIN", "chat_id": "1000"}
    assert reg["vlbeau-glm51"] == {"bot_token": "222:GLM", "chat_id": "2000"}
    assert reg["vlbeau-opus"] == {"bot_token": "333:OPUS", "chat_id": "3000"}


def test_wake_registry_init_skips_profile_missing_env(tmp_path: Path) -> None:
    hermes = tmp_path / ".hermes"
    profiles = hermes / "profiles"
    (profiles / "main").mkdir(parents=True)
    _write_env(
        profiles / "main" / ".env",
        TELEGRAM_BOT_TOKEN="111:MAIN",
        TELEGRAM_HOME_CHANNEL="1000",
    )
    # glm51 has no .env → must be skipped, not crash
    (profiles / "glm51").mkdir()

    out = tmp_path / "wake.json"
    result = runner.invoke(
        app,
        [
            "wake-registry",
            "init",
            "--hermes-profiles",
            str(profiles),
            "--hermes-root",
            str(hermes),
            "--output",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.stdout
    reg = json.loads(out.read_text())
    assert "vlbeau-main" in reg
    assert "vlbeau-glm51" not in reg


def test_wake_registry_init_skips_env_missing_required_keys(tmp_path: Path) -> None:
    hermes = tmp_path / ".hermes"
    profiles = hermes / "profiles"
    (profiles / "main").mkdir(parents=True)
    # Only one of the two required variables → skip
    _write_env(profiles / "main" / ".env", TELEGRAM_BOT_TOKEN="111:MAIN")

    out = tmp_path / "wake.json"
    result = runner.invoke(
        app,
        [
            "wake-registry",
            "init",
            "--hermes-profiles",
            str(profiles),
            "--hermes-root",
            str(hermes),
            "--output",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.stdout
    reg = json.loads(out.read_text())
    assert "vlbeau-main" not in reg


def test_wake_registry_init_empty_result(tmp_path: Path) -> None:
    hermes = tmp_path / ".hermes"
    profiles = hermes / "profiles"
    profiles.mkdir(parents=True)
    out = tmp_path / "wake.json"
    result = runner.invoke(
        app,
        [
            "wake-registry",
            "init",
            "--hermes-profiles",
            str(profiles),
            "--hermes-root",
            str(hermes),
            "--output",
            str(out),
        ],
    )
    assert result.exit_code == 0
    # Empty registry file is still written
    assert out.is_file()
    assert json.loads(out.read_text()) == {}


def test_wake_registry_init_missing_profiles_dir(tmp_path: Path) -> None:
    out = tmp_path / "wake.json"
    result = runner.invoke(
        app,
        [
            "wake-registry",
            "init",
            "--hermes-profiles",
            str(tmp_path / "nope"),
            "--hermes-root",
            str(tmp_path),
            "--output",
            str(out),
        ],
    )
    assert result.exit_code != 0
    assert not out.exists()


def test_wake_registry_init_handles_quoted_values(tmp_path: Path) -> None:
    """Real .env files often quote values — they must be unquoted."""
    hermes = tmp_path / ".hermes"
    profiles = hermes / "profiles"
    (profiles / "main").mkdir(parents=True)
    (profiles / "main" / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=\"111:MAIN\"\nTELEGRAM_HOME_CHANNEL='1000'\n",
        encoding="utf-8",
    )
    out = tmp_path / "wake.json"
    result = runner.invoke(
        app,
        [
            "wake-registry",
            "init",
            "--hermes-profiles",
            str(profiles),
            "--hermes-root",
            str(hermes),
            "--output",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.stdout
    reg = json.loads(out.read_text())
    assert reg["vlbeau-main"] == {"bot_token": "111:MAIN", "chat_id": "1000"}
