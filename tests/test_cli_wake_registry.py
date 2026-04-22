"""Tests for the ``wake-registry`` CLI commands."""

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


# --------------------------------------------------------------------------- #
# Default: v0.4.3+ shared-wake-bot format
# --------------------------------------------------------------------------- #


def test_wake_registry_init_defaults_to_shared_bot_format(tmp_path: Path) -> None:
    """``init`` without flags must emit the shared-wake-bot JSON shape."""
    hermes = tmp_path / ".hermes"
    profiles = hermes / "profiles"
    (profiles / "main").mkdir(parents=True)
    (profiles / "glm51").mkdir()
    _write_env(
        profiles / "main" / ".env",
        TELEGRAM_BOT_TOKEN="111:MAIN_WAKE_BOT",
        TELEGRAM_HOME_CHANNEL="1000",
    )
    _write_env(
        profiles / "glm51" / ".env",
        TELEGRAM_BOT_TOKEN="222:GLM",  # should be dropped in new format
        TELEGRAM_HOME_CHANNEL="2000",
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

    payload = json.loads(out.read_text())
    # Top-level shape
    assert payload["wake_bot_token"] == "111:MAIN_WAKE_BOT"
    assert "agents" in payload
    # Per-agent entries: chat_id yes, bot_token no
    assert payload["agents"]["vlbeau-main"] == {"chat_id": "1000"}
    assert payload["agents"]["vlbeau-glm51"] == {"chat_id": "2000"}
    assert "bot_token" not in payload["agents"]["vlbeau-glm51"]


def test_wake_registry_init_uses_custom_wake_bot_profile(tmp_path: Path) -> None:
    """``--wake-bot-profile <name>`` sources the token from that profile."""
    hermes = tmp_path / ".hermes"
    profiles = hermes / "profiles"
    (profiles / "main").mkdir(parents=True)
    (profiles / "opus").mkdir()
    _write_env(
        profiles / "main" / ".env",
        TELEGRAM_BOT_TOKEN="111:MAIN",
        TELEGRAM_HOME_CHANNEL="1000",
    )
    _write_env(
        profiles / "opus" / ".env",
        TELEGRAM_BOT_TOKEN="999:OPUS_AS_WAKE_BOT",
        TELEGRAM_HOME_CHANNEL="9000",
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
            "--wake-bot-profile",
            "opus",
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(out.read_text())
    assert payload["wake_bot_token"] == "999:OPUS_AS_WAKE_BOT"


def test_wake_registry_init_errors_when_wake_bot_token_missing(tmp_path: Path) -> None:
    """No wake-bot token + no prior registry ⇒ hard error (not silent)."""
    hermes = tmp_path / ".hermes"
    profiles = hermes / "profiles"
    (profiles / "main").mkdir(parents=True)
    # main has a channel but NO bot token — broken config
    _write_env(profiles / "main" / ".env", TELEGRAM_HOME_CHANNEL="1000")

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
    assert result.exit_code != 0, result.stdout
    assert "wake-bot token" in result.stdout.lower()


def test_wake_registry_init_reuses_prior_wake_bot_token(tmp_path: Path) -> None:
    """If the profile's .env has no token but the prior registry has one,
    ``init`` must reuse it instead of erroring out."""
    hermes = tmp_path / ".hermes"
    profiles = hermes / "profiles"
    (profiles / "main").mkdir(parents=True)
    # No TELEGRAM_BOT_TOKEN in main.env
    _write_env(profiles / "main" / ".env", TELEGRAM_HOME_CHANNEL="1000")

    out = tmp_path / "wake.json"
    # Seed a prior registry with a shared token
    out.write_text(
        json.dumps(
            {"wake_bot_token": "PRIOR:TOKEN", "agents": {}}
        )
    )
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
    payload = json.loads(out.read_text())
    assert payload["wake_bot_token"] == "PRIOR:TOKEN"
    assert payload["agents"]["vlbeau-main"]["chat_id"] == "1000"


def test_wake_registry_init_preserves_thread_id_across_regenerations(
    tmp_path: Path,
) -> None:
    """thread_id overrides must survive re-init, even with the new format."""
    hermes = tmp_path / ".hermes"
    profiles = hermes / "profiles"
    (profiles / "main").mkdir(parents=True)
    _write_env(
        profiles / "main" / ".env",
        TELEGRAM_BOT_TOKEN="111:NEW",
        TELEGRAM_HOME_CHANNEL="1000",
    )

    out = tmp_path / "wake.json"
    # Seed a prior registry with a thread_id on main
    out.write_text(
        json.dumps(
            {
                "wake_bot_token": "111:OLD",
                "agents": {
                    "vlbeau-main": {"chat_id": "1000", "thread_id": 42}
                },
            }
        )
    )
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
    payload = json.loads(out.read_text())
    # Token refreshed, thread_id preserved
    assert payload["wake_bot_token"] == "111:NEW"
    assert payload["agents"]["vlbeau-main"]["thread_id"] == 42


def test_wake_registry_init_migrates_legacy_prior_registry(tmp_path: Path) -> None:
    """A prior legacy-format registry must be silently upgraded to shared-bot."""
    hermes = tmp_path / ".hermes"
    profiles = hermes / "profiles"
    (profiles / "main").mkdir(parents=True)
    _write_env(
        profiles / "main" / ".env",
        TELEGRAM_BOT_TOKEN="111:MAIN",
        TELEGRAM_HOME_CHANNEL="1000",
    )

    out = tmp_path / "wake.json"
    # Legacy-format prior registry (no wake_bot_token at top level)
    out.write_text(
        json.dumps(
            {
                "vlbeau-main": {
                    "bot_token": "LEGACY_IGNORE",
                    "chat_id": "1000",
                    "thread_id": 7,
                }
            }
        )
    )
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
    payload = json.loads(out.read_text())
    # Now in new format
    assert payload["wake_bot_token"] == "111:MAIN"
    # thread_id preserved even from legacy shape
    assert payload["agents"]["vlbeau-main"]["thread_id"] == 7
    # per-agent bot_token gone
    assert "bot_token" not in payload["agents"]["vlbeau-main"]


def test_wake_registry_init_skips_profile_missing_channel(tmp_path: Path) -> None:
    hermes = tmp_path / ".hermes"
    profiles = hermes / "profiles"
    (profiles / "main").mkdir(parents=True)
    _write_env(
        profiles / "main" / ".env",
        TELEGRAM_BOT_TOKEN="111:MAIN",
        TELEGRAM_HOME_CHANNEL="1000",
    )
    # glm51 has no .env → skipped
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
    payload = json.loads(out.read_text())
    assert "vlbeau-main" in payload["agents"]
    assert "vlbeau-glm51" not in payload["agents"]


def test_wake_registry_init_empty_result(tmp_path: Path) -> None:
    """No profile has a channel → registry is empty but command succeeds."""
    hermes = tmp_path / ".hermes"
    profiles = hermes / "profiles"
    (profiles / "main").mkdir(parents=True)
    # main only has the wake-bot token — no TELEGRAM_HOME_CHANNEL → entry skipped
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
    payload = json.loads(out.read_text())
    assert payload["wake_bot_token"] == "111:MAIN"
    assert payload["agents"] == {}


def test_wake_registry_init_handles_quoted_values(tmp_path: Path) -> None:
    """Real .env files often quote values — they must be unquoted."""
    hermes = tmp_path / ".hermes"
    profiles = hermes / "profiles"
    (profiles / "main").mkdir(parents=True)
    (profiles / "main" / ".env").write_text(
        'TELEGRAM_BOT_TOKEN="111:QUOTED"\n'
        'TELEGRAM_HOME_CHANNEL="1000"\n',
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
    payload = json.loads(out.read_text())
    assert payload["wake_bot_token"] == "111:QUOTED"
    assert payload["agents"]["vlbeau-main"]["chat_id"] == "1000"


def test_wake_registry_init_ignores_corrupt_prior(tmp_path: Path) -> None:
    """A malformed prior registry must not prevent regeneration."""
    hermes = tmp_path / ".hermes"
    profiles = hermes / "profiles"
    (profiles / "main").mkdir(parents=True)
    _write_env(
        profiles / "main" / ".env",
        TELEGRAM_BOT_TOKEN="111:MAIN",
        TELEGRAM_HOME_CHANNEL="1000",
    )

    out = tmp_path / "wake.json"
    out.write_text("this is not json at all")

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
    payload = json.loads(out.read_text())
    assert payload["wake_bot_token"] == "111:MAIN"


# --------------------------------------------------------------------------- #
# --legacy-format flag for operators who need per-agent-token DMs
# --------------------------------------------------------------------------- #


def test_legacy_format_emits_old_shape(tmp_path: Path) -> None:
    """``--legacy-format`` produces the v0.3 - v0.4.2 JSON shape."""
    hermes = tmp_path / ".hermes"
    profiles = hermes / "profiles"
    (profiles / "main").mkdir(parents=True)
    _write_env(
        profiles / "main" / ".env",
        TELEGRAM_BOT_TOKEN="111:MAIN",
        TELEGRAM_HOME_CHANNEL="1000",
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
            "--legacy-format",
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(out.read_text())
    # Legacy shape: no top-level wake_bot_token, entries carry bot_token
    assert "wake_bot_token" not in payload
    assert payload["vlbeau-main"] == {"bot_token": "111:MAIN", "chat_id": "1000"}
