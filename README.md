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

## Deployment guide (v0.4.4+)

This section is the canonical reference for running the bridge on a
multi-agent Hermes setup. Skip it if you only have one agent.

### Pre-requisites

- One Hermes installation with `N` profiles (one per agent — `main`, `opus`, `glm51`, …)
- Each profile has a unique `A2A_AGENT_ID` in its env (by convention `vlbeau-<profile>`)
- A single shared SQLite bus file, readable by every gateway
- `uv`, `jq`, `curl`, `openssl` available (for the commands below)

### Port allocation — the one rule

**Each Hermes profile's webhook adapter MUST listen on a unique localhost port.**
Ports are only reached over loopback (`127.0.0.1`), so there's no firewall
concern, but two profiles can't share the same port.

Pick any free range and assign one port per profile. We use `8650-8658` for 9
profiles. Keep the mapping stable — it's written into `config.yaml` and the
wake registry, and regenerating the registry re-reads whatever ports are
currently configured.

Example mapping (9-agent fleet):

| Profile | Port | `wake_webhook_url` |
|---|---|---|
| `deepseek` | 8650 | `http://127.0.0.1:8650/webhooks/wake` |
| `main` | 8651 | `http://127.0.0.1:8651/webhooks/wake` |
| `opus` | 8652 | `http://127.0.0.1:8652/webhooks/wake` |
| `glm51` | 8653 | `http://127.0.0.1:8653/webhooks/wake` |
| `gemini` | 8654 | `http://127.0.0.1:8654/webhooks/wake` |
| `heavy` | 8655 | `http://127.0.0.1:8655/webhooks/wake` |
| `mistral` | 8656 | `http://127.0.0.1:8656/webhooks/wake` |
| `qwen36` | 8657 | `http://127.0.0.1:8657/webhooks/wake` |
| `magent` | 8658 | `http://127.0.0.1:8658/webhooks/wake` |

### Generating the shared HMAC secret

Every gateway validates the wake-up signature with the **same** 64-character
hex secret. Generate it once, reuse it in every profile:

```bash
openssl rand -hex 32 > /tmp/a2a-wake-secret.txt
chmod 600 /tmp/a2a-wake-secret.txt
SECRET=$(cat /tmp/a2a-wake-secret.txt)
```

Keep `$SECRET` out of version control. It ends up in two places per profile
(see below), both `chmod 600`.

### Per-profile setup (repeat for each agent)

For each profile `$P` at port `$PORT`:

**1. Patch `~/.hermes/profiles/$P/config.yaml`** — add the webhook adapter:

```yaml
platforms:
  webhook:
    enabled: true
    extra:
      host: "127.0.0.1"
      port: <PORT>             # unique per profile (e.g. 8651)
      rate_limit: 10           # requests/minute — tighter than Hermes default (30)
      secret: "<SECRET>"       # shared 64-hex HMAC key
```

**2. Register the `wake` route** — this is what actually triggers the inbox
triage session when the POST arrives:

```bash
HERMES_HOME=~/.hermes/profiles/$P \
  hermes webhook subscribe wake \
    --prompt "You have been woken up by the A2A bus. Check your inbox." \
    --skills "a2a-inbox-triage" \
    --secret "$SECRET" \
    --deliver log
```

This writes `~/.hermes/profiles/$P/webhook_subscriptions.json`
(`chmod 600`) with a single `wake` route entry. The `a2a-inbox-triage`
skill must be available in that profile's skill tree — install it if
missing (see [skills reference in a2a-workflow](./docs/)).

**3. Restart the gateway** so the new adapter + route take effect.

### Building the wake registry

Once all profiles are configured and at least one has been restarted,
generate the bridge-side registry:

```bash
a2a-mcp-bridge wake-registry init
```

This scans `~/.hermes/profiles/*/config.yaml` in alphabetical order, reads
each `platforms.webhook.extra.{host,port}` plus the `secret` of the `wake`
route in `webhook_subscriptions.json`, and emits
`~/.a2a-wake-registry.json` (`chmod 600`). The first profile with a secret
wins — if two profiles disagree, the CLI prints a warning and skips the
outliers.

Verification checklist after `init`:

```bash
jq 'keys' ~/.a2a-wake-registry.json
#   → ["agents", "wake_webhook_secret"]

jq '.agents | keys | length' ~/.a2a-wake-registry.json
#   → 9 (or however many profiles are configured)

stat -c '%a' ~/.a2a-wake-registry.json
#   → 600

# All 9 gateway webhook endpoints alive?
for port in 8650 8651 8652 8653 8654 8655 8656 8657 8658; do
  curl -s -o /dev/null -w "port $port -> %{http_code}\n" --max-time 3 \
       http://127.0.0.1:$port/health
done
#   → nine "200" lines
```

### Configuration inventory — where things live

After a full install, here's every file the bridge and Hermes touch,
what each one contains, and who owns it:

| Path | Purpose | Perms | Scope |
|---|---|---|---|
| `~/.a2a-bus.sqlite` | Authoritative message store (`messages` + `agents` tables) | 644 | Shared by all agents |
| `/tmp/a2a-signals/<agent>.notify` | Signal file touched by `agent_send` — wakes up long-polling `agent_subscribe` consumers | 644 | One per recipient |
| `~/.a2a-wake-registry.json` | Bridge → gateway routing: `{wake_webhook_secret, agents: {<id>: {wake_webhook_url}}}` | 600 | Single, user-wide |
| `~/.hermes/profiles/<P>/config.yaml` | Hermes gateway config, holds the `platforms.webhook` block | 644 | Per profile |
| `~/.hermes/profiles/<P>/webhook_subscriptions.json` | The `wake` route metadata: prompt, skills, HMAC secret, delivery mode | 600 | Per profile |
| `~/.hermes/profiles/<P>/.env` | `A2A_AGENT_ID`, `TELEGRAM_*` (user visibility only now), etc. | 600 | Per profile |
| `~/.a2a-wake-registry.v*.bak` | Backups auto-created on format migration | 600 | Optional |

**Secret locations** (keep these `chmod 600`): the shared HMAC secret
exists **three times** — once in the bridge registry, once in each
profile's `config.yaml`, and once in each profile's
`webhook_subscriptions.json`. If you rotate it, update all three.

### Inspecting a running setup

These are the commands we reach for first when something looks off:

```bash
# 1) Which registry format am I on? Expected in v0.4.4+:
#    ["agents", "wake_webhook_secret"]. Anything else = stale or legacy.
jq 'keys' ~/.a2a-wake-registry.json

# 2) Every agent's wake URL, one line each:
jq -r '.agents | to_entries[] | "\(.key)\t\(.value.wake_webhook_url)"' \
   ~/.a2a-wake-registry.json | column -t

# 3) Are the 9 gateway webhook endpoints alive?
for port in 8650 8651 8652 8653 8654 8655 8656 8657 8658; do
  printf "port %s -> " "$port"
  curl -s -o /dev/null -w "%{http_code}\n" --max-time 3 \
       http://127.0.0.1:$port/health
done

# 4) Who's been seen on the bus? (and how recently)
sqlite3 -header -column ~/.a2a-bus.sqlite \
  "SELECT id, last_seen_at FROM agents ORDER BY last_seen_at DESC;"

# 5) Unread messages piling up somewhere? (the smoking gun for wake-up
#    regressions — a high pending count + old last_seen_at == dead mailbox)
sqlite3 -header -column ~/.a2a-bus.sqlite \
  "SELECT recipient_id, COUNT(*) AS pending
   FROM messages WHERE read_at IS NULL
   GROUP BY recipient_id ORDER BY pending DESC;"

# 6) Live-tail the last 20 messages on the bus
sqlite3 ~/.a2a-bus.sqlite \
  "SELECT datetime(created_at,'unixepoch','localtime') AS at,
          sender_id, recipient_id, substr(body,1,60) AS snippet
   FROM messages ORDER BY created_at DESC LIMIT 20;"

# 7) Which bridge version is this session actually talking to?
#    (After an upgrade, stdio children may still be on the old binary.)
#    From any MCP session: call mcp_a2a_agent_ping() — returns {version, agent_id}.
#    From shell:
a2a-mcp-bridge --help | head -1

# 8) Did the webhook secret get truncated? (Hermes secret-masking can
#    silently turn a 64-hex into something like "<hex 14 chars>***")
jq -r '.wake_webhook_secret | length' ~/.a2a-wake-registry.json
#   → 64 expected; 14-or-so means the string was masked during an edit.
```

### Rollback

The v0.4.4 `wake-registry init` automatically creates a `.bak` of the
prior file on format migration. To roll back to the Telegram transport
(v0.4.3.1), restore the backup, downgrade the bridge, and restart the
gateways — but note that the Telegram wake-path never worked reliably
in forum-topic supergroups, which is why v0.4.4 replaced it. Downgrading
is usually not the right fix.

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
