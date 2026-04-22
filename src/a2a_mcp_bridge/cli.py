"""Typer CLI entry point: `a2a-mcp-bridge ...`."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import suppress
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from .server import main as server_main
from .store import Store

AGENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
DEFAULT_HERMES_PROFILES = "~/.hermes/profiles"
DEFAULT_HERMES_ROOT = "~/.hermes"
DEFAULT_WAKE_REGISTRY = "~/.a2a-wake-registry.json"
ROOT_PROFILE_AGENT_ID = "vlbeau-opus"
AGENT_ID_PREFIX = "vlbeau-"

app = typer.Typer(
    name="a2a-mcp-bridge",
    help="MCP server for agent-to-agent messaging.",
    no_args_is_help=True,
)
agents_app = typer.Typer(help="Manage and inspect agents.")
messages_app = typer.Typer(help="Inspect messages.")
wake_registry_app = typer.Typer(help="Manage the webhook wake-up registry.")
app.add_typer(agents_app, name="agents")
app.add_typer(messages_app, name="messages")
app.add_typer(wake_registry_app, name="wake-registry")

console = Console()
DEFAULT_DB = "~/.a2a-bus.sqlite"


def _expand(p: str) -> str:
    return str(Path(p).expanduser())


@app.command()
def serve(
    db: str = typer.Option(DEFAULT_DB, help="Path to SQLite database file."),
    agent_id: str = typer.Option(
        None,
        "--agent-id",
        help="Override A2A_AGENT_ID. Required if env var not set.",
    ),
) -> None:
    """Run the MCP stdio server."""
    if agent_id:
        os.environ["A2A_AGENT_ID"] = agent_id
    os.environ["A2A_DB_PATH"] = _expand(db)
    server_main()


@app.command()
def init(
    db: str = typer.Option(DEFAULT_DB, help="Path to SQLite database file."),
) -> None:
    """Create the database file and initialise the schema."""
    path = _expand(db)
    store = Store(path)
    store.init_schema()
    console.print(f"[green]OK[/green] initialised {path}")


@app.command()
def register(
    agent_id: str = typer.Option(
        None,
        "--agent-id",
        help="Agent ID to register immediately.",
    ),
    all_agents: bool = typer.Option(
        False,
        "--all",
        help="Register every Hermes profile found under --hermes-profiles as vlbeau-<profile>.",
    ),
    hermes_profiles: str = typer.Option(
        DEFAULT_HERMES_PROFILES,
        "--hermes-profiles",
        help="Path to Hermes profiles directory (used with --all).",
    ),
    db: str = typer.Option(DEFAULT_DB, help="Path to SQLite database file."),
) -> None:
    """Register one or more agents on the bus without waiting for their first tool call.

    Fixes the v0.1 auto-register gap: an agent is only visible in `agent_list` once it
    has made a tool call. Use this at deployment time to pre-populate the bus.
    """
    if bool(agent_id) == bool(all_agents):
        console.print(
            "[red]error:[/red] provide exactly one of --agent-id or --all",
        )
        raise typer.Exit(code=2)

    store = Store(_expand(db))

    if agent_id:
        if not AGENT_ID_PATTERN.match(agent_id):
            console.print(
                f"[red]error:[/red] invalid agent_id {agent_id!r} — "
                r"must match ^[a-z0-9][a-z0-9_-]{0,63}$"
            )
            raise typer.Exit(code=2)
        store.upsert_agent(agent_id)
        console.print(f"[green]OK[/green] registered {agent_id}")
        return

    # --all: scan Hermes profiles
    profiles_root = Path(_expand(hermes_profiles))
    if not profiles_root.is_dir():
        console.print(f"[red]error:[/red] Hermes profiles directory not found: {profiles_root}")
        raise typer.Exit(code=2)

    registered: list[str] = []
    for entry in sorted(profiles_root.iterdir()):
        if not entry.is_dir():
            continue
        candidate = f"{AGENT_ID_PREFIX}{entry.name}"
        if not AGENT_ID_PATTERN.match(candidate):
            console.print(f"[yellow]skip[/yellow] {candidate} (invalid id)")
            continue
        store.upsert_agent(candidate)
        registered.append(candidate)

    if not registered:
        console.print(f"[yellow]No profiles found under {profiles_root}[/yellow]")
        return

    table = Table(title=f"Registered {len(registered)} agent(s)")
    table.add_column("agent_id")
    for aid in registered:
        table.add_row(aid)
    console.print(table)


@agents_app.command("list")
def agents_list(
    db: str = typer.Option(DEFAULT_DB, help="Path to SQLite database file."),
    window: int = typer.Option(7, help="Active window in days."),
) -> None:
    """List agents seen on the bus within the active window."""
    store = Store(_expand(db))
    agents = store.list_agents(active_within_days=window)
    if not agents:
        console.print("[yellow]No active agents[/yellow]")
        return
    table = Table(title=f"Agents active within {window} days")
    table.add_column("agent_id")
    table.add_column("first_seen")
    table.add_column("last_seen")
    for a in agents:
        table.add_row(a.agent_id, a.first_seen_at.isoformat(), a.last_seen_at.isoformat())
    console.print(table)


@messages_app.command("tail")
def messages_tail(
    db: str = typer.Option(DEFAULT_DB, help="Path to SQLite database file."),
    limit: int = typer.Option(20, help="How many recent messages to show."),
) -> None:
    """Print the most recent messages from the bus (admin/debug only)."""
    conn = sqlite3.connect(_expand(db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, sender_id, recipient_id, body, created_at, read_at
        FROM messages
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    table = Table(title=f"Last {limit} messages")
    table.add_column("id", overflow="ellipsis", max_width=12)
    table.add_column("from")
    table.add_column("to")
    table.add_column("body", overflow="ellipsis", max_width=40)
    table.add_column("sent_at")
    table.add_column("read")
    for r in rows:
        table.add_row(
            r["id"],
            r["sender_id"],
            r["recipient_id"],
            r["body"],
            r["created_at"],
            "✓" if r["read_at"] else "",
        )
    console.print(table)


# --------------------------------------------------------------------------- #
# wake-registry commands (v0.3)
# --------------------------------------------------------------------------- #


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a minimal KEY=VALUE .env file.

    Strips surrounding single/double quotes on values. Ignores blank lines and
    lines starting with '#'. Lines without '=' are skipped silently.
    """
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _profile_webhook_config(profile_dir: Path) -> dict[str, Any] | None:
    """Read webhook configuration from a Hermes profile.

    Looks at two sources in order of priority:

    1. ``<profile>/config.yaml`` → ``platforms.webhook.enabled`` + ``extra.host``,
       ``extra.port``, ``extra.secret``.
    2. ``<profile>/webhook_subscriptions.json`` → per-route ``secret`` for the
       ``wake`` route. When present, this per-route secret OVERRIDES the
       global one (this mirrors how the Hermes webhook adapter resolves
       secrets: route-level beats global).

    Returns a dict with ``host``, ``port``, ``secret`` (str) when a wake
    route is usable, or ``None`` otherwise.
    """
    cfg_path = profile_dir / "config.yaml"
    if not cfg_path.is_file():
        return None

    try:
        import yaml  # type: ignore[import-untyped]  # Lazy import so CLI import cost stays low when unused.
    except ImportError:  # pragma: no cover - yaml is a runtime dep
        return None

    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(cfg, dict):
        return None

    wh = (
        cfg.get("platforms", {})
        .get("webhook", {})
        if isinstance(cfg.get("platforms"), dict)
        else {}
    )
    if not isinstance(wh, dict) or not wh.get("enabled"):
        return None

    extra = wh.get("extra", {})
    if not isinstance(extra, dict):
        return None

    host = str(extra.get("host", "127.0.0.1")).strip() or "127.0.0.1"
    port_raw = extra.get("port")
    port: int
    if isinstance(port_raw, int) and not isinstance(port_raw, bool):
        port = port_raw
    elif isinstance(port_raw, str) and port_raw.strip().isdigit():
        port = int(port_raw.strip())  # tolerate "8650" in YAML
    else:
        return None

    secret = str(extra.get("secret", "")).strip()

    # Route-level secret overrides global — matches Hermes adapter behavior.
    subs_path = profile_dir / "webhook_subscriptions.json"
    if subs_path.is_file():
        try:
            subs = json.loads(subs_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            subs = {}
        if isinstance(subs, dict):
            wake_route = subs.get("wake")
            if isinstance(wake_route, dict):
                route_secret = str(wake_route.get("secret", "")).strip()
                if route_secret:
                    secret = route_secret

    if not secret:
        return None

    return {"host": host, "port": port, "secret": secret}


def _build_webhook_url(host: str, port: int, route: str = "wake") -> str:
    """Build a local webhook URL from (host, port)."""
    # 0.0.0.0 is a bind address; callers reach it via loopback.
    connect_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    return f"http://{connect_host}:{port}/webhooks/{route}"


def _load_existing_registry(path: Path) -> dict[str, Any]:
    """Read an existing registry file, returning the raw top-level dict.

    Silently tolerates missing / malformed files to keep ``init`` idempotent.
    The v0.4.4 format carries nothing that ``init`` needs to preserve from a
    prior run (ports come from config.yaml, secrets from the subscriptions),
    so this is mostly used for logging "migrated from v0.4.x".
    """
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _detect_legacy_format(prior: dict[str, Any]) -> str | None:
    """Classify a prior registry by version. Returns a label or ``None``.

    * ``"v0.4.3"`` — shared-wake-bot format (``wake_bot_token`` + ``agents``).
    * ``"v0.3"`` — per-agent ``bot_token`` at each top-level key.
    * ``None`` — already v0.4.4+, empty, or unrecognised.
    """
    if "wake_webhook_secret" in prior:
        return None  # already v0.4.4
    if "wake_bot_token" in prior:
        return "v0.4.3"
    for value in prior.values():
        if isinstance(value, dict) and "bot_token" in value:
            return "v0.3"
    return None


@wake_registry_app.command("init")
def wake_registry_init(
    hermes_profiles: str = typer.Option(
        DEFAULT_HERMES_PROFILES,
        "--hermes-profiles",
        help="Path to the Hermes profiles directory.",
    ),
    hermes_root: str = typer.Option(
        DEFAULT_HERMES_ROOT,
        "--hermes-root",
        help="Path to the Hermes root directory (its .env is mapped to vlbeau-opus).",
    ),
    output: str = typer.Option(
        DEFAULT_WAKE_REGISTRY,
        "--output",
        "-o",
        help="Where to write the generated wake-registry JSON file.",
    ),
) -> None:
    """Build the webhook wake-up registry from existing Hermes profiles.

    Scans ``<hermes-profiles>/<name>/`` for every subdirectory and maps each
    profile to agent_id ``vlbeau-<name>``. For each profile, reads its
    ``config.yaml`` to extract the ``platforms.webhook.{host, port, secret}``
    and its ``webhook_subscriptions.json`` for a per-route ``wake`` secret
    (route-level secret wins when both are set, matching the Hermes adapter).

    All agents must share the **same** webhook secret — this shared secret
    becomes ``wake_webhook_secret`` at the top level of the registry so the
    bridge can sign any outgoing wake-up without having to fetch a different
    secret per recipient. Profiles whose secret disagrees with the first
    one seen are skipped with a warning.

    Profiles without a usable webhook config are skipped silently — the
    command never fails on a partial environment.

    Output format (v0.4.4+)::

        {
          "wake_webhook_secret": "<shared hex>",
          "agents": {
            "vlbeau-main":  {"wake_webhook_url": "http://127.0.0.1:8651/webhooks/wake"},
            "vlbeau-glm51": {"wake_webhook_url": "http://127.0.0.1:8653/webhooks/wake"}
          }
        }

    If a prior registry in a pre-v0.4.4 format is detected, a migration
    note is logged. The prior content is **overwritten** — it was based on
    Telegram primitives that v0.4.4 no longer uses.
    """
    profiles_root = Path(_expand(hermes_profiles))
    if not profiles_root.is_dir():
        console.print(f"[red]error:[/red] Hermes profiles directory not found: {profiles_root}")
        raise typer.Exit(code=2)

    out_path = Path(_expand(output))
    prior = _load_existing_registry(out_path)
    legacy = _detect_legacy_format(prior)
    if legacy:
        console.print(
            f"[yellow]migrating[/yellow] prior {legacy} registry at {out_path} "
            f"to v0.4.4 webhook format"
        )

    registry_agents: dict[str, dict[str, Any]] = {}
    shared_secret: str | None = None
    skipped_mismatch: list[str] = []

    def _integrate(agent_id: str, profile_dir: Path) -> None:
        """Compute a wake entry for this profile and merge it into state."""
        nonlocal shared_secret
        wc = _profile_webhook_config(profile_dir)
        if wc is None:
            return
        if shared_secret is None:
            shared_secret = wc["secret"]
        elif wc["secret"] != shared_secret:
            # Conflict: v0.4.4 requires a single shared secret. Skip the
            # divergent agent and surface it in the summary.
            skipped_mismatch.append(agent_id)
            return
        registry_agents[agent_id] = {
            "wake_webhook_url": _build_webhook_url(wc["host"], wc["port"])
        }

    # Root .env → vlbeau-opus (if present) — but only when a webhook config
    # also exists for the root directory. Webhooks are per-profile in
    # practice, so this branch is effectively a no-op on typical setups.
    root_env_path = Path(_expand(hermes_root)) / ".env"
    if root_env_path.is_file():
        _integrate(ROOT_PROFILE_AGENT_ID, Path(_expand(hermes_root)))

    # One entry per profile subdirectory.
    for profile_dir in sorted(profiles_root.iterdir()):
        if not profile_dir.is_dir():
            continue
        agent_id = f"{AGENT_ID_PREFIX}{profile_dir.name}"
        if not AGENT_ID_PATTERN.match(agent_id):
            console.print(f"[yellow]skip[/yellow] {agent_id} (invalid id)")
            continue
        _integrate(agent_id, profile_dir)

    # Compose final payload. Even an empty registry is a legal output —
    # callers may have just not configured any webhooks yet.
    payload: dict[str, Any] = {
        "wake_webhook_secret": shared_secret or "",
        "agents": registry_agents,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    # Registry carries ``wake_webhook_secret`` in clear — tighten perms so a
    # multi-user host can't read it via the default umask (0644). Best-effort:
    # some filesystems (mounted FAT/exFAT, Windows shares) reject chmod.
    with suppress(OSError):
        out_path.chmod(0o600)

    if not registry_agents:
        console.print(
            f"[yellow]no wake entries found — wrote empty registry to {out_path}[/yellow]"
        )
        if skipped_mismatch:
            console.print(
                f"[yellow]skipped agents with mismatching webhook secrets:[/yellow] "
                f"{', '.join(skipped_mismatch)}"
            )
        return

    table = Table(
        title=f"Wake registry ({len(registry_agents)} agent(s), webhook) → {out_path}"
    )
    table.add_column("agent_id")
    table.add_column("wake_webhook_url")
    for aid, entry in registry_agents.items():
        table.add_row(aid, entry["wake_webhook_url"])
    console.print(table)
    if skipped_mismatch:
        console.print(
            f"[yellow]skipped agents with mismatching webhook secrets:[/yellow] "
            f"{', '.join(skipped_mismatch)}"
        )


if __name__ == "__main__":
    app()
