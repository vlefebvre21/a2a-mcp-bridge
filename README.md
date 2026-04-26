# a2a-mcp-bridge

> MCP server that lets AI agents message each other — A2A-style peer-to-peer communication, exposed as MCP tools.

**Status:** v0.5.1 — usable in production. Not yet published to PyPI; install from GitHub.

> ### ⚠️ Known limitation: multi-session concurrency per profile
>
> In the current deployment model (Hermes + this bridge), a single
> `agent_id` (a profile like `vlbeau-glm51`) may be served by **several
> concurrent OS processes** at the same time — typically one per
> spawn path (Telegram front-end, webhook wake-up, cron, CLI). Each of
> these processes independently polls `agent_inbox` with
> `unread_only=True`, which **atomically marks messages as read**.
> Consequences:
>
> - Only one session receives any given message. The others never see it.
> - Sessions don't share in-memory state, so the user can believe they
>   are in a continuous conversation while critical replies are
>   consumed by a sibling session that silently resolves the thread.
> - Mutations of shared state (skills, memory, files) can race with no
>   lock and no notification.
>
> If you plan to build a conversation-critical flow on top of this bridge,
> **you must design around this property** — the bridge does not (yet)
> guarantee that `agent_id` identifies a single conversational thread.
> A v0.5 mitigation path is documented in
> [ADR-001 — Multi-session concurrency](docs/adr/ADR-001-multi-session-concurrency.md).

## Why

- **MCP (Anthropic)** is the standard for agent ↔ tool. ✅
- **A2A (Google / Linux Foundation)** is the emerging standard for agent ↔ agent. ✅
- But there's a gap: most MCP-native agents (Claude Desktop, Hermes, Cursor, etc.) don't speak A2A yet.

`a2a-mcp-bridge` is a lightweight MCP server that gives any MCP-capable agent a
simple inbox / send / subscribe API to talk to other agents registered on the
same bus. Think of it as "Postfix for AI agents" but stateless-ish, MCP-native,
and SQLite-backed.

## What it does

Six MCP tools, all stable since v0.5:

| Tool | Description |
|---|---|
| `agent_send(target, message, metadata=None)` | Drop a message in another agent's inbox. Returns a `message_id`. Fires a real-time signal and an optional HTTP webhook wake-up toward the recipient. |
| `agent_inbox(limit=10, unread_only=True)` | Read messages addressed to the calling agent. Atomically marks them as read when `unread_only=True`. |
| `agent_inbox_peek(since_ts=None, limit=50)` | Read-only inbox view — no mark-as-read. Safe for concurrent sessions to inspect the inbox without consuming messages. |
| `agent_list(active_within_days=7)` | List agents seen on the bus in the given window, with their capabilities and last-seen timestamps. |
| `agent_subscribe(timeout_seconds=30, limit=10)` | Long-poll primitive: blocks up to `timeout_seconds` (server-capped at 55 s) waiting for new messages. Returns instantly if messages are already pending. |
| `agent_ping()` | Returns `{"server", "version", "agent_id"}`. Useful to detect a stale stdio child after a bridge upgrade. |

Backed by SQLite by default (zero-dep, single file). For remote/distributed
setups, an optional HTTP facade (`serve-facade`) exposes the bus over REST —
see [HTTP Facade (Remote Mode)](#http-facade-remote-mode) below.

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
  --prompt "You have been woken up by the A2A bus. Read your inbox with mcp_a2a_agent_inbox(). If any message has intent=execute, load the a2a-task-worker skill and follow its workflow (ack, execute, result — all replies with intent=fyi). Otherwise, load a2a-inbox-triage and process normally. Never wrap up prematurely on an execute message." \
  --skills "a2a-inbox-triage" \
  --skills "a2a-task-worker" \
  --secret "<shared hex>" \
  --deliver log
```

Re-running `wake-registry init` overwrites the registry from the current
profile state — the v0.4.4 format has no hand-editable fields to
preserve. If a pre-v0.4.4 registry is found (old `wake_bot_token` or
per-agent `bot_token` keys), a migration banner is printed and the file
is rewritten with the new webhook payload.

#### Webhook routing setup — `intent=execute` vs `intent=triage`

The wake webhook's subscription prompt and skill list are **load-bearing** for
intent-based routing (ADR-002 / ADR-005). When a peer sends a message with
`intent=execute`, the waking session must load `a2a-task-worker` — not the
default `a2a-inbox-triage`, which has no execute branch and will wrap up the
session before the task runs. **This patch is a mandatory installation step**:
without it, fire-and-wait orchestration fails silently.

**Canonical `wake` subscription** (every profile, repeat per agent):

```bash
HERMES_HOME=~/.hermes/profiles/<name> \
  hermes webhook subscribe wake \
  --prompt "You have been woken up by the A2A bus. Read your inbox with mcp_a2a_agent_inbox(). If any message has intent=execute, load the a2a-task-worker skill and follow its workflow (ack, execute, result — all replies with intent=fyi). Otherwise, load a2a-inbox-triage and process normally. Never wrap up prematurely on an execute message." \
  --skills "a2a-inbox-triage" \
  --skills "a2a-task-worker" \
  --secret "<shared hex>" \
  --deliver log
```

**If you already have a pre-routing `webhook_subscriptions.json`** (prompt
starting with "Check your inbox." and `skills: ["a2a-inbox-triage"]` only),
patch it in-place on all profiles:

```bash
for p in ~/.hermes/profiles/*/webhook_subscriptions.json; do
  python3 -c "
import json, sys
path = sys.argv[1]
with open(path) as f: d = json.load(f)
wake = d.get('wake')
if not wake: sys.exit(0)
wake['prompt'] = 'You have been woken up by the A2A bus. Read your inbox with mcp_a2a_agent_inbox(). If any message has intent=execute, load the a2a-task-worker skill and follow its workflow (ack, execute, result — all replies with intent=fyi). Otherwise, load a2a-inbox-triage and process normally. Never wrap up prematurely on an execute message.'
wake['skills'] = ['a2a-inbox-triage', 'a2a-task-worker']
with open(path, 'w') as f: json.dump(d, f, indent=2)
print('patched:', path)
" "$p"
done
```

(The bridge automates this in v0.7 via `a2a-mcp-bridge webhook-setup` — see
issue #23.)

#### Migrating from v0.4.3 or earlier

The v0.4.4 bridge **rejects** legacy Telegram-based registries (logs a
`WARNING`, disables wake-up, continues running). To restore wake-up:

1. Enable the webhook adapter on each gateway profile (config.yaml +
   `hermes webhook subscribe wake ...` on each).
2. Run `a2a-mcp-bridge wake-registry init` to regenerate the registry
   in the v0.4.4 format.
3. Restart the gateways.

Telegram remains available for human↔agent interaction (you message an
agent in its topic, the agent replies there), but it is no longer a
wake-up transport and is **not** an A2A observability channel either —
see the section below.

## Telegram front-end — what it is (and isn't) since v0.4.4

Before v0.4.4, the forum-topic supergroup was load-bearing: it
delivered wake-ups, it routed replies, and it incidentally gave you a
window on the bus. v0.4.4 moved wake-up to local HTTP and the whole
routing layer to SQLite. That leaves the supergroup with **one
surviving role**: it's a multi-agent Telegram front-end for humans.

Concretely:

- ✅ You post in a topic → the matching agent wakes up and replies
  there. This is just "Telegram → Hermes gateway" — it has nothing
  to do with the A2A bus.
- ❌ Agent A calls `agent_send(target="agent-b", ...)` → **nothing
  appears in Telegram.** The message goes to SQLite, the wake POST
  goes over loopback HTTP, agent B reads its inbox via
  `agent_inbox()`. The supergroup is silent during A2A traffic.

So if you're using the supergroup hoping to watch agents talk to each
other, you're watching the wrong window. Use the SQLite inspection
commands in the Deployment guide instead (`sqlite3 ~/.a2a-bus.sqlite
'SELECT ... FROM messages ORDER BY created_at DESC'`).

### Do you still need the supergroup?

Honest answer: **probably not**, if all you do is message one agent at
a time. The supergroup made sense when routing was topic-based; with
v0.4.4 it's just cosmetic grouping. The alternatives, ranked by
simplicity:

1. **Private DMs, one per agent.** Simplest. Each agent gets its own
   bot or its own chat_id. Zero topic routing, zero shared-state
   surprises (like every profile sharing the same `chat_id` in its
   `.env`, which the forum setup requires). Downside: nine separate
   chats in your Telegram sidebar.

2. **One supergroup, non-forum, with `/mention`-based routing.** One
   chat_id shared by all profiles, but no topics — each profile
   responds only when addressed by its `A2A_AGENT_ID`. Less clutter
   than option 1, no forum overhead.

3. **Supergroup with forum topics (current setup).** One chat_id + one
   `thread_id` per profile. Nice visual separation, but comes with:
   forum-topic routing code, `thread_id` preservation across config
   regenerations, the "bot never sees its own topic messages" quirk
   (harmless now that wake-up is HTTP but still confusing), and the
   multi-profile-shares-one-chat gotcha noted in issue #4.

4. **No Telegram at all.** Drive everything via the Hermes CLI / ACP
   / other adapters. The A2A bus doesn't need Telegram to function.

For a pure developer-console workflow, options 1, 2, or 4 are cleaner
than the current forum setup. The project still supports (3) because
migrating away is work and the forum UX has genuine benefits on
mobile — but don't keep it *because* of the A2A bus. It doesn't help
there.

If you want a real observability channel for the bus (every
`agent_send` mirrored to a read-only destination — logs, a dedicated
Telegram topic, a Discord channel, etc.), that's a separate feature,
not something the current supergroup provides. Open an issue if you'd
like to discuss it.

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
    --skills "a2a-workflow" \
    --secret "$SECRET" \
    --deliver log
```

This writes `~/.hermes/profiles/$P/webhook_subscriptions.json`
(`chmod 600`) with a single `wake` route entry. The `a2a-workflow`
skill (under `autonomous-ai-agents/`) ships with Hermes and documents
the full A2A message loop — ensure it's enabled for the profile via
`hermes skills` if the webhook handler reports "skill not found".

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
#    Timestamps are ISO-8601 UTC strings — lexicographic sort works.
sqlite3 -header -column ~/.a2a-bus.sqlite \
  "SELECT id, last_seen_at FROM agents ORDER BY last_seen_at DESC;"

# 5) Unread messages piling up somewhere? (the smoking gun for wake-up
#    regressions — a high pending count + old last_seen_at == dead mailbox)
sqlite3 -header -column ~/.a2a-bus.sqlite \
  "SELECT recipient_id, COUNT(*) AS pending
   FROM messages WHERE read_at IS NULL
   GROUP BY recipient_id ORDER BY pending DESC;"

# 6) Live-tail the last 20 messages on the bus
#    created_at is stored as an ISO-8601 string, so we keep it verbatim.
sqlite3 -header -column ~/.a2a-bus.sqlite \
  "SELECT created_at, sender_id, recipient_id, substr(body,1,60) AS snippet
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

`wake-registry init` **overwrites** any pre-existing registry — it does
not auto-create a `.bak`. If you want a safety net before regenerating,
copy the file yourself:

```bash
cp -a ~/.a2a-wake-registry.json ~/.a2a-wake-registry.json.bak
```

Downgrading to the Telegram transport (v0.4.3.1) is possible but rarely
the right move: the Telegram wake-path never worked reliably in
forum-topic supergroups (a bot never sees its own topic messages, and
routing via a shared crier still left the recipient's gateway deaf),
which is exactly why v0.4.4 replaced it. The usual fix for a wake
failure is to run `wake-registry init` again and check the inspection
commands above, not to roll back.

### Troubleshooting — decision tree

Symptom → first thing to check:

- **Messages persist but recipient never answers.** Run command 5 — a
  rising `pending` for that recipient with no change in its
  `last_seen_at` means the wake never landed. Then:
  - Command 3 (`/health`) — is the recipient's gateway even alive?
  - Command 2 — does the registry still point at the right port?
  - If both OK, tail the recipient gateway's log for `[webhook]` lines
    (signature failure, rate-limit 429, or prompt-handler errors show
    up there).
- **Every wake logs a 401/403.** The HMAC secrets disagree. Re-run
  `wake-registry init` so the registry picks up whatever secret is
  currently in each profile's `webhook_subscriptions.json`. Then
  command 8 — `length` must be 64.
- **`wake-registry init` warns about a mismatched secret.** Two
  profiles carry different `wake` route secrets. Pick one, re-run
  `hermes webhook subscribe wake --secret "$SECRET"` on the outlier(s),
  then regenerate the registry.
- **Bridge logs show `legacy Telegram-based format … wake-up is
  disabled`.** The registry is still in v0.3/v0.4.3 shape. Run
  `wake-registry init` to migrate; the old content will be overwritten
  with the v0.4.4 layout.
- **`uv tool` or session is still on the old bridge binary after an
  upgrade.** `uv tool install --force --reinstall a2a-mcp-bridge` and
  restart any MCP client that spawned the stdio child (they don't
  hot-reload). Verify with `mcp_a2a_agent_ping()` — the `version`
  field must match what you just installed.

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

Restart the client. The six tools (`agent_send`, `agent_inbox`, `agent_inbox_peek`, `agent_list`,
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
| `A2A_BUS_URL` | *unset* | HTTP facade URL for remote mode. When set, `serve` uses `HttpBusStore` instead of local SQLite. Mutually exclusive with `A2A_DB_PATH`. |
| `A2A_SIGNAL_DIR` | `/tmp/a2a-signals` | Directory used by `agent_subscribe` for real-time wake-ups. |
| `A2A_WAKE_REGISTRY` | `~/.a2a-wake-registry.json` | JSON wake registry. v0.4.4+ shape: `{"wake_webhook_secret": "...", "agents": {<id>: {"wake_webhook_url": "..."}}}`. Legacy Telegram-based registries (`wake_bot_token` or per-agent `bot_token`) are detected, logged with a migration warning, and treated as "wake-up disabled" until regenerated. Missing/invalid file → feature silently disabled. |
| `A2A_FACADE_API_KEY` | *unset* | Bearer token for facade authentication. Used by `serve-facade` to require `Authorization: Bearer <key>` on protected endpoints. |
| `A2A_LOG_JSON` | *unset* | If set to `1` / `true` / `yes`, every tool call emits a one-object-per-line JSON log record (schema: `ts`, `level`, `event`, `agent_id`, + `session_id`/`message_id`/`target`/`duration_ms`/`body_hash`/`error_code` when applicable). Unset keeps the pre-v0.5 plain-text format. Evaluated at import time — change requires a server restart. |
| `A2A_LOG_LEVEL` | `INFO` | Log verbosity. |

## HTTP Facade (Remote Mode)

Since v0.5.1 the bus can be exposed over HTTP, allowing agents on **different
machines** to participate. The architecture is:

```
┌──────────────────────┐         ┌──────────────────────────────────────┐
│  VPS (facade server) │         │  Remote machine (agent client)       │
│                      │         │                                      │
│  a2a-mcp-bridge      │  HTTP   │  a2a-mcp-bridge serve                │
│    serve-facade      │◄───────►│    --bus-url http://VPS:8080         │
│    :8080             │         │    --agent-id my-remote-agent        │
│    [SQLite bus.db]   │         │                                      │
└──────────────────────┘         └──────────────────────────────────────┘
```

### Quick start

**On the server** (the machine with the SQLite database):

```bash
pip install a2a-mcp-bridge[facade]
a2a-mcp-bridge serve-facade --host 0.0.0.0 --port 8080 --api-key my-secret-key
```

**On the remote client** (any machine with network access):

```bash
pip install a2a-mcp-bridge[remote]
a2a-mcp-bridge serve --agent-id remote-agent --bus-url http://VPS_IP:8080
```

The remote agent uses `HttpBusStore` under the hood — all six MCP tools
work transparently over HTTP.

### REST API reference

| Method | Path | Auth | Request body | Success response |
|--------|------|------|-------------|-----------------|
| `GET` | `/health` | No | — | `{"status": "ok", "version": "...", "agents": N}` |
| `GET` | `/ping` | No | — | `{"server": "a2a-mcp-bridge", "version": "..."}` |
| `POST` | `/register` | Yes† | `{agent_id, metadata?}` | `{"ok": true}` |
| `POST` | `/send` | Yes† | `{sender, recipient, body, intent?, metadata?}` | `{"message_id": "...", "sent_at": "...", "recipient": "..."}` |
| `POST` | `/inbox` | Yes† | `{agent_id, limit?, unread_only?}` | `{"messages": [...]}` |
| `POST` | `/inbox_peek` | Yes† | `{agent_id, limit?, since_ts?}` | `{"messages": [...]}` |
| `POST` | `/list` | Yes† | `{active_within_days?}` | `{"agents": [...]}` |
| `POST` | `/subscribe` | Yes† | `{agent_id, timeout_seconds?, limit?}` | `{"messages": [...], "timed_out": bool}` |

† Auth required when `--api-key` is configured. Send `Authorization: Bearer <key>`.

#### Request schemas (Pydantic)

- **RegisterBody**: `agent_id: str`, `metadata: dict | None`
- **SendBody**: `sender: str`, `recipient: str`, `body: str`, `intent: str | None`, `metadata: dict | None`
- **InboxBody**: `agent_id: str`, `limit: int | None`, `unread_only: bool | None`
- **InboxPeekBody**: `agent_id: str`, `limit: int | None`, `since_ts: str | None`
- **ListBody**: `active_within_days: int | None`
- **SubscribeBody**: `agent_id: str`, `timeout_seconds: int | None`, `limit: int | None`

#### Error format

All errors return `{"error": {"code": "...", "message": "..."}}` with an appropriate HTTP status code.

| Code | HTTP | Meaning |
|------|------|---------|
| `TARGET_SELF` | 400 | Agent tried to send to itself |
| `TARGET_UNKNOWN` | 404 | Recipient not registered on the bus |
| `VALIDATION_ERROR` | 400 | Pydantic validation failed |
| `CONFIG_ERROR` | 500 | Missing signal-dir or wake-registry |

### Security considerations

- **Localhost by default** — `serve-facade` binds `127.0.0.1` and refuses to
  start on a non-local interface without `--api-key`.
- **Bearer token auth** — when `--api-key` is set, all mutation endpoints
  require `Authorization: Bearer <key>`. Verification uses `hmac.compare_digest`
  (constant-time comparison).
- **No TLS** — the facade does not terminate TLS. Put it behind a reverse
  proxy (nginx, Caddy) with HTTPS for production use, or use an SSH tunnel /
  WireGuard.
- **Webhook wake-up** — the facade forwards wake-up webhooks to the wake
  registry, best-effort. Failures are logged and never block the response.

### `HttpBusStore` client

The `HttpBusStore` class (`bus_store.py`) implements the `BusStore` protocol
using `httpx`. It is used automatically when `--bus-url` is passed to `serve`.

Key behaviours:
- Sends `X-Agent-Id` header on every request.
- Lazy-imports `httpx` — clear error message if missing.
- Network errors: `upsert_agent` best-effort (warns), reads return `[]`,
  `subscribe` returns `([], True)`, `send_message` raises `ValueError`.
- `close()` terminates the httpx client.

## Multi-session concurrency (ADR-001) & MCP docstring visibility

Since v0.5, every MCP tool's docstring opens with a short "Note
(multi-session)" paragraph that clarifies a critical property of the bus:
**`agent_id` identifies a profile, not a conversation**. A single profile
can be served concurrently by multiple Hermes sessions (Telegram, webhook
wake-up, cron, CLI) behind a local gateway cache — see
[`docs/adr/ADR-001-multi-session-concurrency.md`](docs/adr/ADR-001-multi-session-concurrency.md)
for the full model and the v0.5 bridge-side primitives that support it:

1. `agent_inbox_peek(since_ts, limit)` — read-only inbox view, no
   mark-as-read. Safe to call concurrently from any session. Used by the
   gateway for cache recovery and by tooling that wants a global view.
2. Optional `metadata.session_id` on `agent_send` — a reserved key (≤ 128
   chars) that gets hoisted into a dedicated column and surfaced at the top
   level of the `agent_inbox` / `agent_inbox_peek` payload, so recipients
   can correlate a reply with the exact sender session. Validation errors:
   `SESSION_ID_INVALID`, `SESSION_ID_TOO_LARGE`.
3. Structured logging — every tool call emits one record with a minimum
   schema (`ts`, `level`, `event`, `agent_id`) plus optional `session_id`,
   `message_id`, `target`, `duration_ms`, `body_hash`, `error_code`. Toggle
   JSON output via `A2A_LOG_JSON=1`. Bodies and caller metadata are never
   logged verbatim — only a 16-hex `blake2b(digest_size=8)` fingerprint.
4. Optional `session_id` parameter on every read tool (`agent_inbox`,
   `agent_inbox_peek`, `agent_list`, `agent_subscribe`) for log tagging
   from non-`agent_send` call sites.

### FastMCP docstring visibility caveat

FastMCP serves each MCP tool's first docstring line as its tool
description, but **ignores everything after the first blank line** when
streaming tool metadata to some MCP clients (observed on Hermes'
stdio client, 2026-04-22, with `fastmcp` 2.x). For that reason the full
"Note (multi-session)" paragraph is also present here in the README as a
single source of truth, and individual tool docstrings point back to the
ADR rather than duplicating the full design rationale.

**Also note**: FastMCP rejects any MCP tool parameter whose name starts
with an underscore (`InvalidSignature: Parameter _foo cannot start with '_'`).
The plumbing-style convention "prefix private-ish metadata with `_`" that
works in ordinary Python code is therefore unusable at the MCP boundary.
The public `session_id` parameter on the v0.5 read tools carries no
underscore for that reason, even though its role is conceptually
out-of-band from business input.

## CLI reference

```
a2a-mcp-bridge serve              # run the MCP stdio server (local SQLite)
a2a-mcp-bridge serve --bus-url URL # run MCP stdio server against a remote facade
a2a-mcp-bridge serve-facade [...]  # run the HTTP facade server (FastAPI + uvicorn)
a2a-mcp-bridge init               # initialize the SQLite schema
a2a-mcp-bridge register [...]     # pre-register one or all agents
a2a-mcp-bridge agents list        # show agents seen on the bus
a2a-mcp-bridge messages tail      # tail recent messages
a2a-mcp-bridge wake-registry init # build the webhook wake registry from ~/.hermes
```

### `serve-facade` options

| Option | Default | Description |
|---|---|---|
| `--db` | `~/.a2a-bus.sqlite` | SQLite database path. |
| `--host` | `127.0.0.1` | Bind address. Non-local values require `--api-key`. |
| `--port` | `8080` | Listen port. |
| `--api-key` / `A2A_FACADE_API_KEY` | *unset* | Bearer token for authentication. Required if `--host` is not `127.0.0.1` / `localhost`. |
| `--signal-dir` / `A2A_SIGNAL_DIR` | `/tmp/a2a-signals` | Signal directory for `subscribe` wake-ups. |
| `--wake-registry` / `A2A_WAKE_REGISTRY` | `~/.a2a-wake-registry.json` | Webhook wake registry path. |

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

- **v0.5** — Multi-session concurrency resolution via leader-at-gateway
  (see [ADR-001](docs/adr/ADR-001-multi-session-concurrency.md)). Bridge
  side: `agent_inbox_peek(since_ts)` non-destructive read, optional
  `session_id` metadata on `agent_send`, session-tagged logs, clarified
  tool docstrings. Paired with Hermes-side gateway cache work (tracked
  separately) that makes the gateway the sole bus subscriber per
  profile. Plus observability (per-tool stats, structured JSON logs).
- **v0.5.1** — HTTP facade server (`serve-facade`) + `HttpBusStore` client.
  Exposes the SQLite bus over REST (FastAPI) so agents on remote machines
  can participate. Optional Bearer token auth, Pydantic validation,
  async subscribe, webhook wake-up forwarding.
  See [ADR-006.1](docs/adr/ADR-006.1-http-bus-facade.md).

Planned:

- **Later** — Agent Cards (A2A-compliant metadata),
  of the MCP server, allowlist + token-based auth, optional Redis / Postgres
  backend for multi-host deployments, `agent_reply` + threaded conversations.

## Project ethos

- **Minimal.** No framework lock-in. Pure MCP + stdlib + SQLite. The only
  runtime deps added since v0.1 are optional (`httpx` for remote mode,
  `fastapi` + `uvicorn` for the facade — both behind `[remote]` / `[facade]`
  extras). Wake-up uses `urllib.request`.
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

pytest -q              # 209 tests, ≥85% coverage gate
ruff check src/ tests/ # lint
mypy src/              # strict type-check
```

### Optional dependency groups

```bash
pip install a2a-mcp-bridge[facade]   # FastAPI + uvicorn + httpx (run the facade server)
pip install a2a-mcp-bridge[remote]   # httpx only (connect to a remote facade)
pip install a2a-mcp-bridge[dev]      # pytest, ruff, mypy, etc.
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
