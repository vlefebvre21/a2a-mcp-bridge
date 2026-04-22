"""Tests for the ``wake-registry init`` CLI command (v0.4.4 webhook format)."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from a2a_mcp_bridge.cli import app

runner = CliRunner()


def _write_webhook_profile(
    profile_dir: Path,
    *,
    port: int,
    secret: str,
    host: str = "127.0.0.1",
    enabled: bool = True,
    rate_limit: int | None = None,
) -> None:
    """Create a minimal Hermes-style profile directory with a webhook config."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    # Minimal config.yaml covering just the webhook section — the real
    # Hermes config is much richer, but only platforms.webhook matters here.
    yaml_lines = [
        "platforms:",
        "  webhook:",
        f"    enabled: {str(enabled).lower()}",
        "    extra:",
        f'      host: "{host}"',
        f"      port: {port}",
        f'      secret: "{secret}"',
    ]
    if rate_limit is not None:
        yaml_lines.append(f"      rate_limit: {rate_limit}")
    (profile_dir / "config.yaml").write_text(
        "\n".join(yaml_lines) + "\n", encoding="utf-8"
    )
    # Also need a .env to match Hermes profile shape (parseable, any content)
    (profile_dir / ".env").write_text("TELEGRAM_BOT_TOKEN=xxx\n", encoding="utf-8")


def _write_subscription(
    profile_dir: Path, *, route_secret: str, skills: list[str] | None = None
) -> None:
    """Create a webhook_subscriptions.json with a 'wake' route."""
    subs = {
        "wake": {
            "description": "test",
            "events": [],
            "secret": route_secret,
            "prompt": "test",
            "skills": skills or [],
            "deliver": "log",
            "created_at": "2026-01-01T00:00:00Z",
        }
    }
    (profile_dir / "webhook_subscriptions.json").write_text(
        json.dumps(subs), encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# Happy-path: a clean set of profiles produces the v0.4.4 payload
# --------------------------------------------------------------------------- #


def test_init_emits_v044_webhook_format(tmp_path: Path) -> None:
    hermes = tmp_path / ".hermes"
    profiles = hermes / "profiles"

    shared = "abcdef1234567890" * 4  # 64 hex-ish chars
    _write_webhook_profile(profiles / "main", port=8651, secret=shared)
    _write_webhook_profile(profiles / "glm51", port=8653, secret=shared)

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
    assert payload["wake_webhook_secret"] == shared
    assert payload["agents"]["vlbeau-main"] == {
        "wake_webhook_url": "http://127.0.0.1:8651/webhooks/wake"
    }
    assert payload["agents"]["vlbeau-glm51"] == {
        "wake_webhook_url": "http://127.0.0.1:8653/webhooks/wake"
    }


def test_init_uses_route_level_secret_over_global(tmp_path: Path) -> None:
    """Subscription ``wake.secret`` wins over ``platforms.webhook.extra.secret``.

    This mirrors how the Hermes webhook adapter resolves secrets at runtime
    (``route_config.get("secret", global)``). The registry must match so that
    the HMAC signature we compute with the shared secret verifies against
    whatever the gateway actually validates.
    """
    hermes = tmp_path / ".hermes"
    profiles = hermes / "profiles"

    route_secret = "route-secret-" + "x" * 48
    _write_webhook_profile(profiles / "main", port=8651, secret="global-ignored")
    _write_subscription(profiles / "main", route_secret=route_secret)

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
    assert payload["wake_webhook_secret"] == route_secret


def test_init_skips_profiles_without_webhook(tmp_path: Path) -> None:
    """Profiles with no webhook section are silently skipped."""
    hermes = tmp_path / ".hermes"
    profiles = hermes / "profiles"

    shared = "abcd" * 16
    _write_webhook_profile(profiles / "main", port=8651, secret=shared)

    # nowebhook has a .env and a config.yaml but no platforms section
    nowh = profiles / "nowebhook"
    nowh.mkdir(parents=True)
    (nowh / "config.yaml").write_text("model:\n  default: x\n", encoding="utf-8")
    (nowh / ".env").write_text("X=y\n", encoding="utf-8")

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
    assert "vlbeau-nowebhook" not in payload["agents"]
    assert "vlbeau-main" in payload["agents"]


def test_init_skips_disabled_webhook(tmp_path: Path) -> None:
    """platforms.webhook.enabled=false is treated as "no webhook"."""
    hermes = tmp_path / ".hermes"
    profiles = hermes / "profiles"
    _write_webhook_profile(
        profiles / "main", port=8651, secret="x" * 64, enabled=False
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
    assert payload["agents"] == {}


def test_init_skips_profiles_with_mismatching_secret(tmp_path: Path) -> None:
    """Secret conflicts are skipped + surfaced in stdout (not silently merged)."""
    hermes = tmp_path / ".hermes"
    profiles = hermes / "profiles"

    _write_webhook_profile(profiles / "main", port=8651, secret="secret-A" * 8)
    # glm51 has a different secret — must be skipped
    _write_webhook_profile(profiles / "glm51", port=8653, secret="secret-B" * 8)

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
    # First profile scanned wins. Profiles are iterated in sorted order, so
    # glm51 comes before main alphabetically and its secret is adopted.
    assert payload["wake_webhook_secret"] == "secret-B" * 8
    assert "vlbeau-glm51" in payload["agents"]
    assert "vlbeau-main" not in payload["agents"]
    # stdout surfaces the mismatch
    assert "mismatching" in result.stdout.lower() or "mismatch" in result.stdout.lower()
    assert "vlbeau-main" in result.stdout


def test_init_migrates_legacy_v043_registry(tmp_path: Path) -> None:
    """A pre-existing v0.4.3 Telegram registry must be detected + overwritten."""
    hermes = tmp_path / ".hermes"
    profiles = hermes / "profiles"
    shared = "new-secret-" + "x" * 53
    _write_webhook_profile(profiles / "main", port=8651, secret=shared)

    out = tmp_path / "wake.json"
    # Seed a legacy v0.4.3 registry.
    out.write_text(
        json.dumps(
            {
                "wake_bot_token": "111:OLD",
                "agents": {"vlbeau-main": {"chat_id": "-100", "thread_id": 5}},
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
    # Legacy keys gone.
    assert "wake_bot_token" not in payload
    assert payload["wake_webhook_secret"] == shared
    # Migration banner printed.
    assert "migrating" in result.stdout.lower()
    assert "v0.4.3" in result.stdout


def test_init_migrates_legacy_v03_registry(tmp_path: Path) -> None:
    """A v0.3 per-agent-token registry also triggers the migration banner."""
    hermes = tmp_path / ".hermes"
    profiles = hermes / "profiles"
    _write_webhook_profile(profiles / "main", port=8651, secret="s" * 64)

    out = tmp_path / "wake.json"
    out.write_text(
        json.dumps(
            {"vlbeau-main": {"bot_token": "111:X", "chat_id": "1000"}}
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
    assert "migrating" in result.stdout.lower()
    assert "v0.3" in result.stdout


def test_init_tolerates_missing_profile_directory(tmp_path: Path) -> None:
    """Never crash on a partial environment."""
    hermes = tmp_path / ".hermes"
    profiles = hermes / "profiles"
    # Don't create any profile. profiles_root must exist for the CLI not to
    # fail, but it may be empty.
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
    assert result.exit_code == 0, result.stdout
    payload = json.loads(out.read_text())
    assert payload == {"wake_webhook_secret": "", "agents": {}}


def test_init_fails_on_missing_profiles_root(tmp_path: Path) -> None:
    """Non-existent profiles root is a hard error (typer exit code 2)."""
    hermes = tmp_path / ".hermes"
    out = tmp_path / "wake.json"
    result = runner.invoke(
        app,
        [
            "wake-registry",
            "init",
            "--hermes-profiles",
            str(hermes / "does-not-exist"),
            "--hermes-root",
            str(hermes),
            "--output",
            str(out),
        ],
    )
    assert result.exit_code == 2


def test_init_skips_webhook_missing_port(tmp_path: Path) -> None:
    """A malformed webhook section (no port) is skipped, not a crash."""
    hermes = tmp_path / ".hermes"
    profiles = hermes / "profiles"

    main = profiles / "main"
    main.mkdir(parents=True)
    (main / "config.yaml").write_text(
        "platforms:\n"
        "  webhook:\n"
        "    enabled: true\n"
        "    extra:\n"
        '      host: "127.0.0.1"\n'
        '      secret: "x"\n',
        encoding="utf-8",
    )
    (main / ".env").write_text("X=y\n", encoding="utf-8")

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
    assert payload["agents"] == {}


def test_init_accepts_string_port_from_yaml(tmp_path: Path) -> None:
    """YAML that quotes the port (``port: "8651"``) is tolerated."""
    hermes = tmp_path / ".hermes"
    profiles = hermes / "profiles"

    main = profiles / "main"
    main.mkdir(parents=True)
    (main / "config.yaml").write_text(
        "platforms:\n"
        "  webhook:\n"
        "    enabled: true\n"
        "    extra:\n"
        '      host: "127.0.0.1"\n'
        '      port: "8651"\n'
        '      secret: "aaa"\n',
        encoding="utf-8",
    )
    (main / ".env").write_text("X=y\n", encoding="utf-8")

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
    assert payload["agents"]["vlbeau-main"] == {
        "wake_webhook_url": "http://127.0.0.1:8651/webhooks/wake"
    }


def test_init_maps_0000_host_to_loopback(tmp_path: Path) -> None:
    """If config binds 0.0.0.0, the URL reaches it via 127.0.0.1."""
    hermes = tmp_path / ".hermes"
    profiles = hermes / "profiles"
    _write_webhook_profile(
        profiles / "main", port=8651, secret="x" * 64, host="0.0.0.0"
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
    assert payload["agents"]["vlbeau-main"] == {
        "wake_webhook_url": "http://127.0.0.1:8651/webhooks/wake"
    }


def test_init_ignores_corrupt_prior_registry(tmp_path: Path) -> None:
    """A corrupt prior registry must not prevent regeneration."""
    hermes = tmp_path / ".hermes"
    profiles = hermes / "profiles"
    _write_webhook_profile(profiles / "main", port=8651, secret="x" * 64)

    out = tmp_path / "wake.json"
    out.write_text("{ this is not { valid json")

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
    assert payload["agents"]["vlbeau-main"]["wake_webhook_url"].startswith("http://")


def test_init_writes_registry_with_0600_perms(tmp_path: Path) -> None:
    """The registry file contains ``wake_webhook_secret`` in clear — its mode
    must be 0600 after write to keep a multi-user host from snooping it via
    the default umask (typically 0644)."""
    import stat

    hermes = tmp_path / ".hermes"
    profiles = hermes / "profiles"
    _write_webhook_profile(profiles / "main", port=8651, secret="x" * 64)

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

    mode = stat.S_IMODE(out.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"
