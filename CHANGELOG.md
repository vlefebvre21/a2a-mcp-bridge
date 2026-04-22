# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.2] - 2026-04-22

Forum-topic support for Telegram wake-ups, resolving Issue #4 (all nine
VLBeau agents were sharing the same DM chat, so wake-ups crossed wires).
Each recipient may now declare a `thread_id` in the registry and wake-ups
are routed to the corresponding forum topic inside the shared supergroup.

### Added
- **`WakeEntry.thread_id`** — new optional `int | None` field. Absent /
  `None` preserves v0.4.1 behaviour (DM or `General` topic). When set, the
  `sendMessage` POST includes Telegram's `message_thread_id` parameter so
  the wake-up lands in the correct forum topic.
- **`load_registry()`** now reads an optional `"thread_id": <int>` per
  entry, rejecting non-integer values (including the Python `True`/`False`
  footgun) with a clear `ValueError`.
- **`wake-registry init` intelligent merge** — when a previous registry
  exists, manually-edited `thread_id` fields are carried forward even
  though `bot_token` / `chat_id` are refreshed from the Hermes `.env`
  sources. Operators can tweak topic routing in the JSON without fearing
  the next regeneration will nuke their change. Corrupt prior registries
  are silently ignored so `init` remains idempotent.
- **`wake-registry init` output table** now shows a third `thread_id`
  column and a dim footer reporting how many entries had a `thread_id`
  preserved from the prior registry.

### Tests
- 4 new tests in `tests/test_wake.py` covering `thread_id` loading, the
  boolean-as-int footgun, `message_thread_id` inclusion in the POST, and
  explicit verification that the v0.4.1 behaviour is unchanged when no
  `thread_id` is set.
- 3 new tests in `tests/test_cli_wake_registry.py` covering the intelligent
  merge: `thread_id` preservation on re-init, clean first-run baseline
  without `thread_id`, and recovery from a corrupt prior registry.

### Backwards compatibility
Zero breaking changes. Registries written by v0.4.1 load unchanged and
continue to route wake-ups exactly as before. New forum-topic routing is
opt-in: an entry needs `thread_id` for the new behaviour to kick in.

### Migration notes
To adopt forum topics:
1. Create a Telegram supergroup with `is_forum: true` (enable Topics in
   group settings).
2. Create one forum topic per agent (via Bot API `createForumTopic` or
   the Telegram app UI).
3. Edit `~/.a2a-wake-registry.json`: set each entry's `chat_id` to the
   supergroup's `-100...` id and add `"thread_id": <N>` where `<N>` is
   the `message_thread_id` returned by `createForumTopic`.
4. Restart the Hermes gateways so the MCP bridge child processes reload
   the registry (they cache it at startup).
5. Subsequent `a2a-mcp-bridge wake-registry init` runs will preserve the
   `thread_id` values you added.

## [0.4.1] - 2026-04-21

Polish follow-up to v0.4.0, addressing the two nits flagged during the PR #3
review (Issue #5). No behaviour change for clients.

### Changed
- **Performance** — `_bridge_version()` is now cached with
  `@functools.lru_cache(maxsize=1)`. The package version is immutable within a
  server process's lifetime, and `importlib.metadata.version` scans installed
  distributions on each call (~5–10 ms warm). `agent_ping` is now free to spam.
- **Dependency ceiling** — pin `mcp>=1.0,<2` in `pyproject.toml`. The
  `A2AMcp.run_stdio_async` override mirrors the upstream implementation; a
  major-version bump of the MCP SDK could silently drop lifecycle hooks we
  haven't anticipated, so we gate on `<2` until the override is reviewed
  against the new API.

### Documentation
- Extended docstring on `A2AMcp` with a `warning` block documenting the
  sync requirement with upstream `FastMCP` and pointing at the upstream PR
  path that would eventually obsolete the override (exposing
  `notification_options` on `FastMCP.__init__`).

### Tests
- New regression test `test_bridge_version_is_cached` in `tests/test_server.py`
  asserting exactly one underlying `_pkg_version` lookup across three calls.
- Total: **87 tests** (was 86 at v0.4.0).

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
- 5 new tests (86 total).

### Changed
- **Telegram wake-up message format** — now names the reply-to `agent_id`
  explicitly and shows the literal `agent_send(target="...")` call signature.
  The previous v0.3 text could be misread by an LLM agent that confused the
  A2A sender with a Telegram surface identity (bot username / chat peer),
  causing replies to be routed to the wrong target. Regression test added.

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
