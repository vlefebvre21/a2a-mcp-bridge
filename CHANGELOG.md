# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.0] - 2026-04-23

**Theme** — bridge-side primitives for multi-session concurrency (ADR-001
Option A′: leader-at-gateway). The bridge now ships the building blocks
the Hermes gateway needs to recover its inbox cache across process
restarts and to correlate log lines across concurrent agent sessions.

### Added
- **`agent_inbox_peek(since_ts?, limit=50)`** — read-only inbox view that
  never mutates `read_at`. Returns the same message payload shape as
  `agent_inbox`, including already-read messages with their `read_at`
  populated. When `since_ts` is supplied, messages are returned ASC by
  `created_at` (replay order); otherwise newest-first. Primary consumer
  is the Hermes gateway reconstructing its local cache after a restart
  or when lagging behind the bus, but external tooling can also use it
  to inspect an agent's history without consuming pending messages.
- **`messages.sender_session_id` column** — optional opaque correlator
  (≤ 128 bytes UTF-8, nullable) stored alongside every inbound message. Payload
  shapes of `agent_inbox`, `agent_inbox_peek`, and `agent_subscribe` now
  include `"sender_session_id"` for every message (always present, `null`
  when absent). Pre-v0.5 callers that ignore unknown keys are unaffected.
- **`session_id` session tagging on read tools** — `agent_inbox`,
  `agent_inbox_peek`, `agent_list`, and `agent_subscribe` accept an
  optional `session_id: str | None = None` kwarg. The bridge does not
  interpret the value beyond tagging its log events with it, enabling
  end-to-end log correlation when multiple gateway sessions operate
  concurrently on the same bridge (ADR-001 §4 #3).
- **`agent_send` session propagation** — when the caller supplies
  `metadata={"session_id": "..."}`, the value is stored in
  `messages.sender_session_id` and surfaced to the recipient's inbox
  payload. Non-string or oversize values are rejected with error codes
  `SESSION_ID_INVALID` / `SESSION_ID_TOO_LARGE` (≤ 128 bytes UTF-8).
- **`src/a2a_mcp_bridge/logging_ext.py`** — structured logging helper.
  Minimum schema: `{ts, level, event, agent_id}`; optional extras:
  `session_id, message_id, target, duration_ms, body_hash, error_code`.
  Two output formats, toggled at import time by `A2A_LOG_JSON`:
  - `A2A_LOG_JSON=1` → one JSON object per line (log-shipper friendly).
  - unset / anything else → classic plain-text matching the pre-v0.5
    format (no existing tailer breaks).
  Message bodies are NEVER logged verbatim; only a 16-hex
  `blake2b(digest_size=8)` `body_hash` is emitted — traceable, non-PII.

### Changed
- Every MCP tool handler now measures its own wall time and emits one
  INFO log record at completion (or WARN on the `agent_send` error
  path), carrying `event`, `agent_id`, `duration_ms`, and when relevant
  `session_id`, `target`, `message_id`, `body_hash`, `error_code`.
- **Schema migration is idempotent.** Existing DBs add the new column
  via `ALTER TABLE ... ADD COLUMN sender_session_id TEXT` on first open;
  re-opening an already-migrated DB is a no-op. Downgrading to v0.4.x
  is safe — the column is simply unused.

### Notes / caveats
- **FastMCP parameter convention**: the `session_id` parameter on read
  tools is spelled without an underscore prefix. FastMCP's signature
  validator rejects any tool parameter starting with `_`
  (`InvalidSignature: Parameter _session_id ... cannot start with '_'`).
  Earlier WIP used `_session_id` as a "plumbing hint" — that convention
  is incompatible with the MCP boundary.
- **ADR-002 (`docs/adr/ADR-002-wake-intent-coupling.md`)** documents a
  related post-mortem (wake-intent coupling) observed during v0.5
  development on the `docs/adr-002-wake-intent` branch. That branch is
  being landed separately; it has no code dependency on v0.5.

### Tests
- 30 new tests across `tests/test_migrations.py`, `tests/test_inbox_peek.py`,
  `tests/test_session_id.py`, `tests/test_logging.py`.
- Full suite: **142 passed**, ruff clean, mypy clean on `src/`.

## [0.4.4] - 2026-04-22

**Major** — wake-up transport migrates from Telegram to local HTTP webhooks.

v0.4.3 shipped a shared-wake-bot Telegram model that routed wake-ups
through a single "crier" bot to work around the "a bot never sees its own
messages" rule in supergroups. That fix delivered the wake-up message to
the correct forum topic, but the recipient's Hermes gateway polls **its
own** Telegram bot and therefore never received the update: the message
landed in the topic without ever triggering the recipient's agent loop.

v0.4.4 abandons Telegram for wake-up entirely. Each Hermes gateway
exposes a local HTTP webhook endpoint (``http://127.0.0.1:<port>/webhooks/wake``)
that triggers a real agent session when POSTed to. The bridge signs an
HMAC-SHA256 JSON payload with a shared secret and POSTs directly to the
recipient's endpoint, bypassing Telegram. The recipient's gateway
validates the signature, spawns a session, and the agent reads its inbox.

Telegram remains available for operators who want visibility into A2A
traffic (topic-based supergroups still display messages posted by the
Hermes agents themselves when they run), but it is no longer on the
wake-up path.

### Changed
- **``wake.py``** — ``TelegramWaker`` replaced by ``WebhookWaker``. The
  waker POSTs ``{"sender": ..., "target": ..., "source": "a2a-mcp-bridge"}``
  as a compact JSON body, signed via HMAC-SHA256 under a shared secret,
  to each recipient's configured ``wake_webhook_url``. Self-wake guard
  and best-effort error swallowing are preserved unchanged.
- **``load_registry``** returns ``(shared_secret, entries)`` with new
  ``WakeEntry`` shape ``{wake_webhook_url: str}``.
- **Registry format v0.4.4**::

    {
      "wake_webhook_secret": "<64-hex>",
      "agents": {
        "vlbeau-main":  {"wake_webhook_url": "http://127.0.0.1:8651/webhooks/wake"},
        "vlbeau-glm51": {"wake_webhook_url": "http://127.0.0.1:8653/webhooks/wake"}
      }
    }

- **``wake-registry init``** now reads each Hermes profile's
  ``config.yaml`` (``platforms.webhook.{host, port, secret}``) and
  ``webhook_subscriptions.json`` (per-route ``wake`` secret wins over
  global, matching the adapter's resolution order). All profiles must
  share the **same** webhook secret; divergent profiles are skipped with
  a visible warning in the CLI summary.
- **New dep** ``pyyaml>=6.0`` (needed by ``wake-registry init`` to read
  profile configs).

### Removed
- ``TelegramWaker`` class (replaced by ``WebhookWaker``).
- ``--legacy-format``, ``--wake-bot-profile``, ``--reset-chat-ids`` flags
  on ``wake-registry init``. The v0.4.4 CLI has no legacy-compat path: a
  pre-v0.4.4 registry is **detected** (``wake_bot_token`` or per-agent
  ``bot_token`` keys), logged with a ``migrating`` banner, and
  **overwritten** with a fresh v0.4.4 payload.

### Fallback / error handling
- Wake-up is best-effort (unchanged contract from v0.3+): ``agent_send``
  persists to SQLite first, wakes second. A webhook POST failure logs a
  ``WARNING`` and returns ``False`` — the message is still in the bus,
  the recipient will see it on next poll or next wake.
- A legacy-format registry under v0.4.4 disables wake-up (empty registry
  returned from ``load_registry``, migration WARNING logged). The
  bridge continues to store and deliver messages via SQLite + signals;
  operators must run ``a2a-mcp-bridge wake-registry init`` to restore
  wake-up.

### Migration

1. Upgrade the bridge: ``uv tool install --force a2a-mcp-bridge``
2. Each gateway profile must have ``platforms.webhook.enabled: true``
   in its ``config.yaml`` with a unique ``port`` and a ``secret``. The
   same ``secret`` must be used across all profiles so they can share
   the HMAC key.
3. Run ``a2a-bridge wake-registry init`` to regenerate
   ``~/.a2a-wake-registry.json`` in the v0.4.4 format.
4. Restart gateways. The first wake after restart confirms the new
   transport is live (``agent_inbox`` bumps ``last_seen_at`` for the
   recipient within seconds of ``agent_send``).

### Tests
- 111/111 pass (+4 net vs v0.4.3.1). Full rewrite of ``test_wake.py`` and
  ``test_cli_wake_registry.py`` against the webhook format; integration
  tests verify persist-before-wake ordering and that legacy registries
  don't crash ``build_server``.

## [0.4.3.1] - 2026-04-22

Hotfix for `wake-registry init` silently resetting `chat_id` overrides.

### Fixed
- **`wake-registry init` now preserves `chat_id` across regenerations**, not
  just `thread_id`. In v0.4.3 a second invocation would re-read `chat_id`
  from every profile's `.env` and overwrite any supergroup id the operator
  had set in the registry. When profile `.env` files carry a DM chat id
  (typical Hermes default) but the registry was pointing at a Telegram
  supergroup (`-100...`), this turned every wake-up into a DM silently.
  The merge now carries `chat_id` forward just like `thread_id`.

### Added
- **`wake-registry init --reset-chat-ids`** — opt-in flag to force the old
  v0.4.3 behaviour, re-reading `chat_id` from each profile's `.env` even
  when the existing registry already has one. `thread_id` is still
  preserved in this mode.
- The CLI summary now reports how many `chat_id` and `thread_id` entries
  were preserved (vs. pulled from `.env`) so operators get a clear signal
  that the merge actually carried overrides forward.

### Migration
No action required. A regular `a2a-bridge wake-registry init` under v0.4.3.1
on a v0.4.3 registry will preserve your existing `chat_id` and `thread_id`
overrides — which is what v0.4.3 _should_ have done. If you want the old
reset-everything behaviour on purpose, add `--reset-chat-ids`.

## [0.4.3] - 2026-04-22

Shared-wake-bot format. Resolves the self-wake dead-end introduced by
v0.4.2's forum-topic routing: in a Telegram supergroup, a bot never
receives messages it posted itself, so routing the wake-up through the
recipient's own token made the wake-up land in the topic but never
trigger the recipient's gateway. v0.4.3 posts every wake-up through a
single shared bot (typically `vlbeau-main`'s `@VLBeauBot`) so each
recipient sees an actual incoming update from a different sender.

### Added
- **Registry format v2** — top-level `wake_bot_token` + `agents` object::

    ```json
    {
      "wake_bot_token": "123:ABC",
      "agents": {
        "vlbeau-main":  {"chat_id": "-1001234567890", "thread_id": 5},
        "vlbeau-glm51": {"chat_id": "-1001234567890", "thread_id": 7}
      }
    }
    ```

  The single `wake_bot_token` is used to POST every wake-up. Each entry
  carries only `chat_id` (+ optional `thread_id`) — per-agent
  `bot_token` is no longer needed or stored.

- **Self-wake guard** — `TelegramWaker.wake()` silently returns `False`
  when `agent_id == sender_id`, preventing wake-loops on the same
  gateway if a buggy caller ever sends a message to itself.

- **`uses_shared_token` property** on `TelegramWaker` for introspection
  and future observability work.

- **`message_thread_id` alias** — the registry accepts both `thread_id`
  (preferred, shorter) and `message_thread_id` (Telegram-native) on
  each entry. Operators typing the Bot API name by reflex no longer get
  a silent drop.

- **CLI `--wake-bot-profile <name>`** — selects the Hermes profile whose
  `TELEGRAM_BOT_TOKEN` becomes the shared wake bot (default: `main`).

- **CLI `--legacy-format`** — emits the v0.3 - v0.4.2 JSON shape for
  operators who explicitly want per-agent DM wake-up (no supergroup).

### Changed
- **`load_registry()`** now returns a `(shared_token, entries)` tuple
  instead of just `entries`. Callers inside the bridge are updated
  (`server._load_waker`). External callers must unpack the tuple.

- **`wake-registry init`** output now includes the format mode in its
  title (`"shared-bot (main)"` vs `"legacy per-agent"`) so operators
  can tell at a glance which shape they just wrote.

- **`wake-registry init` errors loudly** when the new format is
  requested but no wake-bot token can be resolved (profile missing
  `TELEGRAM_BOT_TOKEN` and no prior registry to reuse from). Previously
  this would have silently produced an unusable registry.

### Deprecated
- **Legacy per-agent `bot_token` format** (v0.3 - v0.4.2). Still accepted
  by `load_registry()` — existing registries keep working — but
  `load_registry()` now logs a migration warning the first time it
  parses one. Run `a2a-mcp-bridge wake-registry init` to upgrade.

### Tests
- Test count: 111 (up from 96 in v0.4.2), ruff clean, mypy clean.
- New in `tests/test_wake.py`:
  - Full `TestLoadRegistrySharedBot` class (6 tests: happy path, empty
    token rejected, agents must be dict, chat_id required,
    `message_thread_id` alias accepted, no legacy warning).
  - `TestTelegramWakerSharedBot` (4 tests: shared token wins over entry
    token, thread_id routing, `uses_shared_token` flag, orphan entry
    with no token returns False).
  - `TestSelfWakeGuard` (2 tests: legacy + shared mode, both skip and
    make no HTTP call).
  - `test_legacy_format_warns_about_migration` — regression guard that
    the deprecation warning is actually emitted.
- New in `tests/test_cli_wake_registry.py`:
  - `test_wake_registry_init_defaults_to_shared_bot_format`
  - `test_wake_registry_init_uses_custom_wake_bot_profile`
  - `test_wake_registry_init_errors_when_wake_bot_token_missing`
  - `test_wake_registry_init_reuses_prior_wake_bot_token`
  - `test_wake_registry_init_preserves_thread_id_across_regenerations`
  - `test_wake_registry_init_migrates_legacy_prior_registry`
  - `test_legacy_format_emits_old_shape`

### Migration
Existing v0.4.2 registries continue to work without change. To adopt
the shared-wake-bot format (recommended for all supergroup setups):

1. Verify `~/.hermes/profiles/main/.env` has a valid
   `TELEGRAM_BOT_TOKEN` — that token becomes the shared wake bot.
2. Run `a2a-mcp-bridge wake-registry init` — the command detects any
   prior `thread_id` overrides and carries them forward into the new
   format.
3. Restart the Hermes gateways so MCP bridge child processes reload
   the registry.

Operators who prefer per-agent DM wake-ups (no supergroup) should run
`a2a-mcp-bridge wake-registry init --legacy-format` instead.

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
