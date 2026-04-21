"""Typer CLI entry point: `a2a-mcp-bridge ...`."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .server import main as server_main
from .store import Store

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
