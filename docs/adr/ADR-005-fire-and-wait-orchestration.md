# ADR-005 — Fire-and-wait orchestration for A2A multi-agent task handoffs

- **Status:** Proposed
- **Date:** 2026-04-26
- **Authors:** VLBeauGLM51 (drafter, vlbeau-glm51), Vincent Lefebvre

## 1. Context

ADR-001 resolved message theft between concurrent sessions via the **leader-at-gateway**
pattern and `agent_inbox_peek(since_ts)`. ADR-002 introduced the `intent` enum
on `agent_send` (`triage`, `execute`, `review`, `question`, `fyi`), with the v1
γ-variant differentiating on a **binary wake axis**: `intent=fyi` skips the
wake-up webhook entirely; all other intents trigger it.

Despite these primitives, the **default Hermes pattern** remains:

```
Every agent_send → webhook wake → gateway spawns amnesic session
→ loads a2a-inbox-triage → processes one message → exits cleanly
```

For task-oriented orchestration, this is both **costly** (N sessions per N round-trips)
and **broken** (RC#10 in `a2a-workflow`: *"task dies as ack"*). The triage skill
acknowledges the task brief and exits without executing — the work never happens.

### 1.1 Concrete problems

| # | Problem | Severity | Impact |
|---|---------|----------|--------|
| 1 | Task dies as ack | High | RC#10: executor acks and exits per triage contract. Zero work done. |
| 2 | N sessions per N round-trips | Medium | 10 exchanges = 10 spawned sessions, each re-loading skills + inbox from zero. |
| 3 | Subscribe-orphan messages | High | Wake-up session atomically mark-as-reads the message. `agent_subscribe()` unblocks but finds empty inbox. Message consumed by parasite session. |

## 2. Decision

Introduce a **"fire-and-wait" orchestration pattern** at the skill level (zero bridge
changes). Two new Hermes skills:

- **`a2a-task-dispatch`** (orchestrator) — sends a task, then blocks on
  `agent_subscribe()` awaiting result.
- **`a2a-task-worker`** (executor) — receives task, executes it, returns result
  in a single persistent session.

**Critical rule:** all post-initial messages use **`intent='fyi'`**. Only the very
first message (orchestrator→worker) uses `intent='execute'` to trigger the wake.

```
┌────────────────────────┬───────────────────────────────────────────┐
│ Primitive               │ Role in fire-and-wait                    │
├────────────────────────┼───────────────────────────────────────────┤
│ agent_send(...execute)  │ Initial wake — fires webhook, spawns E1  │
│ agent_send(...fyi)      │ Subsequent — persists + signal, no wake  │
│ agent_subscribe()       │ Blocking read in both sides              │
│ SQLite bus              │ Persistent store — survives crashes      │
│ Signal file (A2A_SIGNAL)│ Immediate subscribe unblock              │
└────────────────────────┴───────────────────────────────────────────┘
```

## 3. Sequence diagram

```
Orch (session O1)       Bus SQLite          Exec (session E1)
      |                      |                      |
      |--agent_send(exec,    |                      |
      |   intent=execute)--->|                      |
      |                      |--signal touch------->|
      |                      |--webhook wake------->| (spawne E1)
      |                      |                      |
      |--agent_subscribe()-->|                      |--agent_inbox()->
      |   (blocks ≤ 55s)     |                      |<--[execute msg]--
      |                      |                      |--agent_send(orch,
      |                      |<---------(fyi)-------|   'ack', fyi)
      |                      |--signal touch------->|
      |                      | (webhook SKIPPED on  |
      |                      |  intent=fyi!)         |
      |<---unblocks(ack msg) |                      |
      |                      |                      | (does the work...)
      |                      |                      |--agent_send(orch,
      |                      |<---(fyi result)------|   result, fyi)
      |<---unblocks(result)  |                      |
      |                      |                      |--session E1 ends
      | (continues in O1)    |                      |
```

### 3.1 Flow

1. **Fire** — Orch sends `intent='execute'` → full ADR-002 wake chain → E1 spawns.
2. **Ack** — Exec reads task, sends ack with `intent='fyi'`. Signal touches,
   webhook skipped. Orch's `agent_subscribe()` unblocks. No new session.
3. **Work** — Exec performs task in-session (tools, edits, commits, etc.).
4. **Result** — Exec sends result with `intent='fyi'`. Orch's subscribe unblocks.
5. **Continue** — Orch processes result in O1. Zero session churn after the wake.

## 4. Why `intent='fyi'` solves the multi-session conflict

### 4.1 Wake-triggering path (`intent='execute'`, `triage`, `review`, `question`, or absent — `intents.py` defaults absent to `triage`)

```
1. Bridge persists message in SQLite
2. Bridge touches signal file → subscribe() receives signal
3. Bridge POSTs webhook → gateway spawns NEW session (a2a-inbox-triage)
4. New session calls agent_inbox(unread_only=True)
   → ATOMIC mark-as-read: message consumed by new session
5. subscribe() unblocks (signal was touched)
6. Subscriber calls agent_inbox(unread_only=True) → EMPTY (step 4 already consumed it)
7. FAILURE: parasite session ate the message
```

### 4.2 Fire-and-wait path (`intent='fyi'`)

```
1. Bridge persists message in SQLite ✓
2. Bridge touches signal file ✓ → subscribe() receives signal
3. Bridge SKIPS the webhook POST ✓  (ADR-002: fyi ne wake pas)
4. Only subscriber sees message → ingests into the EXISTING session
5. SUCCESS: no parasite, no race, single session continuity
```

The wake-up webhook is the root cause of the spawn-on-request race. Suppressing
it for all post-initial messages eliminates the race by removing the component
that creates it.

## 5. Cost analysis

### 5.1 Baseline (10 round-trips, current pattern)

| Metric | Value |
|--------|-------|
| Orchestrator sessions | 1 |
| Executor sessions (wakes) | 10 |
| **Total OpenRouter sessions** | **11** |
| Typical input/output per session | ~6k in / ~1k out |

**GLM-5.1** ($0.20/1M in, $0.85/1M out):
```
11 × (6 000 × $0.20 + 1 000 × $0.85) / 1 000 000 ≈ $0.022
```

**Opus 4.7** ($15/1M in, $75/1M out):
```
11 × (6 000 × $15 + 1 000 × $75) / 1 000 000 ≈ $1.82
```

**Local (Ollama):** $0.00 (but still 11× process init overhead)

### 5.2 Fire-and-wait (same 10 round-trips)

| Metric | Value |
|--------|-------|
| Orchestrator sessions | 1 |
| Executor sessions | 1 (persistent) |
| **Total OpenRouter sessions** | **2** |
| Orch tokens | ~6k in + ~2k out (short replies) |
| Exec tokens | ~4k in + ~6k out (main work) |

**GLM-5.1:**
```
Orch: (6 000 × $0.20 + 2 000 × $0.85) / 1 000 000 ≈ $0.003
Exec: (4 000 × $0.20 + 6 000 × $0.85) / 1 000 000 ≈ $0.006
Total: ~$0.010  (÷2 vs baseline)
```

**Opus 4.7:**
```
Orch: (6 000 × $15 + 2 000 × $75) / 1 000 000 ≈ $0.24
Exec: (4 000 × $15 + 6 000 × $75) / 1 000 000 ≈ $0.51
Total: ~$0.87  (÷2 vs baseline)
```

**Local:** $0.00 (but 2× init overhead instead of 11×)

### 5.3 The real savings

Token cost reduction (~2×) matters on expensive models, but the *primary*
savings are **elimination of N–1 redundant sessions** — each re-initialises
skills, memory, and inbox from scratch. Fire-and-wait also provides
conversational continuity (worker remembers prior messages) and predictable
session lifecycle (no orphan sessions from failed wakes).

## 6. Limits

### 6.1 Hermes session timeouts

The `vlbeau-glm51` profile `config.yaml`:

```yaml
gateway_timeout: 1800    # 30 min hard cap
inactivity_timeout: 120  # 2 min without activity → terminates
```

Each `agent_subscribe()` blocks up to 55 s (server-capped) before returning.
Since 55 s < 120 s, a single subscribe call never trips the inactivity
timer on its own. **However**, the effective ceiling on how long the
orchestrator can wait for the next message is `inactivity_timeout: 120s`,
not `gateway_timeout: 1800`: once a subscribe returns (either with a
message or timed out), the clock starts again; if more than 120 s passes
with no new activity (e.g. executor working silently), the session is
terminated. The 30-minute `gateway_timeout` only caps the cumulative
session lifetime across many activity-resetting events.

**Practical implication for the worker:** send an `intent='fyi'`
heartbeat every ≤ 90 s while executing long tasks, so the orchestrator's
subscribe keeps returning and the inactivity timer keeps resetting. Without
heartbeats, any silent exec step > 120 s kills the orchestrator session.

For tasks that genuinely need > 30 minutes wall-clock: bump
`gateway_timeout`, keep the ≤ 90 s heartbeat discipline, or fall back to
wake-per-message.

### 6.2 Orchestrator crash during subscribe

The executor's `intent='fyi'` response persists in SQLite. On the next
natural contact with the orchestrator, the response is present in its inbox
— graceful fire-and-forget fallback. Session context lost, message preserved.

### 6.3 Executor busy with another request

Parallel `intent='execute'` from two orchestrators result in both messages
visible in the executor's inbox at next read, handled sequentially. This is
an ADR-001 one-session-per-profile constraint, not a fire-and-wait limitation.

### 6.4 Network partition

Bridge uses SQLite WAL, single-writer. All agents share one filesystem (local
infra). No multi-host concern at present.

## 7. Alternatives considered

| Alternative | Verdict | Rationale |
|-------------|---------|-----------|
| Current wake-per-message | Rejected | Costly, RC#10, no conversational continuity, subscribe race. |
| Polling `agent_inbox` loop | Rejected | Worse than subscribe on latency and cost (each poll is a tool call). |
| Bridge "persistent conversation mode" | Rejected | Over-engineering. Subscribe already provides the blocking primitive. Keep bridge dumb, skills smart. |
| WebSockets / SSE | Rejected | Out of scope. Signal file + subscribe is near-real-time on local infra. |

## 8. Implementation

Two new Hermes skills, **no bridge changes**.

### 8.1 `a2a-task-dispatch` (orchestrator)

**Path:** `~/.hermes/profiles/*/skills/software-development/a2a-task-dispatch/SKILL.md`

**Contract:**
1. Accept task brief (target, description, optional timeout).
2. Send with `intent='execute'`.
3. Loop on `agent_subscribe()`: process acks, results, or cancellations.
4. On timeout (default 10 min, well under `inactivity_timeout: 120s` via
   regular subscribe activity, and far under `gateway_timeout: 1800`),
   send cancellation and report failure.

### 8.2 `a2a-task-worker` (executor)

**Path:** `~/.hermes/profiles/*/skills/software-development/a2a-task-worker/SKILL.md`

**Contract:**
1. Invoked by `intent='execute'` wake-up.
2. Read task brief from inbox.
3. Send ack with `intent='fyi'`.
4. Execute task.
5. Send result with `intent='fyi'`.
6. Exit session cleanly.

### 8.3 Propagation

Via `skill-fanout` (`devops/skill-fanout`) across all 9 profiles. No migrations,
no breaking changes, no feature flags. Agents not using these skills are unaffected.

## 9. Testing

### 9.1 Happy path

From `vlbeau-glm51`, dispatch to `vlbeau-qwen36`: *"Create
`/tmp/a2a-fire-wait-test.py` with `print('fire-and-wait OK')"*.

Verify:
- (1) Subscribe unblocks on ack receipt (no 55s timeout).
- (2) GLM-51 stays in **single session** (check gateway logs).
- (3) All return messages have `intent='fyi'` (SQLite `messages` table).
- (4) No `a2a-inbox-triage` parasite sessions on GLM side.
- (5) File exists and runs: outputs `"fire-and-wait OK"`.

### 9.2 Timeout path

Dispatch with `timeout_minutes=1` to a deliberately slow task.
Verify: orchestrator times out, sends `intent='fyi'` cancellation, reports
failure cleanly. Executor result (if any) arrives in orchestrator inbox post-crash.

### 9.3 Multi-round-trip path

Dispatch a task requiring 3 back-and-forth exchanges. Verify session count
stays at 2 (1 orchestrator + 1 executor).

## 10. References

- **ADR-001** — Leader-at-gateway, `agent_inbox_peek`, multi-session concurrency.
- **ADR-002** — Intent enum, `intent=fyi` skips wake webhook, Option γ v1.
- **Root cause #10** in `a2a-workflow` skill: *"task dies as ack"*.
- **New skills:** `a2a-task-dispatch` + `a2a-task-worker`.
- **Bridge v0.4.4** — Signal file mechanism (`A2A_SIGNAL_DIR`).
- **Hermes config:** `gateway_timeout: 1800`, `inactivity_timeout: 120` (glm51).
