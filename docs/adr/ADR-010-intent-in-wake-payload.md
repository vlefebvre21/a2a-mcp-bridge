# ADR-010 — Intent surfacing in wake-up payload (ADR-002 v2)

- **Status:** Accepted — 2026-06-19 (Option A adopted; implementation deferred to v0.12)
- **Date:** 2026-06-19
- **Last updated:** 2026-06-19
- **Context window:** post v0.11.0 bridge review
- **Authors:** VLBeauGLM51 (vlbeau-glm51), vlefebvre21
- **Related:** [ADR-002 — Wake-up intent coupling](ADR-002-wake-intent-coupling.md)
- **Review finding:** B6 (Intent non transmis dans le payload webhook)

## 1. Context

ADR-002 (accepted 2026-04-24) introduced the `intent` field on `agent_send`
and a binary wake policy (`fyi` skips wake, all others wake). The v1
implementation (Option γ) shipped in v0.6.0 with one deliberate deferral:

> ADR-002 §4.1: *The on-the-wire JSON on the wake webhook payload is unchanged
> for v0.6 — the bridge currently does not yet surface `intent` to the gateway
> because no skill-dispatch logic consumes it there. That is the v0.7 hook
> where Hermes-side routing lands.*

Five versions later (v0.6 → v0.11.0), this hook has not been delivered. The
webhook payload remains:

```json
{"sender": "<agent_id>", "target": "<agent_id>", "source": "a2a-mcp-bridge"}
```

No `intent`. No `message_id`. No idempotency key.

### 1.1 Current workaround

The Hermes-side `a2a-workflow` skill implements a "Step 0" workaround: when a
wake-up fires, the spawned session calls `agent_inbox_peek()` (read-only, no
mark-as-read) to inspect the pending message's `intent` field, then decides
whether to run triage logic or task-execution logic. The wake-up itself is
intent-blind — the routing happens entirely in the skill, after the session
has already been spawned.

This works, but it means:

1. **Every intent triggers the same wake-up cost.** An `execute` task handoff
   and a `triage` heads-up both spawn a full Hermes session, load skills, call
   inbox, and only then diverge. The cost savings from intent-aware routing
   (skipping the session for `fyi`, using a lighter skill for `question`)
   are not realized.

2. **No idempotency on wake-up.** The webhook payload has no `message_id`. If
   the wake-up is retried (network glitch, gateway restart, transient 5xx),
   the recipient is woken twice for the same message. The second wake spawns
   a session that finds an empty inbox (the first session already consumed it)
   and exits — wasting a full session budget.

3. **The gateway cannot route by intent.** The webhook adapter treats every
   wake-up identically: spawn a session, invoke `a2a-inbox-triage`. There is
   no mechanism to dispatch to `a2a-task-exec` for `intent=execute` or skip
   the session entirely for `intent=fyi` when the message has already been
   peeked.

### 1.2 ADR-002 deferred items

ADR-002 §4.1 and §5.3 list three items deferred to "v0.7":

| Item | ADR-002 ref | Status |
|------|-------------|--------|
| Surface `intent` in webhook payload | §4.1 ("v0.7 hook") | ❌ not delivered |
| Per-intent retry/timeout policy | §5.3 Q2 ("Deferred to v0.7") | ❌ not delivered |
| `agent_list` "accepted intents" field | §5.3 Q3 ("Deferred to v0.7") | ❌ not delivered |

This ADR addresses the first item (payload surfacing) and the idempotency gap.
The other two remain deferred — they depend on Hermes-side skill dispatch,
which is out of scope for this repo.

## 2. Problem statement

Decide whether to:

1. **Surface `intent` and `message_id` in the webhook payload now**, enabling
   gateway-side intent routing and wake-up idempotency.
2. **Keep the workaround** (Step 0 skill-side routing) and defer the payload
   change indefinitely.

### 2.1 Risks of the status quo

| # | Risk | Severity | Scenario |
|---|------|----------|----------|
| 1 | **Double-wake on retry** | Medium | Webhook delivery has a transient failure → bridge retries → gateway spawns 2 sessions for 1 message. Second session wastes a full LLM call budget (~$0.14/wake for Opus). |
| 2 | **No gateway-side routing** | Medium | Every wake-up invokes `a2a-inbox-triage` regardless of intent. An `execute` handoff gets triaged (acked and exited) instead of executed. This is the original ADR-002 motivating incident (2026-04-22, Opus→Qwen, 9h silence). The Step 0 workaround catches it, but only if the skill is correctly propagated to every profile. |
| 3 | **Cost: intent-blind wakes** | Low-Medium | `fyi` messages already skip the wake entirely (ADR-002 v1). But `question` and `review` messages wake the full triage path when a lighter skill would suffice. In a chatty mesh, this compounds. |
| 4 | **Skill propagation race** | Medium | If a profile's `a2a-inbox-triage` skill is stale or not indexed (observed 2026-04-22, see a2a-workflow skill §"Defensive inbox check"), the Step 0 routing fails silently. The wake-up spawns a session with no skill, the session doesn't check inbox, the message sits unread. Payload-level intent would allow the gateway to inject the intent into the session prompt directly, bypassing the skill dependency. |

## 3. Options considered

### 3.1 Option A — Surface `intent` + `message_id` in the payload (additive)

Extend the webhook JSON body with two fields:

```json
{
  "sender": "<agent_id>",
  "target": "<agent_id>",
  "source": "a2a-mcp-bridge",
  "intent": "execute",
  "message_id": "a1b2c3d4-..."
}
```

Both fields are **additive** — existing gateway webhook adapters that ignore
unknown keys continue to work unchanged. The Hermes webhook adapter can opt-in
to reading `intent` and `message_id` when available.

**Idempotency:** the gateway tracks `message_id` values it has already
processed (in-memory LRU or a small SQLite table). If a wake-up arrives with
a `message_id` that was already consumed, the gateway skips the session spawn
and returns 200 to the bridge.

**Intent routing:** the gateway webhook adapter can use `intent` to:
- Skip session spawn for `fyi` if the message was already peeked (the inbox
  cache has it).
- Inject `intent` into the session prompt so the skill knows the intent
  without calling `inbox_peek` first.
- Eventually dispatch to specialized skills (`a2a-task-exec` for `execute`).

**✅ Pros:**

- **Backward-compatible.** Old gateways ignore the new fields. New gateways
  gain routing and idempotency.
- **Small bridge-side change.** ~20 LOC in `wake.py` (add two fields to the
  JSON body). No schema migration, no new table.
- **Closes the idempotency gap.** `message_id` enables dedup at the gateway
  level, eliminating double-wake waste.
- **Unblocks Hermes-side skill dispatch.** The gateway can now see the intent
  before spawning a session, enabling per-intent routing without the Step 0
  peek workaround.
- **Future-proof.** `message_id` in the payload is also the foundation for
  per-intent retry policies (ADR-002 §5.3 Q2) and delivery acknowledgements.

**❌ Cons:**

- **Hermes-side changes are out of scope for this repo.** The bridge can
  surface the data, but the gateway webhook adapter must be updated to
  consume it. This is a `hermes-agent` upstream change, not a bridge change.
- **Idempotency store.** The gateway needs to track seen `message_id` values.
  An in-memory LRU (e.g. 1000 entries, ~5 min TTL) is sufficient for the
  current 9-profile fleet but adds state to the gateway.
- **HMAC signature covers the body.** Changing the body changes the signature.
  Old gateways that verify the signature will see a different body → signature
  mismatch. **Mitigation:** the gateway must be updated to handle the new
  fields before the bridge ships the change. Alternatively, use a rolling
  upgrade: the bridge adds fields, the gateway's signature verification
  ignores unknown fields (it already treats the body as opaque per `wake.py`
  comment: *"the gateway's webhook adapter treats this as an opaque event
  body"*).

### 3.2 Option B — Separate webhook for intent-aware wakes

Add a second webhook endpoint (e.g. `POST /wake-intent`) that carries the full
payload. The old endpoint stays for backward compat. New gateways subscribe to
the new endpoint; old gateways keep using the old one.

**✅ Pros:**

- Zero risk to existing webhook signature verification.
- Clean separation of v1 and v2 wake semantics.

**❌ Cons:**

- **Doubles the webhook surface.** Two endpoints, two configs, two code paths
  in the gateway.
- **Config drift.** Each profile's wake-registry must be updated to point to
  the new endpoint. If one profile is missed, it silently falls back to v1.
- **Overkill.** The existing payload is already documented as opaque to the
  gateway — adding fields is within the existing contract.

### 3.3 Option C — Keep the workaround, defer indefinitely

Accept that Step 0 (skill-side peek + routing) is the permanent solution.
Document the idempotency gap as a known limitation.

**✅ Pros:**

- Zero implementation cost.

**❌ Cons:**

- The double-wake cost persists. At ~$0.14/wake for Opus and ~2391 automatic
  wakes observed in one day (ADR-002 §4.1 rationale), even a 5% retry rate
  wastes ~$17/day.
- The skill propagation race (risk #4) remains a silent failure mode.
- ADR-002's "v0.7 hook" promise is broken permanently. The ADR says "that is
  the v0.7 hook where Hermes-side routing lands" — five versions later, it
  hasn't landed, and this option says it never will.

### 3.4 Option D — Full ADR-002 v2 delivery (intent + retry + accepted-intents)

Deliver all three deferred items at once: payload surfacing, per-intent
retry/timeout, and `agent_list` accepted-intents.

**✅ Pros:**

- Completes ADR-002 v2 in one pass.

**❌ Cons:**

- **Hermes-side dependency.** Per-intent retry policy and accepted-intents
  require gateway-side changes that are out of scope for this repo.
- **Bigger blast radius.** Shipping three changes at once increases the risk
  of regression and makes review harder.
- **YAGNI.** The payload surfacing is the foundation for the other two; they
  can be built incrementally once the payload carries the data. Shipping the
  payload first is the natural sequencing.

## 4. Decision

**Adopt Option A (surface `intent` + `message_id` in the webhook payload).**

### Rationale

1. **It's the natural next step.** ADR-002 explicitly deferred this to "v0.7".
   The bridge is now at v0.11.0. The deferral has outlived its rationale —
   the v1 enum is stable (5 intents, unchanged since v0.6.0), the `messages`
   table already stores `intent` and `id`, and the `wake.py` code already has
   both values in scope when building the payload.

2. **It's cheap.** ~20 LOC in `wake.py`. No schema migration. No new table.
   No new env var. The payload is already documented as opaque to the gateway,
   so adding fields is within the existing contract.

3. **It closes a real cost leak.** The double-wake problem (risk #1) wastes
   LLM call budget on retry. `message_id` in the payload enables gateway-side
   dedup with a trivial in-memory LRU. The ROI is immediate.

4. **It unblocks Hermes-side evolution.** Once the gateway can see `intent`
   before spawning a session, the Step 0 workaround can be simplified (no
   `inbox_peek` needed for routing), and specialized skills (`a2a-task-exec`)
   can be dispatched directly. These are Hermes-side changes, tracked
   separately, but they are **blocked** until the bridge delivers the data.

5. **It's backward-compatible.** Old gateways ignore the new fields. The
   rolling upgrade path is: update the gateway's signature verification to
   treat the body as truly opaque (it already should, per the code comment),
   then ship the bridge change.

### Implementation plan

**Bridge-side (this repo, v0.12):**

1. `wake.py` — add `intent` and `message_id` to the JSON payload body.
2. Update the code comment to reflect that the payload now carries
   `intent` and `message_id` (no longer purely opaque).
3. Update the wake-registry docstring to mention the new fields.
4. Add a test: `test_wake_payload_contains_intent_and_message_id`.
5. CHANGELOG entry.

**Hermes-side (tracked separately, not blocking):**

6. Gateway webhook adapter: read `intent` and `message_id` from the payload.
7. Gateway idempotency: in-memory LRU of seen `message_id` values (keyed by
   `target` agent_id). Skip session spawn if already seen.
8. Gateway intent injection: pass `intent` into the session prompt so the
   skill knows the intent without calling `inbox_peek` first.
9. (Future) Gateway skill dispatch: route `intent=execute` to
   `a2a-task-exec` instead of `a2a-inbox-triage`.

### Signature compatibility

The webhook body is signed with HMAC-SHA256 using the shared webhook secret.
Adding fields changes the body, which changes the signature.

**Current state:** the gateway's webhook adapter verifies the signature by
recomputing it over the received body. Since the body is treated as opaque
(per the `wake.py` code comment), the gateway does not parse it before
verification — it verifies the signature of whatever bytes it received, then
parses the JSON. **Therefore, adding fields is safe**: the gateway verifies
the signature of the new body (which matches, since the bridge signed it),
then parses the JSON (which now has extra fields it can ignore or consume).

The only failure mode is if the gateway has a **body whitelist** that rejects
unknown fields — but no such whitelist exists in the current Hermes webhook
adapter (confirmed by the code comment: *"treats this as an opaque event
body"*).

**Rolling upgrade order:**

1. Ship the bridge change (v0.12). Old gateways continue to work — they
   verify the new body's signature (which is valid) and ignore the new fields.
2. Ship the gateway change (hermes-agent, independent release). New gateways
   consume `intent` and `message_id` from the payload.

No coordinated upgrade window is needed.

### What remains deferred

| Item | Status | Why |
|------|--------|-----|
| Per-intent retry/timeout policy | Deferred | Requires gateway-side session budget management. The payload now carries `intent`, so this can be built incrementally on the Hermes side. |
| `agent_list` accepted-intents | Deferred | Requires a new field in the `agents` table and the `agent_list` MCP tool output. Low urgency — no consumer needs it yet. |
| Specialized skill dispatch (`a2a-task-exec`) | Deferred (Hermes-side) | The bridge delivers the data; the gateway must build the dispatch. Tracked in the `a2a-workflow` skill. |
| `fyi` skip-if-peeked optimization | Deferred (Hermes-side) | The gateway can now see `intent=fyi` in the payload and skip the session if the inbox cache already has the message. Small optimization, not urgent. |

## 5. Consequences

### 5.1 Positive

- The webhook payload now carries the data needed for gateway-side routing and
  idempotency. The bridge's part of the ADR-002 v2 contract is fulfilled.
- Double-wake waste can be eliminated once the gateway implements `message_id`
  dedup.
- The Step 0 workaround (skill-side `inbox_peek` for intent) can eventually be
  simplified to a direct read from the session prompt — but this is a
  Hermes-side change, not blocking.
- `message_id` in the payload is the foundation for delivery acknowledgements
  (bridge knows whether the gateway accepted the wake) in a future iteration.

### 5.2 Negative

- The bridge now has a slightly larger public contract (two more fields in the
  webhook body). Removing them in the future would be a breaking change.
- The Hermes-side changes (items 6-9 in the implementation plan) are not
  delivered by this ADR. The bridge-side change is necessary but not sufficient
  — the full benefit requires the gateway to consume the new fields.
- The idempotency LRU in the gateway adds in-memory state. If the gateway
  crashes and restarts, the LRU is lost, and a retry within the LRU's TTL
  window could double-wake. This is acceptable (the LRU is a best-effort
  optimization, not a guarantee), but should be documented.

### 5.3 Open questions

- **Should the payload also include `intent` for `fyi` messages (which skip
  the wake)?** Yes — even though `fyi` doesn't trigger a wake, if the wake
  is eventually sent (e.g. a future "soft wake" for `fyi`), the payload
  should carry the intent. No harm in including it; the `intent` field is
  already on the message row.
- **Should `message_id` be the bridge's internal row ID or a UUID?** It should
  be the message's `id` field (already a UUID, stored in `messages.id`).
  No new ID space needed.
- **Should the gateway's idempotency LRU be persisted to SQLite?** Leaning:
  **no for v0.12**. An in-memory LRU with a 5-minute TTL is sufficient for
  the retry window (bridge retries within seconds, not minutes). Persistence
  adds write amplification for negligible benefit.

## 6. References

- [ADR-002 — Wake-up intent coupling](ADR-002-wake-intent-coupling.md) — the
  parent ADR whose v2 deferral this ADR addresses (§4.1, §5.3 Q2, Q3).
- `src/a2a_mcp_bridge/wake.py:339-347` — current webhook payload construction
  (the code to be modified).
- `src/a2a_mcp_bridge/intents.py` — `VALID_INTENTS`, `DEFAULT_INTENT`,
  `normalize_intent()`, `wakes()` — the intent vocabulary surfaced in the
  payload.
- `a2a-workflow` skill (Hermes-side) — the Step 0 workaround (inbox_peek for
  intent routing) that this ADR's payload change enables simplifying.
- Bridge review 2026-06-19 — finding B6 ("Intent non transmis dans le payload
  webhook").
- ADR-002 §4.1 rationale — the 2391 automatic wakes at ~$0.14 each that
  motivated the `fyi` skip-wake optimization; the same cost driver applies to
  double-wake elimination via `message_id` idempotency.
