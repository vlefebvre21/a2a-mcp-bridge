# ADR-007 — File Transfer Primitive (Out-of-Band Payload)

- **Status:** Accepted — implemented (v0.7.0 same-machine, v0.7.1–v0.7.2 façade Phase C, v0.7.4–v0.7.5 cross-NAT hardening)
- **Date:** 2026-05-01
- **Last updated:** 2026-05-02 (updated status to reflect shipped cross-machine transfer; added §8 post-implementation notes for #42 and #44)
- **Context window:** v0.6.2 → v0.7
- **Authors:** VLBeauClaudeOpus (architect, `vlbeau-opus`), Vincent Lefebvre
- **Related issue:** [#33](https://github.com/vlefebvre21/a2a-mcp-bridge/issues/33)

## 1. Context

The A2A bus carries every `agent_send` body inline: the full payload string
travels through the SQLite `messages.body` column, is pulled by the recipient
via `agent_inbox` / `agent_inbox_peek`, and is **rehydrated into the receiving
LLM's context window** the moment the MCP client presents the message.

This works well for control-plane traffic (hundreds of bytes to a few KB:
"please do X", "status: done", small JSON payloads). It breaks down as soon
as an agent needs to hand off a **payload that the LLM does not actually need
to read**, only to forward or store.

### 1.1 Triggering incident (2026-05-01)

`vlbeau-magent` delegated a task to `vlbeau-macqwen36` (Qwen 3.6 27B Q8, served
from a Mac over the ADR-006 reverse-SSH tunnel, **64 KB effective context
window**): "summarise this YouTube transcript and mail the result back".

`macqwen36` produced the summary correctly, but the handoff **back to magent**
required sending the ~28 845 chars transcript as part of an intermediate
message. The transcript alone consumed ~30 KB — nearly half the context
budget — before the agent could even compose its response. The session
saturated mid-generation.

This is a **structural problem**, not a local-model problem. Any
forwarding-pattern (agent A fetches → agent B routes → agent C stores) forces
every hop to *read into context* something it only needs to *pass along*.
Agents with larger windows (Claude Opus at 200 K, GPT-5 at 1 M) pay the same
tax in tokens-per-dollar even if they don't saturate.

### 1.2 What we explicitly want to preserve

- **MCP tool surface stability.** No breaking change to `agent_send`,
  `agent_inbox`, `agent_inbox_peek`, `agent_subscribe`, `agent_list`,
  `agent_ping`. Existing flows keep working.
- **ADR-001 semantics.** Atomic mark-as-read, `sender_session_id`,
  `since_ts` replay — all remain on the message channel.
- **ADR-002 intent semantics.** The wake-up axis (`fyi` skips wake, others
  wake) stays attached to the message, independent of any file attachment.
- **ADR-006 distributed fleet compatibility.** Whatever we add must work for
  agents living on different machines reaching the bus via the HTTP façade
  (ADR-006.1).

## 2. Problem statement

Provide a way for one agent to hand a blob of bytes to another agent **without
the blob ever passing through either LLM's context window**, while keeping the
coordination metadata (who, what, when, why) on the existing message channel.

### 2.1 Constraints and risks

| # | Concern | Severity | Example / rationale |
|---|---------|----------|---------------------|
| 1 | **Path traversal** | High | Sender names a file `"../../etc/passwd"`; recipient reads it directly. |
| 2 | **Unbounded disk growth** | High | Staged files never deleted — the VPS fills up silently. |
| 3 | **Cross-machine unreachability** | High | Mac agent produces a file; VPS agent has no access to the Mac filesystem. |
| 4 | **Integrity** | Medium | Recipient reads a half-written file because sender hasn't flushed yet. |
| 5 | **Auth bypass** | Medium | Any process on the same machine reads staged files regardless of which agent they target. |
| 6 | **Metadata pollution of the bus** | Low | Encoding small blobs into `messages.body` bloats the SQLite file and index pages. |
| 7 | **Backward compatibility** | Low | Pre-v0.7 clients must not crash when they see a transfer-reference message. |

## 3. Options considered

### 3.1 Option A — Shared staging directory (local filesystem)

**Design sketch.**

- New env var `A2A_TRANSFER_DIR` (default `~/.a2a-transfers/`, same
  resolution pattern as `A2A_SIGNAL_DIR`).
- New MCP tool `agent_send_file(target, file_path, description="",
  intent="triage", expires_in=86400)`:
  1. Hash-and-copy the source into
     `<transfer_dir>/<uuid>/<sha256[:16]>_<filename>`.
  2. Write a sibling manifest `<transfer_dir>/<uuid>/meta.json`
     (sender, recipient, size, sha256, created_at, expires_at, description).
  3. Call the existing `agent_send` with a structured body:
     ```json
     {"kind": "file_transfer", "transfer_id": "<uuid>", "path":
      "<absolute path>", "size": N, "sha256": "...", "filename": "...",
      "description": "..."}
     ```
  4. Recipient receives a normal inbox message. The LLM sees only the small
     metadata JSON (a few hundred bytes), not the file content.
- New MCP tool `agent_fetch_file(transfer_id) -> {path, size, sha256,
  filename, description, expires_at}`: resolves a transfer_id to a local
  path the recipient can open with `read_file`. Validates that the caller
  is the declared recipient.
- New MCP tool `agent_delete_file(transfer_id)`: caller-scoped deletion
  (sender or recipient can delete).
- Background cleanup: periodic sweep (similar to the rate-limiter's
  `prune_stale`) deletes transfers past `expires_at`. Default TTL 24 h.

**✅ Pros.**

- Zero network hop for same-machine transfers (most VPS-internal traffic).
- Trivial for the LLM: one small metadata message per file, no encoding.
- Path allow-list via `A2A_TRANSFER_DIR` prefix check kills risk #1.
- TTL + quota kill risk #2.
- Works with the existing SQLite bus — no schema change required for
  the transfer record itself (it's just a message body).

**❌ Cons.**

- **Does not work cross-machine.** The file lives on one filesystem. If
  sender is on the Mac (ADR-006 remote node) and recipient is on the VPS,
  the recipient's `fetch_file` call finds an empty path. This is the exact
  triggering case (§1.1) — so Option A *alone* does not solve the
  motivating problem.
- Race between sender copying and recipient reading (risk #4) must be
  handled with atomic rename (`tmp → final`).
- Local-filesystem auth is weaker than HTTP Bearer — any process running
  as the same UNIX user reads everything (risk #5). We mitigate with
  `0600` file permissions and per-transfer UUID-dirs, but it's not
  enforced at protocol level.

### 3.2 Option B — Inline blob via base64 in message body

**Design sketch.**

- No new tool. Extend `agent_send` convention so a body shaped like
  `{"kind": "blob", "filename": "...", "base64": "..."}` is understood
  by receiver-side helpers as a file.
- Cap at ~10 KB raw (~13 KB base64) — anything larger refused.

**✅ Pros.**

- Zero infrastructure. Works everywhere today. Trivial to implement
  (client-side helper only).
- No cleanup, no TTL, no path traversal risk.

**❌ Cons.**

- **Still pollutes LLM context.** The base64 blob lands in
  `messages.body`, the LLM reads it back on inbox retrieval. Exactly the
  problem we are trying to solve — just with more characters per byte
  (base64 expansion: +33%).
- 10 KB cap is a narrow sweet spot: too small for transcripts, too big
  for what the bus is designed for (control plane). Most interesting
  payloads (transcripts, reports, datasets) sit in the forbidden zone.
- Bloats the SQLite file (risk #6) — the bus is a log, not a blob store.

Option B is listed for completeness. It does not meet requirement §2
(blob must not enter LLM context) and is **rejected**.

### 3.3 Option C — HTTP transfer endpoint on the ADR-006 façade

**Design sketch.**

- Extend the HTTP façade (ADR-006.1) with three endpoints:
  - `POST /transfers` — multipart upload, returns
    `{transfer_id, sha256, size, expires_at}`.
  - `GET /transfers/<id>` — stream download, requires
    `Authorization: Bearer <A2A_FACADE_API_KEY>` AND the caller's
    `agent_id` must match the transfer's declared recipient (enforced
    server-side via a new `transfers` table).
  - `DELETE /transfers/<id>` — scoped to sender or recipient.
- New SQLite table `transfers(id TEXT PK, sender_id, recipient_id,
  filename, size, sha256, created_at, expires_at, path_on_disk, deleted_at)`.
  Idempotent migration pattern (cf. `_add_column_if_missing` in store.py).
- New MCP tool `agent_share_url(target, url_or_path, description="",
  intent="triage")` that:
  1. If `url_or_path` is a local path, uploads it via `POST /transfers`
     (or copies locally if on the same node).
  2. Sends the resulting `transfer_id` + metadata to the recipient via
     `agent_send` (same body schema as Option A).
- Recipient `agent_fetch_file(transfer_id)` becomes transport-aware: if
  running against a local bus, reads directly from disk; if running
  against an `--bus-url`, does `GET /transfers/<id>` through the façade.
- Rate limiting: add `A2A_RATE_LIMIT_TRANSFER` with a conservative
  default (e.g. 6 uploads/min per IP — transfers are expensive).
- The existing `_EXEMPT_ROUTES` frozenset in `rate_limit.py`
  (`/health`, `/ping`) stays; `/transfers/*` is NOT exempt.

**✅ Pros.**

- **Works cross-machine.** This is the only option that solves the
  triggering incident (§1.1) directly.
- Reuses ADR-006.1 auth machinery (Bearer token, constant-time compare).
- Server-side ACL prevents same-UNIX-user leak (risk #5) at the protocol
  boundary.
- A single primitive works for all topologies — local bus agents can
  still use Option A as a fast path, but the fallback is uniform.

**❌ Cons.**

- **Heavier to implement.** ~500–800 LOC vs ~300 for Option A. Requires:
  schema migration, new façade endpoints with streaming, multipart body
  parsing, new rate-limit bucket, new client code path in
  `HttpBusStore`, new CLI flags on `serve`.
- **Adds operational surface.** One more endpoint class to monitor, one
  more table to vacuum. Transfers reference paths on disk — the façade
  process must have write access to `A2A_TRANSFER_DIR`.
- **Size limit tension.** Streaming is easy; policing request size is
  harder. Need a `max_transfer_size` env var and a 413 response path,
  plus a way for the client to check the cap before starting upload.
- **No TLS in core.** The façade still relies on a reverse proxy or SSH
  tunnel for transport encryption (ADR-006.1 §Authentication model) —
  that means blob contents traverse localhost in clear on the VPS, and
  the SSH tunnel on the Mac↔VPS leg. Acceptable for the current
  threat model but worth naming.

### 3.4 Option A + C (combined, two-phase)

The combo: both primitives coexist. Client-side helper picks the fastest path:

```
agent_send_file(target, file_path, ...):
    if recipient is on the same node (agent record has same node_id):
        → Option A (local staging dir)
    else:
        → Option C (upload to façade, attach URL)
```

This is the shape we recommend. See §4.

## 4. Decision

**Adopt Option A + C in that order, shipped as two sequential PRs:**

- **v0.7.0 — PR1: Option A (same-machine staging).** Delivers the feature
  for the 90% case (VPS↔VPS, Mac↔Mac internal routing) and validates
  the wire protocol (`{"kind": "file_transfer", ...}` body schema, MCP
  tool shapes). Low-risk, small diff.
- **v0.7.1 — PR2: Option C (cross-machine façade endpoints).** Adds the
  `/transfers` routes, the `transfers` SQLite table, and the
  transport-aware dispatch in `agent_send_file`. Solves the §1.1
  triggering case.

**Why this order, not the other:**

- PR1 gives us a working end-to-end flow on a single box, with exhaustive
  tests for the path-traversal / TTL / ACL logic that PR2 will reuse.
- PR2 then only has to add the HTTP transport layer — the security model,
  the manifest schema, and the MCP tools are already shipped.
- If we shipped C first, we'd carry façade complexity immediately for a
  feature most agents won't need. Doing A first also gives Vincent a
  natural review gate.

**Why not just C (skip A):** tempting because C is a superset, but C on
localhost still pays one HTTP round-trip per transfer vs a `shutil.copy`.
For chatty internal pipelines that's a measurable slowdown. Keeping A as
the local fast-path is cheap once A is shipped.

**Why not the proposed Option B at all:** doesn't meet §2 (blob must
bypass LLM context). See §3.2.

### 4.1 Wire protocol (frozen by this ADR, applies to both PRs)

**Message body (JSON, UTF-8)** when `agent_send` carries a transfer reference:

```json
{
  "kind": "file_transfer",
  "version": 1,
  "transfer_id": "<uuid4>",
  "filename": "transcript.md",
  "size": 28845,
  "sha256": "7b2c9f...",
  "description": "YouTube summary for magent",
  "expires_at": "2026-05-02T19:42:41Z",
  "locator": {
    "scheme": "file" | "http",
    "path": "/home/vince/.a2a-transfers/<uuid>/..." ,
    "url":  "http://bus.vlbeau.local:PORT/transfers/<uuid>"
  }
}
```

- `version` pins the schema. Bumped if/when we change shape.
- Exactly one of `locator.path` or `locator.url` is populated depending
  on scheme.
- Recipients unaware of `kind=file_transfer` (pre-v0.7 agents) see the
  raw JSON as a regular body — ugly but safe, no crash. This satisfies
  risk #7.

### 4.2 Security model (frozen by this ADR)

1. **Path allow-list.** On write: resolved path must start with
   `A2A_TRANSFER_DIR` (realpath, not symlink-following). On read: same
   check, plus `agent_id == transfer.recipient_id` (or sender, for
   deletion).
2. **File mode.** Staged files created `0600`; transfer dirs `0700`.
3. **TTL.** Default 24 h. Configurable per-transfer up to a hard cap
   of 7 days (env var `A2A_TRANSFER_MAX_TTL_SECONDS=604800`).
4. **Size cap.** Env var `A2A_TRANSFER_MAX_SIZE_BYTES` (default
   100 MB). Upload beyond → 413.
5. **Quota per agent.** Env var `A2A_TRANSFER_MAX_PENDING_PER_AGENT`
   (default 50). Server refuses new transfers from an agent over quota
   until expiry or deletion.
6. **Integrity.** Writer writes to `.tmp` then `os.rename` atomically.
   Readers never see half-flushed files.
7. **ACL on cross-machine path (C).** The `transfers` row is the source
   of truth; the façade refuses `GET /transfers/<id>` unless the
   `Authorization: Bearer` token is valid AND the requesting agent_id
   matches `transfers.recipient_id`.

### 4.3 Cleanup strategy

- **On sender side:** atomic write + sync means the file is valid the
  moment the manifest is written. No coordination needed with recipient.
- **On recipient side:** `agent_fetch_file` opens the file read-only.
  Recipient explicitly calls `agent_delete_file(transfer_id)` when
  done. Missing call → TTL sweep cleans up.
- **Background sweeper:** a module-level coroutine in the bridge
  iterates `transfer_dir` every `_TRANSFER_SWEEP_INTERVAL_S = 300 s`
  and removes expired transfers. Pattern mirrors `RateLimiter.prune_stale`.
- **Sleeping nodes caveat (ADR-006 Mac/autossh pattern):** if the
  recipient node is suspended (Mac closed lid, laptop asleep), its
  gateway cannot subscribe or fetch. A transfer whose TTL expires while
  the recipient sleeps is reaped server-side and is irrecoverable. The
  24 h default is sized for overnight sleep (typical <12 h) but *not*
  for weekend-long suspensions. Per-transfer `expires_in` lets the
  sender bump to the 7 d hard cap when targeting a known-sleepy
  recipient. See open question in §5.3 on whether to auto-extend on
  recipient reappearance.

## 5. Consequences

### 5.1 Positive

- Issue #33 resolved: agents can hand off arbitrary-size payloads without
  loading them into context.
- Same-machine transfers become free (path + sha256 only on the wire).
- Cross-machine transfers become uniform: no per-agent special-casing,
  no agent-specific storage backends.
- Wire protocol is frozen in this ADR, so PR1 and PR2 can be reviewed
  against a spec rather than against each other.

### 5.2 Negative

- Two new SQLite-adjacent concerns: the `transfers` table (PR2) and the
  `<transfer_dir>/<uuid>/meta.json` files (PR1 and PR2). Recovery after
  a crash during a sweep is not a concern (re-run is idempotent), but
  recovery after a partial write is: atomic-rename discipline is now
  required in a second code path.
- `A2A_TRANSFER_DIR` adds one more filesystem prerequisite to the Hermes
  gateway setup — documented in README alongside `A2A_SIGNAL_DIR`.
- The façade process (v0.7.1) gains write access to the transfer
  directory. If the façade is ever exposed to the internet without a
  reverse proxy, this widens the blast radius of a remote-code-execution
  bug on the façade itself.
- MCP tool count goes from 6 to 9 (`agent_send_file`, `agent_fetch_file`,
  `agent_delete_file`). Worth the ergonomic cost but pushes us closer to
  the limits of what an LLM can reason about in one call.

### 5.3 Open questions

- **Should `agent_fetch_file` copy into the recipient's workspace or return a
  path under the shared dir?** Leaning: **return the shared path** and let
  the recipient's `read_file` tool operate in place. Copying would double
  disk usage for large files.
- **What happens when the recipient is offline at send time?** Leaning:
  **nothing special** — the transfer sits until TTL. The inbox message
  wakes the recipient like any other; on retrieval it fetches the file.
- **Do we expose a streaming `agent_fetch_file_stream`?** Leaning: **no for
  v0.7**. Recipient reads the local path with whatever tool it wants.
  Streaming matters only if the recipient is on a node without direct
  disk access to the file — punt to a future ADR (ADR-007bis).
- **Is `sha256` validated on `agent_fetch_file`?** Leaning: **yes, by
  default, toggle via `verify=False` kwarg**. The ~50 ms cost on a 100 MB
  file is negligible vs. silent corruption detection.
- **Should the sweeper auto-extend `expires_at` when a recipient reappears
  on the bus (e.g. Mac wakes up) but has un-fetched transfers?** Leaning:
  **no for v0.7, yes for a future v0.7.x**. The cleanest mechanism is a
  "touch-on-first-peek" — when `agent_inbox_peek` surfaces a
  `kind=file_transfer` body whose `expires_at` is within 1 h of now, the
  bridge bumps it by one TTL period. Avoids irrecoverable drops after
  weekend suspensions without requiring senders to guess downtime.
  Validates against the ADR-006 autossh/sleep pattern once we have
  telemetry from the v0.7.0 rollout.

## 6. References

- [Issue #33 — feat: agent_send_file — file transfer primitive without LLM context loading](https://github.com/vlefebvre21/a2a-mcp-bridge/issues/33)
- [ADR-001 — Multi-session concurrency](ADR-001-multi-session-concurrency.md) (mark-as-read semantics preserved)
- [ADR-002 — Wake-intent coupling](ADR-002-wake-intent-coupling.md) (`intent` passes through unchanged)
- [ADR-006 — Distributed Agent Fleet](ADR-006-distributed-agent-fleet.md) (cross-machine motivation)
- [ADR-006.1 — HTTP Bus Facade](ADR-006.1-http-bus-facade.md) (façade being extended in PR2)
- [Rate limiter pattern](../../src/a2a_mcp_bridge/rate_limit.py) — `prune_stale` model reused for `_transfer_sweep`
- [Signal dir pattern](../../src/a2a_mcp_bridge/signals.py) — `A2A_SIGNAL_DIR` resolution model reused for `A2A_TRANSFER_DIR`

## 8. Post-implementation notes

### 8.1 Bug #42 — `_iso_utc` rejects ISO strings from the HTTP façade (v0.7.3 / v0.7.4)

**Problem.** When the HTTP façade returned transfer metadata that included ISO-8601 timestamp strings (e.g. `"2026-05-02T19:42:41Z"`), the client-side `_iso_utc()` helper raised a `ValueError` because it only accepted `datetime` objects, not raw strings from the JSON wire format.

**Fix (v0.7.3).** `_iso_utc` was updated to accept either a `datetime` or a string, parsing ISO-8601 strings via `datetime.fromisoformat()` with a timezone-aware fallback for the trailing `Z` suffix.

**Follow-up fix (v0.7.4, PR #43).** When `A2A_BUS_URL` is set, `_rewrite_transfer_url()` rewrites the locator URL from `http://127.0.0.1:<port>/transfers/<id>` to `https://<bus_url_host>/transfers/<id>`. This is critical for NAT'd recipients (e.g. a Mac behind a residential router) that cannot reach the VPS's loopback address. The rewrite happens in `agent_fetch_file` before dispatch, so the recipient's `GET /transfers/<id>` targets the correct public host.

### 8.2 Bug #44 — 403 Forbidden on every cross-host fetch, missing `X-Agent-Id` in `_facade_download` (v0.7.5)

**Problem.** The HTTP façade enforces recipient ACL on `GET /transfers/<id>`: the request must carry both `Authorization: Bearer <token>` **and** `X-Agent-Id: <agent_id>` (must match `transfers.recipient_id`). However, `_facade_download` in `tools.py` was added before this ACL enforcement and did not emit the `X-Agent-Id` header, causing every cross-host fetch to return 403 Forbidden.

**Fix (v0.7.5, PR #45).** `_facade_download` now accepts an optional `agent_id` parameter. When non-empty, it injects `X-Agent-Id: <agent_id>` into the `urllib.request.Request` headers alongside the existing `Authorization` bearer token.

### 8.3 Lesson: two client paths for transfer download

The cross-machine download path splits between two implementations:

| Client | Module | HTTP library | Header handling |
|---|---|---|---|
| `HttpBusStore.download_transfer` | `bus_store.py` ~l.420 | `httpx` | Headers set at client init (l.202) |
| `_facade_download` | `tools.py` ~l.442 | stdlib `urllib.request` | Headers set per-request; `X-Agent-Id` added in v0.7.5 |

Both must emit `X-Agent-Id` for ACL enforcement. The stdlib path deliberately avoids a hard `httpx` dependency but requires explicit header wiring. This duplication is a consequence of the v0.7.2 "facade priority" dispatch (when `A2A_BUS_URL` is set, `agent_fetch_file` always routes via `_facade_download` even for Store backends). Any future ACL-related header changes must be applied to **both** paths.
