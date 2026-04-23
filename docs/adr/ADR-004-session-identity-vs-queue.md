# ADR-004 — Session identity: ephemeral-sessions-with-cache vs persistent-session-with-queue

- **Status:** Accepted — 2026-04-23 (Option W de facto)
- **Date:** 2026-04-23
- **Context window:** during v0.5.0 post-mortem
- **Authors:** VLBeauClaudeOpus (vlbeau-opus), Vincent Lefebvre

## 1. Context

ADR-001 chose **Option A′ (leader-at-gateway)** to fix message theft between concurrent sessions of the same profile. v0.5.0 shipped the bridge-side primitives (`agent_inbox_peek`, `session_id` metadata, session-tagged logs). A live test on 2026-04-23 confirmed three things:

1. The bridge-side primitives work.
2. One process per profile is maintained (gateway singleton holds).
3. **N distinct conversational sessions continue to be spawned within that single process**, one per inbound trigger (Telegram message, webhook wake-up, A2A inbound). They do not share in-memory state, do not share conversation history, and do not know about each other.

Point 3 is the *user-facing* symptom of ADR-001's "illusion of identity" (risk #5). Even with a shared inbox cache (ADR-001 §4 items 5-8, pending), the user still talks to **one of N threads**, not to "Qwen". The cache prevents message theft; it does not prevent conversation fragmentation.

This ADR forces us to decide **whether A′ is the right target in the first place**, or whether we should aim one level higher: **1 persistent session per profile**, consuming a local queue — the queue being fed by the gateway cache.

## 2. Problem statement

Two questions have been conflated under "fix ADR-001":

- **Q1 — Delivery semantics:** "a message sent to `vlbeau-qwen36` is not silently stolen by a sibling session and archived without the main conversation seeing it." → A′ with cache solves this.
- **Q2 — Identity semantics:** "when the user says 'we discussed this yesterday', *one* session embodies that memory." → A′ with cache **does not** solve this. Multiple sessions still live in parallel, each with its own conversation history.

The v0.6 issue (#14) currently targets Q1. Before implementing, we need to decide whether Q2 is in scope too, because the two fixes have **different architectures**:

- **Q1-only** → keep spawning sessions ephemerally, add a shared cache so they see each other's inbox.
- **Q1+Q2** → stop spawning multiple sessions; have **one long-lived session** per profile that consumes all inbound triggers through a single queue.

These are not compatible incremental steps. Choosing Q1-only now and then trying to move to Q1+Q2 later is a near-rewrite of the gateway, not an extension.

## 3. Options

### 3.1 Option X — Ephemeral sessions + shared cache (current A′ as documented)

Status quo of the current ADR-001 / issue #14 plan.

- Gateway drains the A2A bus atomically, writes into `~/.hermes/profiles/<id>/inbox-cache.db`.
- Each inbound trigger (Telegram, webhook, cron) still spawns a **new Hermes session** as today.
- Sessions read the cache at spawn + incrementally per turn, and write `handled_by` markers after acting.

**Fixes:** Q1 (no message theft), risks #1, #2, #3, #6, #7 of ADR-001 §2.1.
**Does NOT fix:** Q2 (illusion of identity), risks #5, #9. Two sessions can still have two different conversations with the user under the same `agent_id` — they will now both *see* the messages the other handled, but they don't share conversation history, they don't reason about each other's output, and the user still perceives "Qwen said one thing on Telegram and something contradictory on A2A".

**Cost:** ~2-3 days bridge-side (items 5-6), ~3-5 days Hermes-side (items 7-8, requires gateway modifications).

**Acceptance test:** the existing one in issue #14 (2 sessions → 1 session after fix). *This test fails under Option X* — the goal under X is "1 process + N sessions seeing the same cache", not "1 session".

**Correction needed to issue #14 if X is chosen:** the test must be rewritten to assert "no duplicate reply, no orphaned handled marker", not "exactly 1 session".

### 3.2 Option Y — Persistent single session + local queue

Inverts the ownership:

- Each profile runs **one long-lived Hermes agent process**, not one gateway + spawned sessions.
- The gateway becomes a **trigger demux**: Telegram message / webhook / A2A inbound → all write to a local queue `~/.hermes/profiles/<id>/session-queue.db`.
- The single session reads the queue in a loop (like a worker). Each item becomes the next "turn" of the conversation — same session_id, same conversation history, same in-memory state.
- Replies are routed back to the *origin* of the trigger (Telegram chat_id, HTTP response, A2A recipient) by attaching the origin context to each queue item.

**Fixes:** Q1 (single consumer by construction) AND Q2 (single conversational thread — `agent_id` truly identifies one conversation).

**Trade-offs:**
- ✅ Semantically honest: "the user talks to Qwen" is now true.
- ✅ Conversation history is coherent across channels (Telegram + A2A + webhook).
- ✅ Simpler mental model downstream: other agents reasoning about `vlbeau-qwen36` reason about one actor.
- ❌ **Bigger Hermes-side rewrite**: the current gateway assumes one session per entry point. Refactoring to "one persistent session + trigger demux" is not trivial.
- ❌ **Session lifetime becomes much longer**: memory leaks, context-window overflow (compression needed), LLM provider rate limits need per-profile pacing.
- ❌ **Single point of blocking**: if the persistent session is in the middle of a long LLM call when Telegram message M arrives, M waits. Today, a parallel session would have handled M instantly.
- ❌ **Cross-channel leak risk**: user A writes on Telegram, user B triggers a webhook. Both become queue items in the same conversation. This might surprise users. Requires explicit context separation in the queue item payload.
- ❌ **Harder to multi-user**: one profile = one persistent conversation = one audience. Multi-tenant deployments would need one persistent session per user, which multiplies the problem.

**Cost:** ~1-2 weeks (Hermes-side refactor + queue format + origin routing + compression strategy + tests).

**Acceptance test (as written in #14):** the test "1 process + 1 session" **passes naturally** under Option Y — it's the definition.

### 3.3 Option Z — Hybrid: persistent session optional, opt-in per profile

Some profiles benefit from Y (the orchestrator `vlbeau-main` — one continuous conversation with Vincent across channels). Others may not (a batch worker like `vlbeau-qwen36` doing 50 parallel code-review tasks).

- Add `session_mode: persistent | ephemeral` in profile `config.yaml`.
- `persistent` profiles run Option Y (one process, queue, persistent session).
- `ephemeral` profiles run Option X (gateway + cache + spawned sessions).
- Both share the same bridge-side primitives (agent_inbox_peek, session_id metadata).

**Fixes:** Q1 always; Q2 for profiles that opt in.

**Trade-offs:**
- ✅ Migrates incrementally: start X everywhere, flip profiles to Y as needed.
- ✅ Respects profile heterogeneity (orchestrator ≠ batch worker).
- ❌ Doubles some surface, but less than naive sum suggests: two code paths and two test matrices, yes — but the **session lifecycle infrastructure is shared** between Y and Z (see §3.5 below). Empirically closer to **~1.3×** than 2×.
- ❌ Downstream agents (other profiles on the bus) need to know or discover which mode a recipient is in, to reason about whether they're talking to one thread or many. Or we hide this behind a consistent facade (e.g. `agent_list` exposes `session_mode`).

**Cost:** X cost + Y cost − shared-infra savings + integration layer (~3 weeks total if both paths fully implemented; ~2 weeks if we defer the `session_mode` facade and let peers introspect via `agent_list`).

### 3.4 Option W — Do nothing more (stop at v0.5 primitives)

Keep v0.5 as-is. Document the limitations. The primitives (`agent_inbox_peek`, `session_id` metadata) give downstream agents enough tools to reason about session fragmentation explicitly. Users and agents learn to work with "agent_id = profile, not conversation".

**Fixes:** nothing beyond v0.5.
**Trade-offs:**
- ✅ Zero additional cost.
- ❌ The ADR-001 risks #1-#7 remain.
- ❌ User experience stays confusing (the 2026-04-23 incident is evidence).

Listed for completeness; not recommended.

### 3.5 Session lifecycle & garbage collection (applies to Y and Z)

Option Y (and therefore Z) introduces a schema dependency that Option X does not have: a persistent **`sessions`** table on the bridge side, tracking live persistent-session consumers. Current v0.5.0 schema has no such table — `messages.sender_session_id` is a tag column, not a consumer cursor. Before adopting Y/Z we must decide the GC strategy.

**Proposed schema** (minimum viable):

```sql
CREATE TABLE sessions (
    agent_id         TEXT NOT NULL,
    session_id       TEXT NOT NULL,
    last_heartbeat_at TIMESTAMP NOT NULL,
    last_read_at     TIMESTAMP,
    status           TEXT NOT NULL DEFAULT 'active',  -- active | expired (see §3.5.1)
    origin_metadata  JSON,                            -- Telegram chat_id, webhook URL, etc.
    PRIMARY KEY (agent_id, session_id)
);
```

Strategies considered (ordered by simplicity):

**1. TTL passive on `last_heartbeat_at` — baseline recommendation.**
- Each `agent_subscribe(persistent=True)` call bumps `last_heartbeat_at = now()`.
- Passive sweep (triggered on inbox reads, no background thread required): any session with `heartbeat < now() - 1h` → `status = 'expired'`, unconsumed messages fall back to the global inbox (`read_at` stays NULL, they become deliverable to the next consumer).
- **Pro:** no active monitoring, no bridge-side thread, no new cron.
- **Con:** a flapping `read_at` can cause a message to be redistributed after expiration. Acceptable if agents assume idempotence on their handlers (already required by §9 of `a2a-inbox-triage`: "verify before apologizing").
- **Bounded sweep:** each passive sweep expires at most `N` sessions per inbox read (recommended `N=100`, ordered by `last_heartbeat_at ASC`), keeping the read operation O(1) amortized regardless of fleet-wide session accumulation. If sustained accumulation is observed in production (sweep saturating the 100-cap on every read), a separate low-frequency cron (e.g. hourly) handles the long tail without blocking the hot path.

**2. LRU cap per `agent_id` — refinement if respawn-races observed.**
- Soft cap: 1 persistent session per profile (nominal case).
- Hard cap: 3 (tolerates respawn race during restart). Beyond → reject with `SESSION_QUOTA_EXCEEDED`, new session waits for LRU expiry.
- **Pro:** bounds memory, prevents fleet-wide explosion. 9 profiles × 1 session = 9 queues, sustainable.
- **Con:** adds a per-agent counter check on `agent_subscribe(persistent=True)`. Small surface.

**3. Fleet-wide hard cap N — not recommended.**
- Recreates the "who gets the slot?" problem that v0.5 just solved. Skip.

**Recommendation:** ship **strategy 1 (TTL)** as baseline in the v0.7 Option Y path. Add **strategy 2 (LRU per agent_id)** only if production telemetry shows respawn-race session accumulation. No fleet-wide cap.

**Cross-benefit with Option Z:** once the `sessions` table exists for GC, it is also the natural home for per-session origin metadata (Telegram chat_id, HTTP response handle, started_at, etc.) that Option Y's trigger demux needs to route replies back to the correct channel. The incremental cost of supporting both modes under Z is therefore **~1.3× Option Y alone**, not 2× — the lifecycle infrastructure is mutualized between them. This strengthens the Z recommendation in §5 against the "doubles the surface" critique in §3.3.

**Acknowledgement:** the GC analysis in this section was contributed by Qwen (vlbeau-qwen36) on 2026-04-23, 07:38 UTC — cross-agent review of the initial ADR draft. The `bounded sweep` refinement under strategy 1 was added by Opus (vlbeau-opus) on 2026-04-23, 07:48 UTC.

#### 3.5.1 Session state machine (v0.7)

**States:** `active` | `expired`.

**Transitions:**

| From | Trigger | To | Who fires it |
|---|---|---|---|
| _(none)_ | `agent_subscribe(persistent=True)` or first inbox read on fresh `(agent_id, session_id)` | `active` | client (insert row) |
| `active` | `agent_subscribe` / inbox read on existing row | `active` | client (bumps `last_heartbeat_at`) |
| `active` | `last_heartbeat_at < now() - TTL` caught by passive sweep | `expired` | bridge (sweep, bounded N=100/read, see §3.5) |
| `expired` | _(terminal)_ | — | — |

**Invariant:** monotonic progression. `active → expired` is one-way; `expired` is terminal. Sessions are never resurrected — a client that wants to resume work after expiry must mint a **new** `session_id`, which creates a fresh row. The PK `(agent_id, session_id)` enforces this at the schema level.

**Message routing by state:**

- `active` — `agent_send(target=agent_id, metadata.session_id=X)` pins the message to session X's queue. An active `agent_subscribe` long-poll for X receives it.
- `expired` — the row is a tombstone. The passive sweep that expires the row also recycles any queued `sender_session_id=X` messages back to the global inbox (sets `sender_session_id` to NULL), so no messages are orphaned. New `agent_send` with `metadata.session_id=X` against an expired row falls through to the global inbox as if the session had never existed.

**Optional observability flag:** implementations MAY add `shutdown_pending BOOLEAN DEFAULT FALSE` on the `sessions` row for operational dashboards that want to distinguish "the client has signalled it is about to exit" from "fully live". This flag is **not** part of the state enum and has no routing semantics — it exists purely for `agent_list` / monitoring readouts. Routing remains binary: the row is either `active` (enqueue) or `expired` (tombstone).

**Crash / degradation semantics:**

- **Bridge crash during `active`** — session rows persist in the `sessions` table, messages persist in the `messages` table. On restart, client long-poll reconnects resume cleanly; `last_heartbeat_at` is bumped on the first reconnect. No message loss because the `messages` row is durable, not the in-memory long-poll wait queue.
- **Client crash (no graceful close)** — the session sits in `active` until its TTL elapses, then the passive sweep transitions it to `expired` and recycles the queue to the global inbox. Typical recovery latency is bounded by TTL (default 1h; tunable per deployment). For client classes that need faster recycling, the remedy is either (i) shorter TTL or (ii) the caller mints a new `session_id` on reconnect rather than reusing the old one — the old row will still sweep away eventually, but the caller is not blocked.
- **Bridge restart in the middle of a passive sweep** — safe. The sweep is an ordinary `UPDATE sessions SET status='expired' WHERE ... LIMIT N` plus the recycle `UPDATE messages SET sender_session_id=NULL WHERE ...`. Both are wrapped in a single transaction; a crash either rolls back (sweep retries next read) or commits (sweep is durable). No intermediate state is observable.

**Alternatives considered.**

A three-state machine `active | draining | expired` was evaluated in detail. The proposed `draining` state would be entered by a client-driven graceful shutdown primitive (`agent_close_session()` or `agent_subscribe(persistent=True, close=True)`), flush already-queued messages for a bounded `drain_TTL` window, then transition to `expired`. **Rejected**, three reasons:

1. **No trigger in v0.7.** v0.7's API surface has no client-driven session close primitive. Documenting a state with no entry path is dead weight.
2. **No read-semantics distinct from `active`.** A `draining` session that still delivers queued messages to an outstanding long-poll is, from the peer's observable behaviour, indistinguishable from `active`. The only difference is write-side (reject new pins), which we cover via a routing check that reads `status`, not a separate state.
3. **Monotonicity is architecturally load-bearing.** A strictly monotonic two-state machine eliminates an entire class of concurrency bugs: double-transition, resurrection, TOCTOU on the state field. `active → draining → expired` is also monotonic, but `draining` introduces a non-obvious "writes blocked, reads live" sub-regime whose invariants are harder to reason about, especially under bridge crash mid-drain.

Re-introducing `draining` in v0.8+ remains trivial: the SQL column is already `TEXT`, so `ALTER TYPE status ADD VALUE 'draining'` (Postgres) or just widening the `CHECK` constraint (SQLite) is a one-migration change if a graceful `agent_close_session()` with async drain window is ever spec'd. The cost of delaying the decision is therefore zero, and the benefit of a simpler v0.7 invariant is material. YAGNI.

**Acknowledgement:** the §3.5.1 state machine (two-state form) was designed jointly by Qwen (vlbeau-qwen36) and Opus (vlbeau-opus) via A2A on 2026-04-23, 07:46–08:00 UTC. An earlier three-state draft was superseded by this section after cross-review converged on monotonicity over drain-granularity.

## 4. Decision criteria

For a decision, compare along three axes:

| Criterion | Option X (A′) | Option Y (persistent) | Option Z (hybrid) | Option W (nothing) |
|---|---|---|---|---|
| Fixes message theft (Q1) | ✅ | ✅ | ✅ | ❌ |
| Fixes illusion of identity (Q2) | ❌ | ✅ | ✅ (opt-in) | ❌ |
| Matches user's mental model ("one Qwen") | ❌ | ✅ | ✅ (per-profile) | ❌ |
| Implementation cost | Medium (~1 wk) | High (~2 wk) | Very high (~3-4 wk) | Zero |
| Risk of regression | Low | Medium (lifetime / compression) | Medium-High (two paths) | None |
| Supports multi-tenant later | ✅ (unchanged) | Needs rework | ✅ (ephemeral profiles) | ✅ |
| Delays v0.6 | No | ~1 week extra | ~2-3 weeks extra | N/A |
| Honest to ADR-001 §2.1 risk #5 | No | Yes | Partial | No |

## 5. Decision (2026-04-23)

**Adopt Option W (do nothing beyond v0.5 primitives).**

After detailed scoping of Option X (v0.6 = A′ gateway-side cache), Vincent arbitrated that the ~8 days of work — of which ~7 would land in `hermes-agent` upstream (not in this repo), requiring either an upstream PR of uncertain welcome or a maintained fork — is **disproportionate to the benefit it delivers**.

The benefit of Option X is Q1 only (no message theft). Q1 is a secondary user pain: the test of 2026-04-23 showed the fragmented sessions eventually converge (Qwen did reply to Opus in the end), and silent message archiving is rare in practice. The primary user pain is Q2 (one agent_id ≠ one conversation), and Q2 is **not fixable without changing the framework's core session model** — which Hermes does not want to change.

Paying a high maintenance cost for a partial fix of a secondary problem, while the primary problem remains, is a poor trade.

### Rationale for rejecting each option

| Option | Why rejected |
|---|---|
| **X** (A′ cache) | 8 days of mostly-upstream work for Q1 only. Q2 untouched. Fork risk. |
| **Y** (persistent session + queue) | Would fix both Q1 and Q2, but ~2 weeks of work forcing Hermes into a shape it does not natively support. High regression risk. Hermes is vertical by design. |
| **Z** (hybrid opt-in) | Combines the costs of X and Y. Doubles the test matrix and maintenance surface. |
| **W** (do nothing) | **Selected.** Accepts that `agent_id` ≠ conversation and documents the limit. |

### Scenario 3 — bridge as inter-framework bus (future evolution)

Noted for the record: should the need for persistent-identity agents become pressing, the correct direction is **not** to patch Hermes but to **run a framework that natively supports persistent identity** (OpenClaw, Letta) on the same A2A bus. `a2a-mcp-bridge` is already framework-agnostic (SQLite bus + MCP interface); nothing prevents an OpenClaw agent from registering under `vlbeau-cognitive-X` and coexisting with Hermes profiles under `vlbeau-qwen36` etc.

This is the **long-term** escape hatch. It does not require any v0.6 of the bridge; it requires choosing the right framework for the right profile.

### What happens next

- **Issue #14 closed as "won't implement"** — see comment for full rationale.
- **Issue #13 v0.6 roadmap remains valid** for the 3 operational gaps (crash-recovery, multi-gateway, hot-reload), which have value independent of the Q1/Q2 debate.
- **v0.5 is the stable endpoint** for the concurrency work on this bridge. Future bridge releases focus on operational robustness, not session semantics.
- **CHANGELOG update**: document that ADR-001 §2.1 risks #1-#7 remain present by design — downstream agents must treat `agent_id` as a profile identifier, not a conversation identifier.

## 6. Open questions for Vincent — ANSWERED 2026-04-23

1. **Your daily experience of the bug** — *Answered:* The lived pain is Q2 (fragmentation of identity), not Q1 (message theft). Q1 converges eventually in practice.

2. **Multi-channel routing** under Option Y — *Moot:* Y not selected.

3. **Blast radius of long sessions** — *Moot:* persistent sessions not pursued within Hermes. If needed, addressed by the framework (OpenClaw/Letta) rather than by extending Hermes.

4. **Other VLBeau profiles as consumers** — *Moot under W:* `agent_id` semantics unchanged. Downstream agents continue to treat the bus as "send to a profile, not a conversation".

5. **ADR-001 status** — ADR-001 stays Accepted in its original scope (Q1). The v0.5 primitives it motivated are shipped and useful. The gateway-side items 5-8 are **deferred indefinitely**. This ADR-004 clarifies that ADR-001 alone does not close the user-visible gap, and that closing it requires a framework change — not pursued here.

## 7. Next steps

Retained from the decision above:

- Close issue #14 with pointer to this ADR.
- Update CHANGELOG in the next bridge release to document the acceptance of ADR-001 §2.1 risks #1-#7 as permanent.
- Issue #13 operational roadmap (v0.7) remains independent and valid.
- No v0.6 session-semantics work. v0.5 is the stopping point.

## 8. References

- ADR-001 — the A′ decision this ADR reconsiders.
- Issue #14 — tracks the v0.6 Option X implementation. This ADR does not cancel it, but clarifies what its acceptance test means.
- Issue #13 (correction comment) — 2026-04-23 retex of v0.5 showing N-session behaviour persists within one process.
- 2026-04-23 live test in bus `~/.a2a-bus.sqlite` (messages `8c4ce4f1`, `3f3d51c8`, `a3f97f03`, plus opus-side session duplication) — concrete evidence that A′ alone does not close the user-facing gap.
