# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
