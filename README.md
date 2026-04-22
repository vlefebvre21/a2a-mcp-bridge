# a2a-mcp-bridge

> MCP server that lets AI agents message each other — A2A-style peer-to-peer communication, exposed as MCP tools.

**Status:** v0.4.4 — usable in production. Not yet published to PyPI; install from GitHub.

## Why

- **MCP (Anthropic)** is the standard for agent ↔ tool. ✅
- **A2A (Google / Linux Foundation)** is the emerging standard for agent ↔ agent. ✅
- But there's a gap: most MCP-native agents (Claude Desktop, Hermes, Cursor, etc.) don't speak A2A yet.

`a2a-mcp-bridge` is a lightweight MCP server that gives any MCP-capable agent a
simple inbox / send / subscribe API to talk to other agents registered on the
same bus. Think of it as "Postfix for AI agents" but stateless-ish, MCP-native,
and SQLite-backed.

## What it does

Five MCP tools, all stable since v0.4:

| Tool | Description |
|---|---|
| `agent_send(target, message, metadata=None)` | Drop a message in another agent's inbox. Returns a `message_id`. Fires a real-time signal and an optional HTTP webhook wake-up toward the recipient. |
| `agent_inbox(limit=10, unread_only=True)` | Read messages addressed to the calling agent. Atomically marks them as read when `unread_only=True`. |
| `agent_list(active_within_days=7)` | List agents seen on the bus in the given window, with their capabilities and last-seen timestamps. |
| `agent_subscribe(timeout_seconds=30, limit=10)` | Long-poll primitive: blocks up to `timeout_seconds` (server-capped at 55 s) waiting for new messages. Returns instantly if messages are already pending. |
| `agent_ping()` | Returns `{"server", "version", "agent_id"}`. Useful to detect a stale stdio child after a bridge upgrade. |

Backed by SQLite by default (zero-dep, single file). No authentication (trust
the local filesystem / use it on a private machine or behind a tunnel).

## Real-time delivery

Delivery is **push + pull hybrid** since v0.2. The authoritative store is
always the SQLite file, but two advisory mechanisms wake consumers up:

1. **Signal files** (`v0.2+`) — every `agent_send` writes
   `$A2A_SIGNAL_DIR/<recipient>.notify` (default `/tmp/a2a-signals/`). Any
   agent blocked in `agent_subscribe` wakes up immediately.
2. **HTTP webhook wake-up** (`v0.4.4+`) — if the recipient is listed in
   the wake registry (see below), the bridge also POSTs an HMAC-signed
   JSON payload to the recipient's local Hermes gateway webhook endpoint,
   which spawns a real agent session that reads the inbox. Best-effort:
   any failure (missing entry, HTTP error, network error) is logged and
   never blocks the SQLite write.

Prior to v0.4.4 the wake-up transport was Telegram. It was replaced by
local HTTP webhooks because Telegram's "a bot never sees its own
messages" rule made per-agent bot wake-ups unreliable in forum-topic
supergroups, and the shared-wake-bot workaround still left the
recipient's gateway deaf (it only polls its own bot, not the crier's).
Webhook POSTs go straight to the recipient's gateway and never fail to
reach the intended session loop.

### Wake registry (optional)

```bash
# Build a registry from your Hermes profiles (reads config.yaml +
# webhook_subscriptions.json under each profile directory).
a2a-mcp-bridge wake-registry init

# Override the path via env var if you want
export A2A_WAKE_REGISTRY=~/.a2a-wake-registry.json
```

Each Hermes profile that has `platforms.webhook.enabled: true` in its
`config.yaml` with a `port` and a `secret` is registered as
`vlbeau-<profile>`. Profiles without a webhook config are skipped
silently. All profiles must share the same HMAC secret so the bridge
can sign any wake-up with a single key.

#### Registry format (v0.4.4+)

```json
{
  "wake_webhook_secret": "<64-hex HMAC secret>",
  "agents": {
    "vlbeau-main":  {"wake_webhook_url": "http://127.0.0.1:8651/webhooks/wake"},
    "vlbeau-glm51": {"wake_webhook_url": "http://127.0.0.1:8653/webhooks/wake"},
    "vlbeau-opus":  {"wake_webhook_url": "http://127.0.0.1:8652/webhooks/wake"}
  }
}
```

#### Hermes profile configuration

Each Hermes profile needs a `wake` route on its webhook adapter. Minimal
per-profile `config.yaml`:

```yaml
platforms:
  webhook:
    enabled: true
    extra:
      host: "127.0.0.1"
      port: 8651            # unique per profile
      rate_limit: 10        # requests / minute (defaults to 30)
      secret: "<shared hex>"
```

Then register the `wake` subscription (triggers an agent session loop
on POST, reading the A2A inbox via the `a2a-inbox-triage` skill):

```bash
HERMES_HOME=~/.hermes/profiles/<name> \
  hermes webhook subscribe wake \
  --prompt "You have been woken up by the A2A bus. Check your inbox." \
  --skills "a2a-inbox-triage" \
  --secret "<shared hex>" \
  --deliver log
```

Re-running `wake-registry init` overwrites the registry from the current
profile state — the v0.4.4 format has no hand-editable fields to
preserve. If a pre-v0.4.4 registry is found (old `wake_bot_token` or
per-agent `bot_token` keys), a migration banner is printed and the file
is rewritten with the new webhook payload.

#### Migrating from v0.4.3 or earlier

The v0.4.4 bridge **rejects** legacy Telegram-based registries (logs a
`WARNING`, disables wake-up, continues running). To restore wake-up:

1. Enable the webhook adapter on each gateway profile (config.yaml +
   `hermes webhook subscribe wake ...` on each).
2. Run `a2a-mcp-bridge wake-registry init` to regenerate the registry
   in the v0.4.4 format.
3. Restart the gateways.

Telegram remains useful for visibility (topic-based supergroups still
show agent-authored messages), but it is no longer a wake-up transport.

## Quick start

### Install from GitHub (recommended)

```bash
# With uv (fastest)
uv tool install --from git+https://github.com/vlefebvre21/a2a-mcp-bridge a2a-mcp-bridge

# With pipx
pipx install git+https://github.com/vlefebvre21/a2a-mcp-bridge
```

### Initialize the bus

```bash
a2a-mcp-bridge init --db ~/.a2a-bus.sqlite
```

### Wire it to an MCP client

Add this to your client's MCP config. Each agent **must** set its own
`A2A_AGENT_ID`; the bridge refuses to start without one.

#### Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or
`%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```jsonc
{
  "mcpServers": {
    "a2a": {
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

#### Hermes (or any MCP-capable agent)

Same shape — point `command` at the `a2a-mcp-bridge` entry point installed by
`uv tool install`, set a distinct `A2A_AGENT_ID` per profile, and share the
same `A2A_DB_PATH`.

Restart the client. The five tools (`agent_send`, `agent_inbox`, `agent_list`,
`agent_subscribe`, `agent_ping`) become available. Any other MCP-capable
agent pointed at the same `A2A_DB_PATH` (with its own `A2A_AGENT_ID`) can
exchange messages with you.

### Pre-register agents (optional)

By default an agent is registered on the bus at its first tool call. To
pre-populate:

```bash
# Single agent
a2a-mcp-bridge register --agent-id vlbeau-main

# All Hermes profiles at once (→ vlbeau-<profile>)
a2a-mcp-bridge register --all --hermes-profiles ~/.hermes/profiles
```

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `A2A_AGENT_ID` | *required* | Identity this process advertises on the bus. |
| `A2A_DB_PATH` | `./a2a-bus.sqlite` | SQLite file (shared by all agents on the bus). |
| `A2A_SIGNAL_DIR` | `/tmp/a2a-signals` | Directory used by `agent_subscribe` for real-time wake-ups. |
| `A2A_WAKE_REGISTRY` | `~/.a2a-wake-registry.json` | JSON wake registry. v0.4.4+ shape: `{"wake_webhook_secret": "...", "agents": {<id>: {"wake_webhook_url": "..."}}}`. Legacy Telegram-based registries (`wake_bot_token` or per-agent `bot_token`) are detected, logged with a migration warning, and treated as "wake-up disabled" until regenerated. Missing/invalid file → feature silently disabled. |

## CLI reference

```
a2a-mcp-bridge serve              # run the MCP stdio server
a2a-mcp-bridge init               # initialize the SQLite schema
a2a-mcp-bridge register [...]     # pre-register one or all agents
a2a-mcp-bridge agents list        # show agents seen on the bus
a2a-mcp-bridge messages tail      # tail recent messages
a2a-mcp-bridge wake-registry init # build the webhook wake registry from ~/.hermes
```

## Roadmap

Shipped so far:

- **v0.1** — Three MCP tools, SQLite store, Typer CLI, CI on 3.11 / 3.12 / 3.13.
- **v0.2** — Signal-file real-time delivery + `agent_subscribe` + `register` CLI.
- **v0.3** — Telegram wake-up on `agent_send` + `wake-registry init` CLI.
- **v0.4** — `agent_ping` tool + `tools.listChanged` capability advertised at handshake.
- **v0.4.1** — `lru_cache` on version lookup, `mcp<2` dependency ceiling.
- **v0.4.2** — Forum-topic routing (`thread_id` → `message_thread_id`) +
  `wake-registry init` preserves `thread_id` overrides across regenerations.
  Resolves [Issue #4](https://github.com/vlefebvre21/a2a-mcp-bridge/issues/4).
- **v0.4.3** — Shared-wake-bot format. A single `wake_bot_token` at the top
  of the registry posts every wake-up, so forum-topic routing actually wakes
  the recipient's gateway (a bot does not receive its own messages in a
  supergroup). Self-wake guard added. Legacy per-agent format still accepted.
- **v0.4.3.1** — `wake-registry init` preserves `chat_id` across
  regenerations (previously only `thread_id` was preserved; `chat_id`
  was silently re-read from `.env` on every init, resetting supergroup
  overrides to DM defaults).
- **v0.4.4** — Wake-up transport migrated from Telegram to local HTTP
  webhooks. A single shared HMAC secret signs every wake-up; each gateway
  exposes `http://127.0.0.1:<port>/webhooks/wake` and triggers a real
  agent session on POST. The Telegram-based transport is gone: it never
  reliably woke gateways in forum-topic supergroups (a bot doesn't poll
  another bot's messages). Legacy registries are detected and rejected
  with a migration warning.

Planned:

- **v0.5** — Observability (per-tool stats, structured JSON logs).
- **Later** — Agent Cards (A2A-compliant metadata), HTTP A2A endpoint in front
  of the MCP server, allowlist + token-based auth, optional Redis / Postgres
  backend for multi-host deployments, `agent_reply` + threaded conversations.

## Project ethos

- **Minimal.** No framework lock-in. Pure MCP + stdlib + SQLite. Zero new
  runtime deps added since v0.1 (wake-up uses `urllib.request`).
- **Standards-first.** When A2A defines a primitive, we map to it. No NIH.
- **Multi-agent, multi-host.** Designed for the case where you run 2+ AI
  agents across different machines / profiles and want them to coordinate.
- **Auditable.** All messages stored as plain rows in SQLite.
  `sqlite3 bus.db "SELECT * FROM messages"` just works.
- **Backwards compatible.** Tool signatures and registry format are frozen
  across minor versions. v0.4.x clients can still talk to a v0.1.x store, and
  vice versa. New registry fields (like `thread_id` in v0.4.2) are optional.

## Development

```bash
git clone https://github.com/vlefebvre21/a2a-mcp-bridge
cd a2a-mcp-bridge
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"

pytest -q              # 111 tests, ≥85% coverage gate
ruff check src/ tests/ # lint
mypy src/              # strict type-check
```

See [docs/spec/v0.1.md](./docs/spec/v0.1.md) for the original contract and
[CHANGELOG.md](./CHANGELOG.md) for the full release history.

## License

MIT — see [LICENSE](./LICENSE).

## Inspiration

- [Model Context Protocol](https://modelcontextprotocol.io) (Anthropic)
- [A2A Protocol](https://github.com/a2aproject) (Google / Linux Foundation)
- OpenClaw's `sessions_send` tool (prior art for agent-to-agent messaging
  in an MCP-adjacent framework).
