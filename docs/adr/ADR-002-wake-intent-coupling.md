# ADR-002 — Wake-up intent is hardcoded to inbox triage

- **Status:** Proposed
- **Date:** 2026-04-23
- **Context window:** post v0.4.4
- **Authors:** VLBeauClaudeOpus (architect), Vincent Lefebvre

## 1. Context

Since v0.4.4, when agent A calls `agent_send(B, ...)`, the bridge fires an
HTTP wake-up to B's gateway. B's gateway spawns a fresh session that is
invoked with a fixed system prompt — in the current Hermes integration,
that prompt always invokes the **`a2a-inbox-triage`** skill.

The `a2a-inbox-triage` skill has a well-defined, bounded contract:

1. `agent_ping` to confirm identity.
2. `agent_inbox(unread_only=True)` to drain the inbox.
3. Triage the messages (ack, reply, archive).
4. **Wrap up cleanly and end the session.**

This contract is correct for the *default* case: a peer agent sent a
conversational update that deserves a reply. It is wrong for the case
where the inbound message is a **task handoff** — "go implement X, ~400
LOC, ping me with the commit hash when done".

## 2. Problem statement

A task handoff over A2A currently dies at the triage boundary.

Observed on 2026-04-22 ~21:59:
- Opus (`vlbeau-opus`) sent Qwen (`vlbeau-qwen36`) a greenlight to start
  implementing the v0.5 bridge milestone (~400 LOC budget, 5 review
  criteria, "ping ready-for-review with commit hash").
- Wake-up webhook fired, Qwen's gateway spawned a session, the
  `a2a-inbox-triage` skill ran.
- Qwen read the message, **acknowledged** the task in a reply to Opus
  (msg `82856f90` at 21:59:31), and **exited cleanly** per the triage
  contract.
- No implementation session was spawned. No commit happened. No
  ready-for-review ping. 9h of silence.

The root cause is not a bug in either the skill or the wake-up — each
works as designed. The root cause is a **missing primitive**: the
wake-up carries a single implicit intent (`triage`), so a task
handoff and a casual "heads up, I pushed a fix" arrive through the same
channel and are handled by the same code path.

### 2.1 Concrete risks

| # | Risk | Severity | Example |
|---|------|----------|---------|
| 1 | **Task handoff dies as ack** | High | Implementer agent acks the task, never executes it. Discovered hours later by the requester. |
| 2 | **Requester has no feedback loop** | High | The ack looks like progress. No "accepted but not started" signal, no timeout. |
| 3 | **Silent scope reinterpretation** | Medium | Triage skill tends to condense messages into a reply — nuance and deliverables in the original brief get summarized away in the ack and lost. |
| 4 | **Manual rescue required** | Medium | User (Vincent) had to notice the silence, check bus state, and re-prompt Qwen via Telegram the next morning. |
| 5 | **No way to express "do the thing" on the bus** | Medium | Any caller that wants an agent to *act* on a message, not just *respond* to it, has to reach out of band (Telegram, cron, direct MCP call). This defeats the point of A2A as a delegation mesh. |
| 6 | **Cross-agent protocol ambiguity** | Low-Medium | Skill prompts across profiles diverge on how to handle "action-required" messages. Each agent has its own heuristic, none are guaranteed. |

### 2.2 Why this is a *bridge* problem (not just a Hermes skill problem)

Three reasons:

1. **The wake-up payload is the bridge's public contract.** It is the
   bridge that POSTs the webhook with its current prompt template. The
   skill on the receiving side only exists because the bridge's wake-up
   shape made it the natural integration point.
2. **The intent is caller knowledge.** Only the *sender* knows whether
   a given message is a FYI, a question, a review request, or a task
   handoff. That metadata must travel with the message, which means
   crossing the bridge.
3. **Skills are per-profile.** If every profile invents its own
   action-detection heuristic in its local `a2a-inbox-triage` skill,
   behaviour diverges silently and the bridge cannot reason about
   delivery semantics at all.

## 3. Options considered

### 3.1 Option A — `intent` field on `agent_send`

Extend `agent_send(target, message, metadata, intent)` with an enum:

- `triage` (default) — "read, reply if relevant, done". Current behaviour.
- `execute` — "this is a task handoff. After ack, continue autonomously
  until the task is done or you hit a stop condition."
- `review` — "I need you to review X. Produce a structured LGTM /
  REQUEST_CHANGES output."
- `question` — "I need an answer, not a task. Reply and exit."
- `fyi` — "Heads up, no action required, no reply expected."

The bridge propagates `intent` into the wake-up payload. Hermes reads it
and dispatches to a matching skill (`a2a-inbox-triage`, `a2a-task-exec`,
`a2a-review`, …).

- ✅ Explicit, caller-driven, machine-readable.
- ✅ Small bridge-side change (one optional field, one payload update).
- ✅ Compatible with current behaviour (`intent=None` → `triage`).
- ✅ Enables protocol-level semantics the bridge can enforce (e.g.
  `execute` implies longer session budget, different timeout).
- ❌ Requires a matching set of skills on the Hermes side. Without them
  the field is decorative.
- ❌ Enum creep: callers will want more intents (`escalate`, `broadcast`,
  …). Versioning discipline needed.
- ❌ Does nothing if the sender lies — the receiver must still validate
  (e.g. an `execute` from a peer you don't trust may need downgrading).

### 3.2 Option B — Receiver-side heuristic on the inbox-triage skill

Keep the current bridge contract. Upgrade `a2a-inbox-triage` to detect
task-handoff patterns in message content (keywords: "implement", "LOC
budget", "ping me when done", presence of a review checklist, etc.) and
branch into an execution path instead of closing the session.

- ✅ No bridge change at all.
- ✅ Ships as a skill update, rolls out per profile at own pace.
- ❌ Heuristic. Will miss handoffs worded indirectly and will false-positive
  on casual messages ("when you have a minute, could you implement…").
- ❌ Divergence between profiles. Qwen and GLM will develop different
  triggers, leading to unpredictable mesh behaviour.
- ❌ Cannot express metadata the message body doesn't contain (e.g. "this
  is a low-priority task, batch it with others").
- ❌ Leaves the bridge's public contract as ambiguous as it is today.

### 3.3 Option C — Two-channel bus (`notify` vs `task`)

Split the bus into two logical queues. `agent_send(..., queue="task")`
goes into a separate table/topic with different wake-up semantics
(longer timeout, different skill, possibly different retry policy).

- ✅ Cleaner separation than Option A.
- ✅ Allows per-queue policies (e.g. task queue supports idempotency
  keys, dedup, and retry-on-crash).
- ❌ Biggest schema change. Migration burden.
- ❌ Most callers will just always pick one queue and the other will rot.
- ❌ Harder to extend than Option A (each new intent = new queue or a
  secondary split).

### 3.4 Option D — Send-side "self-dispatch" wrapper

Provide a helper tool `agent_task_handoff(target, brief, stop_condition,
review_criteria)` that is *implemented in terms of* `agent_send` but
sets a structured JSON body the receiver parses deterministically.

- ✅ Purely additive; no core bridge change.
- ✅ Structured contract on both sides, no free-text parsing.
- ❌ Two near-identical primitives; callers will still use `agent_send`
  with free text "because it's easier", and the problem persists.
- ❌ Doesn't solve the general case, only the task-handoff case.
- ❌ Feels like a feature patch rather than a protocol fix.

## 4. Decision

**Adopt Option A (explicit `intent` field on `agent_send`) as the
v0.6 target, with Option D kept as a syntactic sugar layer on top
should caller ergonomics prove poor.**

Option A is preferred because:

- It surfaces intent as a **first-class, machine-readable protocol
  element** instead of burying it in skill heuristics.
- It is backward-compatible: absence of `intent` means `triage`,
  matching today's behaviour exactly.
- It unlocks downstream work (per-intent timeouts, per-intent retry,
  per-intent session budget) that would be awkward to retrofit onto a
  heuristic system.
- It is the only option that puts the bridge in a position to
  **observe and reason** about delivery semantics. Option B hides the
  semantics in per-profile skills the bridge cannot see.

Concretely, the v0.6 milestone spans both this bridge and the Hermes
gateway / skills:

**Bridge-side (this repo)**

1. **`intent` field on `agent_send`** — optional string, enum-validated
   against a fixed list (`triage`, `execute`, `review`, `question`,
   `fyi`). Unknown values rejected with a clear error. Default: `triage`.
2. **Intent propagation** — `intent` stored on the message row, echoed
   in `agent_inbox` / `agent_inbox_peek` output, and included in the
   wake-up webhook payload.
3. **Per-intent wake policy** — the bridge may apply different
   rate-limit / retry defaults per intent (e.g. `execute` retries on
   transient wake-up failure; `fyi` does not).
4. **Docstring & README update** — document the enum, the contract of
   each value, and the recommended skill mapping.

**Hermes-side (tracked separately, can ship incrementally)**

5. A new skill `a2a-task-execution` invoked when the incoming wake-up
   carries `intent=execute`. Contract: ack, execute, commit, ping
   ready-for-review — does *not* wrap up after the ack.
6. Optional `a2a-review-request` skill for `intent=review`.
7. The existing `a2a-inbox-triage` skill is narrowed to `intent=triage`
   (and `fyi` / `question`, which have similar wrap-up-after-reply
   semantics).
8. Routing: the webhook platform picks the skill based on the payload's
   `intent` field instead of hardcoding `a2a-inbox-triage`.

Option B is explicitly rejected as the primary solution. The observed
failure mode on 2026-04-22 (Qwen acking a task handoff and exiting) is
exactly what Option B would continue to produce on edge cases;
keyword-based escalation is a best-effort bandaid, not a fix.

Option C is deferred. It may become relevant if we introduce retry
semantics or delivery guarantees that differ fundamentally by intent —
at that point a queue-per-intent becomes a cleaner factoring than a
field-per-intent. Until then, a single queue with an `intent` column is
simpler.

## 5. Consequences

### 5.1 Positive

- Task handoffs over A2A become a first-class, reliable operation
  instead of a "hope the receiver parses your wording correctly" leap.
- The bridge gains the vocabulary to document its delivery guarantees
  per intent (timeout, retry, wake policy).
- Senders can express intent explicitly; receivers can validate and
  downgrade if they don't trust a given sender's claim.
- Skills gain a clean dispatch key instead of each profile inventing
  its own heuristic.

### 5.2 Negative

- Another shared contract the bus and Hermes must agree on. Skew
  (bridge v0.6 sender → Hermes gateway without the new skills)
  degrades gracefully to triage behaviour, which is the current
  baseline — but it means the new intent value is effectively a no-op
  until the Hermes side catches up.
- The enum must be versioned. Adding new values is easy; removing is
  painful.
- The meaning of `execute` requires care. An agent that blindly honours
  `execute` from any peer is a trust-delegation risk (arbitrary agent
  telling it to do arbitrary work). Receivers will need a small
  allowlist or downgrade policy.

### 5.3 Open questions

- Where does the enum live — bridge repo, a shared schema package, or
  documented in the README only? Leaning: **bridge repo** (it owns the
  tool definitions).
- Should `intent=execute` trigger a different wake-up retry policy
  bridge-side? Leaning: **yes**, longer backoff and more attempts, on
  the grounds that a missed task handoff is more costly than a missed
  FYI.
- Should the bridge expose a per-agent "accepted intents" field in
  `agent_list` so callers can check before sending? Leaning: **v0.7**
  — useful but not blocking. Ship the enum first, learn the mesh
  behaviour, then codify capabilities.
- How does `intent=execute` interact with ADR-001's gateway-mediated
  inbox cache? Leaning: the cache stores the intent alongside the
  message; the gateway consults the intent to decide which skill to
  invoke when spawning the session. Both ADRs compose cleanly.
- What does a receiver do with an unknown intent? Leaning: **downgrade
  to triage and log a warning** rather than reject. Forward compat.

## 6. References

- Discovery incident: 2026-04-22 21:59 Opus→Qwen handoff for v0.5
  implementation; Qwen acked and exited, silence for 9h; diagnosed
  2026-04-23 morning via SQLite bus inspection and Qwen's gateway logs.
- Bridge v0.4.4 webhook wake-up mechanism — the delivery primitive
  this ADR layers intent on top of.
- ADR-001 — the gateway-mediated inbox cache. `intent` is part of the
  cache entry under Option A′.
- `a2a-inbox-triage` skill (per-profile) — the current implicit
  single-intent receiver, which becomes one of several intent-routed
  skills under this proposal.
