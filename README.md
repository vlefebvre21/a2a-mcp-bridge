# a2a-mcp-bridge

> MCP server that lets AI agents message each other — A2A-style peer-to-peer communication, exposed as MCP tools.

**Status:** 🚧 Pre-alpha — spec phase.

## Why

- **MCP (Anthropic)** is the standard for agent ↔ tool. ✅
- **A2A (Google / Linux Foundation)** is the emerging standard for agent ↔ agent. ✅
- But there's a gap: most MCP-native agents (Claude Desktop, Hermes, Cursor, etc.) don't speak A2A yet.

`a2a-mcp-bridge` is a lightweight MCP server that gives any MCP-capable agent a simple inbox/send API to talk to other agents registered on the same bus. Think of it as "Postfix for AI agents" but stateless-ish and MCP-native.

## What it does (v0.1)

Three MCP tools:

| Tool | Description |
|---|---|
| `agent_send(target, message)` | Drop a message in another agent's inbox. Returns a `message_id`. |
| `agent_inbox(limit=10, unread_only=true)` | Read messages addressed to the calling agent. Marks them as read. |
| `agent_list()` | List agents registered on the bus, with their capabilities. |

Backed by SQLite by default (zero-dep, single file). No authentication in v0.1 (trust the local network / use it on a private machine or behind a tunnel).

## Planned (v0.2+)

- `agent_reply(message_id, ...)` + threaded conversations
- Agent Cards (A2A-compliant metadata)
- HTTP A2A endpoint in front of the MCP server
- Optional Redis / Postgres backends for multi-host deployments
- Allowlist + token-based auth
- Streaming message delivery (SSE)

## Quick start

> Not published yet — see the [Plan](./docs/plans/2026-04-21-v0.1.md) for build progress.

```bash
# Install (future)
pipx install a2a-mcp-bridge

# Run the bus
a2a-mcp-bridge serve --db ~/.a2a-bus.sqlite

# Connect from Claude Desktop / Hermes / Cursor via MCP config
# (see docs/mcp-client-setup.md once v0.1 ships)
```

## Getting started with Claude Desktop

Add this snippet to your Claude Desktop config
(`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS,
`%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```jsonc
{
  "mcpServers": {
    "a2a-bus": {
      "command": "a2a-mcp-bridge",
      "args": ["serve"],
      "env": {
        "A2A_AGENT_ID": "claude-desktop-vince",
        "A2A_DB_PATH": "/home/vince/.a2a-bus.sqlite"
      }
    }
  }
}
```

Restart Claude Desktop. The three tools `agent_send`, `agent_inbox`, and
`agent_list` become available. Any other MCP-capable agent pointed at the same
`A2A_DB_PATH` (with its own `A2A_AGENT_ID`) can exchange messages with you.

## Project ethos

- **Minimal.** No framework lock-in. Pure MCP + SQLite.
- **Standards-first.** When A2A defines a primitive, we map to it. No NIH.
- **Multi-agent, multi-host.** Designed for the case where you run 2+ AI agents across different machines and want them to coordinate.
- **Auditable.** All messages stored as plain rows in SQLite. `sqlite3 bus.db "SELECT * FROM messages"` just works.

## License

MIT — see [LICENSE](./LICENSE).

## Inspiration

- [Model Context Protocol](https://modelcontextprotocol.io) (Anthropic)
- [A2A Protocol](https://github.com/a2aproject) (Google / Linux Foundation)
- OpenClaw's `sessions_send` tool (prior art for agent-to-agent messaging in an MCP-adjacent framework)
