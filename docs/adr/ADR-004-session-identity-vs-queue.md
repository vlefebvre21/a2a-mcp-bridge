# ADR-004 — Session identity: ephemeral-sessions-with-cache vs persistent-session-with-queue

- **Status:** Proposed — decision pending
- **Date:** 2026-04-23
- **Context window:** during v0.5.0 post-mortem; blocks v0.6 implementation
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
- ❌ Doubles the surface: two code paths, two test matrices, two failure modes.
- ❌ Downstream agents (other profiles on the bus) need to know or discover which mode a recipient is in, to reason about whether they're talking to one thread or many. Or we hide this behind a consistent facade (e.g. `agent_list` exposes `session_mode`).

**Cost:** X cost + Y cost + integration layer (~3-4 weeks total if both paths fully implemented).

### 3.4 Option W — Do nothing more (stop at v0.5 primitives)

Keep v0.5 as-is. Document the limitations. The primitives (`agent_inbox_peek`, `session_id` metadata) give downstream agents enough tools to reason about session fragmentation explicitly. Users and agents learn to work with "agent_id = profile, not conversation".

**Fixes:** nothing beyond v0.5.
**Trade-offs:**
- ✅ Zero additional cost.
- ❌ The ADR-001 risks #1-#7 remain.
- ❌ User experience stays confusing (the 2026-04-23 incident is evidence).

Listed for completeness; not recommended.

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

## 5. Recommendation (subject to Vincent's decision)

**Recommend Option Z (hybrid), with Option Y as the default for new profiles and Option X as the migration starting point.**

Reasoning:

- **The user's lived problem** (2026-04-23 Telegram session unaware of concurrent opus session) is a Q2 problem, not a Q1 problem. Option X alone would not have prevented that experience.
- **Heterogeneity is real**: `vlbeau-main` (orchestrator) wants one continuous identity; a future `vlbeau-codereview-batch` might genuinely want N parallel sessions. Forcing one model is wrong.
- **Z lets us ship incrementally**: v0.6 = Option X (cache, items 5-8 of ADR-001). v0.7 = Option Y path, opt-in via config. We do not commit to the full cost upfront.
- **Both paths share the bridge-side primitives** already shipped in v0.5 — no wasted work.

If hybrid is too expensive and we must pick one:

- **Pick Y (persistent)** if Vincent's primary use case is "I talk to Qwen once, across channels, and it should remember". This is the direction that closes the conceptual gap, even if the refactor is real.
- **Pick X (A′)** if batch-parallel workflows are imminent and a persistent-session model would block them. Accept that "illusion of identity" persists; document it prominently.

## 6. Open questions for Vincent

1. **Your daily experience of the bug** — on 2026-04-23 you wanted Qwen to know about "our" conversation across the Telegram-ping and the A2A-ping. That's a Q2 problem. Is it representative? Or is message theft (Q1) the thing that actually hurts?

2. **Multi-channel routing** under Option Y — if `vlbeau-opus` is in persistent mode and you write on Telegram while another agent pings via A2A, should those become consecutive turns in one conversation (integrated context) or should they stay in separate logical threads inside the same session (two sub-conversations)? The former is simpler; the latter needs sub-thread identity.

3. **Blast radius of long sessions** — under Option Y, a session can live for weeks. Are we prepared to invest in context compression, memory rotation, and checkpoint/restore? Or do we cap sessions at a time budget and accept a controlled "new session" boundary (e.g. daily reset)?

4. **Other VLBeau profiles as consumers** — if `vlbeau-qwen36` moves to persistent (Y), its peers sending `agent_send` to it expect one actor. Does `vlbeau-glm51` need to know it's now talking to a persistent Qwen vs an ephemeral one? (Lean: no, the bridge contract is unchanged from sender's view.)

5. **ADR-001 status** — if we adopt this ADR-004, ADR-001 becomes either "superseded by ADR-004" or "scoped to Q1 only, Q2 addressed separately". Lean: ADR-001 stays, scoped to Q1; ADR-004 layers on top for Q2.

## 7. Next steps if accepted

- **v0.6** (bridge-side): implement ADR-001 items 5-8 (Option X cache + read API + handled markers). Issue #14 remains valid, rewrite its acceptance test to assert "no duplicate reply / no orphan handled marker" (not "1 session").
- **v0.7** (Hermes-side): introduce `session_mode: persistent | ephemeral` in profile config. Implement the persistent path (queue format, trigger demux, origin routing). Opt-in per profile starting with `vlbeau-main`.
- **v0.8** (validation): migrate 2-3 additional profiles to persistent, observe behaviour, iterate on compression / context-cap policies.

## 8. References

- ADR-001 — the A′ decision this ADR reconsiders.
- Issue #14 — tracks the v0.6 Option X implementation. This ADR does not cancel it, but clarifies what its acceptance test means.
- Issue #13 (correction comment) — 2026-04-23 retex of v0.5 showing N-session behaviour persists within one process.
- 2026-04-23 live test in bus `~/.a2a-bus.sqlite` (messages `8c4ce4f1`, `3f3d51c8`, `a3f97f03`, plus opus-side session duplication) — concrete evidence that A′ alone does not close the user-facing gap.
