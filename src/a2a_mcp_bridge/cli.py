"""Typer CLI entry point: `a2a-mcp-bridge ...`."""

from __future__ import annotations

import json
import os
import re
import sqlite3
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
wake_registry_app = typer.Typer(help="Manage the Telegram wake-up registry.")
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


def _entry_from_env(env: dict[str, str]) -> dict[str, Any] | None:
    """Build a wake registry entry shell from one profile's env.

    Returns ``{"chat_id": ...}`` when at least a Telegram channel is known.
    Returns ``None`` when the profile has no Telegram channel (can't wake).

    ``bot_token`` from the env is intentionally *not* copied here: in the
    v0.4.3+ shared-wake-bot format only the wake bot's token matters, and
    legacy callers that still need per-agent tokens handle that elsewhere.
    """
    chat_id = env.get("TELEGRAM_HOME_CHANNEL", "").strip()
    if not chat_id:
        return None
    return {"chat_id": chat_id}


def _legacy_entry_from_env(env: dict[str, str]) -> dict[str, Any] | None:
    """Legacy helper: includes per-agent ``bot_token`` in the entry.

    Kept for operators who explicitly opt out of the shared-bot model via
    ``--legacy-format``.
    """
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = env.get("TELEGRAM_HOME_CHANNEL", "").strip()
    if not token or not chat_id:
        return None
    return {"bot_token": token, "chat_id": chat_id}


def _load_existing_registry(path: Path) -> tuple[str | None, dict[str, dict[str, Any]]]:
    """Read an existing registry file, returning (prior_shared_token, prior_agents).

    v0.4.2: used by ``wake-registry init`` to preserve manually-edited fields
    (typically ``thread_id`` for forum-topic routing) across regenerations.
    v0.4.3: also preserves the ``wake_bot_token`` if the prior registry used
    the shared-bot format, so operators don't have to re-supply it.
    Silently tolerates missing / malformed files to keep ``init`` idempotent.
    """
    if not path.is_file():
        return None, {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, {}
    if not isinstance(raw, dict):
        return None, {}

    shared = raw.get("wake_bot_token")
    if isinstance(shared, str) and shared:
        agents = raw.get("agents")
        if isinstance(agents, dict):
            return shared, {k: v for k, v in agents.items() if isinstance(v, dict)}
        return shared, {}

    # Legacy format: each entry is a dict under the top-level keys.
    return None, {k: v for k, v in raw.items() if isinstance(v, dict)}


# Keys that ``wake-registry init`` will preserve from the existing registry
# when an entry already exists for the same agent_id. Chat_id and bot_token
# are overwritten from the current Hermes .env source; everything else in
# this tuple is carried forward so operators can edit freely.
_PRESERVE_KEYS = ("thread_id",)


def _merge_with_existing(
    new_entry: dict[str, Any],
    existing_entry: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge a freshly-built env entry with its prior value, keeping overrides.

    Any key listed in :data:`_PRESERVE_KEYS` found in the existing entry is
    carried over, so operators can edit ``thread_id`` by hand without fearing
    that the next ``wake-registry init`` will nuke their change.
    """
    if not existing_entry:
        return new_entry
    merged = dict(new_entry)
    for key in _PRESERVE_KEYS:
        if key in existing_entry:
            merged[key] = existing_entry[key]
    return merged


# Name of the Hermes profile whose TELEGRAM_BOT_TOKEN is used as the shared
# wake bot by default. Can be overridden via ``--wake-bot-profile``.
DEFAULT_WAKE_BOT_PROFILE = "main"


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
    wake_bot_profile: str = typer.Option(
        DEFAULT_WAKE_BOT_PROFILE,
        "--wake-bot-profile",
        help=(
            "Name of the Hermes profile whose TELEGRAM_BOT_TOKEN is used as "
            "the shared wake bot. The shared bot posts every wake-up on "
            "behalf of the sender so that Telegram supergroups with forum "
            "topics work correctly. Ignored with --legacy-format."
        ),
    ),
    legacy_format: bool = typer.Option(
        False,
        "--legacy-format",
        help=(
            "Emit the v0.3 - v0.4.2 registry format with one bot_token per "
            "agent instead of the shared-wake-bot format. Only needed for "
            "per-agent DM wake-up setups. Not recommended."
        ),
    ),
) -> None:
    """Build the Telegram wake-up registry from existing Hermes profiles.

    Scans ``<hermes-profiles>/<name>/.env`` for every subdirectory and maps each
    profile to agent_id ``vlbeau-<name>``. If ``<hermes-root>/.env`` exists, it
    is mapped to :data:`ROOT_PROFILE_AGENT_ID` (``vlbeau-opus``).

    Profiles without ``TELEGRAM_HOME_CHANNEL`` set are skipped silently —
    the command never fails on a partial environment.

    By default, emits the v0.4.3+ shared-wake-bot format: a single
    ``wake_bot_token`` drawn from ``--wake-bot-profile`` (default ``main``)
    is used for every wake-up, which is required for Telegram supergroups
    with forum topics (a bot does not receive its own messages). Pass
    ``--legacy-format`` to emit the old per-agent-token format instead.
    """
    profiles_root = Path(_expand(hermes_profiles))
    if not profiles_root.is_dir():
        console.print(f"[red]error:[/red] Hermes profiles directory not found: {profiles_root}")
        raise typer.Exit(code=2)

    out_path = Path(_expand(output))
    prior_shared_token, prior_agents = _load_existing_registry(out_path)

    build_entry = _legacy_entry_from_env if legacy_format else _entry_from_env

    registry_agents: dict[str, dict[str, Any]] = {}

    # Root .env → vlbeau-opus (if present)
    root_env_path = Path(_expand(hermes_root)) / ".env"
    if root_env_path.is_file():
        entry = build_entry(_parse_env_file(root_env_path))
        if entry:
            registry_agents[ROOT_PROFILE_AGENT_ID] = _merge_with_existing(
                entry, prior_agents.get(ROOT_PROFILE_AGENT_ID)
            )

    # One entry per profile subdirectory
    for profile_dir in sorted(profiles_root.iterdir()):
        if not profile_dir.is_dir():
            continue
        env_path = profile_dir / ".env"
        if not env_path.is_file():
            continue
        entry = build_entry(_parse_env_file(env_path))
        if not entry:
            continue
        agent_id = f"{AGENT_ID_PREFIX}{profile_dir.name}"
        if not AGENT_ID_PATTERN.match(agent_id):
            console.print(f"[yellow]skip[/yellow] {agent_id} (invalid id)")
            continue
        registry_agents[agent_id] = _merge_with_existing(
            entry, prior_agents.get(agent_id)
        )

    # Resolve the shared wake-bot token (new format only).
    shared_token: str | None = None
    if not legacy_format:
        wake_bot_env = profiles_root / wake_bot_profile / ".env"
        if wake_bot_env.is_file():
            parsed = _parse_env_file(wake_bot_env)
            candidate = parsed.get("TELEGRAM_BOT_TOKEN", "").strip()
            if candidate:
                shared_token = candidate
        # Fallback: reuse the one we had in the prior registry (if any).
        if not shared_token and prior_shared_token:
            shared_token = prior_shared_token
            console.print(
                f"[yellow]reused wake_bot_token from prior registry[/yellow] "
                f"(profile '{wake_bot_profile}' has no TELEGRAM_BOT_TOKEN)"
            )

    # Compose the final payload.
    if legacy_format:
        payload: dict[str, Any] = dict(registry_agents)
    else:
        if not shared_token:
            console.print(
                f"[red]error:[/red] could not resolve a wake-bot token from "
                f"profile '{wake_bot_profile}' (and no prior registry with one). "
                f"Either populate {wake_bot_profile}/.env with TELEGRAM_BOT_TOKEN "
                f"or re-run with --legacy-format."
            )
            raise typer.Exit(code=3)
        payload = {"wake_bot_token": shared_token, "agents": registry_agents}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    if not registry_agents:
        console.print(
            f"[yellow]No wake entries found — wrote empty registry to {out_path}[/yellow]"
        )
        return

    # Report how many thread_id overrides were preserved so operators notice
    # when a merge actually did something useful.
    preserved = sum(1 for e in registry_agents.values() if "thread_id" in e)

    title_mode = "legacy per-agent" if legacy_format else f"shared-bot ({wake_bot_profile})"
    table = Table(
        title=f"Wake registry ({len(registry_agents)} agent(s), {title_mode}) → {out_path}"
    )
    table.add_column("agent_id")
    table.add_column("chat_id")
    table.add_column("thread_id")
    for aid, entry in registry_agents.items():
        tid = entry.get("thread_id")
        table.add_row(aid, str(entry["chat_id"]), "—" if tid is None else str(tid))
    console.print(table)
    if preserved:
        console.print(
            f"[dim]preserved thread_id on {preserved} entr"
            f"{'y' if preserved == 1 else 'ies'} from prior registry[/dim]"
        )


if __name__ == "__main__":
    app()
