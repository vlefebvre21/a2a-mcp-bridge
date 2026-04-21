# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-04-21

### Added
- **`agent_ping` tool** — returns `{"server", "version", "agent_id"}`. Useful
  for detecting a stale stdio child after an `a2a-mcp-bridge` upgrade:
  clients can call it at session start and compare the reported version
  against the installed package to decide whether to prompt an operator
  restart.
- **`tools.listChanged` capability advertised** at handshake time via a
  `FastMCP` subclass (`A2AMcp`) that injects
  `NotificationOptions(tools_changed=True)`. The project still registers
  tools statically, but the capability is declared so future plugin-style
  dynamic tool additions become a drop-in change without client restart.
- Startup log now includes the running bridge version
  (`starting a2a-mcp-bridge ... version=0.4.0`).
- 4 new tests (85 total).

### Documentation
- `server.py` carries an extended module comment explaining why
  `list_changed` alone cannot solve the "client still talks to an old
  stdio server after upgrade" problem, and how `agent_ping` complements it.

## [0.3.0] - 2026-04-21

### Added
- **Telegram wake-up on `agent_send`.** When a recipient is listed in the
  wake registry (`A2A_WAKE_REGISTRY`, default `~/.a2a-wake-registry.json`),
  `agent_send` fires a Telegram prompt to the recipient's bot so their gateway
  processes the new message without having to poll. Best-effort: any failure
  (missing entry, HTTP error, network error) is logged and never blocks the
  canonical SQLite write.
- New `wake` module (`src/a2a_mcp_bridge/wake.py`) with:
  - `TelegramWaker.wake(agent_id, sender_id)` — POSTs to
    `https://api.telegram.org/bot<token>/sendMessage` via stdlib
    `urllib.request` (no new runtime deps).
  - `load_registry(path)` — reads a JSON file mapping each `agent_id` to
    `{bot_token, chat_id}`.
- **CLI command** `a2a-mcp-bridge wake-registry init` that builds the JSON
  registry by scanning `~/.hermes/profiles/<name>/.env` for each profile
  (→ `vlbeau-<name>`) and optionally `~/.hermes/.env` (→ `vlbeau-opus`). Reads
  `TELEGRAM_BOT_TOKEN` and `TELEGRAM_HOME_CHANNEL`, skips incomplete profiles
  silently. Supports `--hermes-profiles`, `--hermes-root`, and `-o/--output`.
- New `A2A_WAKE_REGISTRY` environment variable to override the registry path.
- 27 new tests (13 for `wake.py`, 6 for the CLI command, 8 for end-to-end
  integration). Total: **81 tests** across the project.

### Changed
- `tool_agent_send` now accepts an optional `waker` parameter. Callers that
  omit it get the v0.2 behaviour unchanged.
- `build_server` loads the wake registry automatically at startup if the env
  var or default file points to a readable JSON map. A missing or malformed
  registry is logged and wake-up is disabled — the server still boots.

### Unchanged (contract preserved)
- The four MCP tools (`agent_send`, `agent_inbox`, `agent_list`,
  `agent_subscribe`) keep their exact signatures and return payloads. No new
  tool in this release — wake-up is purely a server-side effect.

## [0.2.0] - 2026-04-21

### Added
- **`a2a-mcp-bridge register`** CLI command to pre-populate agents on the bus
  without waiting for their first MCP tool call. Supports `--agent-id <ID>` for
  a single agent, or `--all --hermes-profiles <dir>` to register every Hermes
  profile as `vlbeau-<profile>` in one shot. Fixes the v0.1 auto-register gap.
- **Real-time delivery** via signal files. `agent_send` now writes a file to
  `A2A_SIGNAL_DIR` (default `/tmp/a2a-signals/<recipient>.notify`) whenever a
  message is stored. Advisory only — the SQLite store remains authoritative.
- **`agent_subscribe` MCP tool** — long-poll primitive that blocks up to
  `timeout_seconds` (capped at 55 s) waiting for a new message and returns it
  immediately when the recipient's signal fires. Returns instantly if messages
  are already pending. Payload shape matches `agent_inbox` plus a `timed_out`
  flag.
- New `A2A_SIGNAL_DIR` environment variable to override the signal directory.
- 20 new tests (7 for `register`, 13 for signals + `agent_subscribe`). Total
  54 tests, 92% coverage.

### Changed
- `tool_agent_send` now accepts an optional `signal_dir` parameter. Callers
  that omit it get the v0.1 behaviour unchanged.

### Unchanged (contract preserved)
- The three v0.1 MCP tools (`agent_send`, `agent_inbox`, `agent_list`) keep
  their exact signatures and return payloads. Existing clients keep working.

## [0.1.0] - 2026-04-21

### Added
- MCP server with three tools: `agent_send`, `agent_inbox`, `agent_list`.
- SQLite-backed message store (single-file, zero-dependency).
- Typer CLI: `serve`, `init`, `agents list`, `messages tail`.
- Agent identity via `A2A_AGENT_ID` env var (enforced, no silent default).
- GitHub Actions CI on Python 3.11, 3.12, 3.13.
- 85%+ test coverage gate.
