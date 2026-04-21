"""Typer CLI entry point: `a2a-mcp-bridge ...`."""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .server import main as server_main
from .store import Store

AGENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
DEFAULT_HERMES_PROFILES = "~/.hermes/profiles"
AGENT_ID_PREFIX = "vlbeau-"

app = typer.Typer(
    name="a2a-mcp-bridge",
    help="MCP server for agent-to-agent messaging.",
    no_args_is_help=True,
)
agents_app = typer.Typer(help="Manage and inspect agents.")
messages_app = typer.Typer(help="Inspect messages.")
app.add_typer(agents_app, name="agents")
app.add_typer(messages_app, name="messages")

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


if __name__ == "__main__":
    app()
