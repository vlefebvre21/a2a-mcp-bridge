# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-04-21

### Added
- MCP server with three tools: `agent_send`, `agent_inbox`, `agent_list`.
- SQLite-backed message store (single-file, zero-dependency).
- Typer CLI: `serve`, `init`, `agents list`, `messages tail`.
- Agent identity via `A2A_AGENT_ID` env var (enforced, no silent default).
- GitHub Actions CI on Python 3.11, 3.12, 3.13.
- 85%+ test coverage gate.
