# ADR-001 — Multi-session concurrency model for Hermes profiles on A2A

- **Status:** Accepted — resolution path chosen (Option A′), implementation pending
- **Date:** 2026-04-22
- **Last updated:** 2026-04-22 (decision revised from hybrid B+C to A′ after review)
- **Context window:** post v0.4.4
- **Authors:** VLBeauClaudeOpus (architect), Vincent Lefebvre

## 1. Context

`a2a-mcp-bridge` assumes **one agent identity = one logical consumer** of its
inbox. In the current Hermes deployment this assumption is violated: a single
profile (e.g. `vlbeau-glm51`) can run as several concurrent OS processes at the
same time, because multiple independent entry points can spawn an agent
session for the same profile:

- **Telegram front-end** — a user sends a message, Hermes gateway spawns a
  session.
- **HTTP webhook wake-up** (v0.4.4) — another agent calls `agent_send`, the
  bridge POSTs the wake URL, the gateway spawns a *different* session.
- **CLI / cron / direct invocation** — manual or scheduled runs spawn yet
  another session.

Each of these sessions:

1. Loads the profile's `config.yaml`, memory, skills, Obsidian vault, etc.
2. Reads `agent_inbox(unread_only=True)` at most once per turn. Because the
   read is **atomic mark-as-read**, the first session to poll wins the
   message — the others never see it.
3. Keeps its own conversation history in RAM for the duration of the session.
4. Can mutate shared resources: skills (`skill_manage`), memory
   (`memory add/replace`), Obsidian notes, git repositories, the A2A bus
   itself.

This was discovered in practice on 2026-04-22: while the user was actively
chatting with one `vlbeau-glm51` session on Telegram, a parallel session
(webhook-spawned) silently processed messages from Qwen and DeepSeek, acked
them, patched a reasoning error in a poker EV calculation, and moved on.
The Telegram session was never aware any of this happened.

## 2. Problem statement

The property "I am talking to agent X" is an illusion. In reality the user
is talking to **one of N threads** that share a name, a disk, and some MCP
servers, but **not an in-memory state, not a conversation history, and not a
turn-by-turn awareness of each other**.

### 2.1 Concrete risks

| # | Risk | Severity | Example |
|---|------|----------|---------|
| 1 | **Context theft** — an inbound message with information critical to the user's live conversation is consumed by another session and invisibly "resolved" | High | User debugs issue A with session S1; DeepSeek replies via A2A with the missing clue; session S2 (webhook) consumes it and archives the thread. S1 keeps guessing. |
| 2 | **Silent mutation of skills** | High | Session S2 patches `skill-X` while S1 is actively following S1-cached version. S1's behaviour is now inconsistent with its own documentation. |
| 3 | **Silent mutation of memory** | Medium | Session S2 does `memory add`. S1's in-memory snapshot is stale. A fact the user believes was recorded may or may not have been. |
| 4 | **Concurrent file writes** (Obsidian, git working trees, project files) | High | Two sessions patch the same file. Last write wins, diff lost. |
| 5 | **Illusion of identity** | Medium | The user says "we decided X" — but which session decided? No single conversation embodies the decision. |
| 6 | **Duplicate side-effects** | Medium | Two sessions receive overlapping triggers and both send the email / push the commit / ping the other agent. Idempotence is not guaranteed. |
| 7 | **Broken observability** | Medium | A post-mortem requires correlating logs across PIDs — no session-level trace ID exists. |
| 8 | **Cost drift** | Low-Medium | N sessions × M tokens on OpenRouter. The user has no real-time signal of how many sessions are live. |
| 9 | **Self-contradiction** | Low | Two sessions of the same agent reply differently to the same question to the same user. Embarrassing but recoverable. |

### 2.2 Why this is a *bridge* problem (not just a Hermes problem)

The bridge explicitly exposes `agent_id` as a first-class identity and
guarantees delivery to *that* identity. Today it delivers to "whoever
happens to poll first under that name". A downstream agent that calls
`agent_send("vlbeau-glm51", ...)` has no way to know whether they are
talking to the same conversational thread as last time, or to a brand-new
session with no memory of the prior exchange.

The bridge therefore shares responsibility for the semantics of agent
identity, even if the process model that violates it lives in Hermes.

## 3. Options considered

### 3.1 Option A — Leader election per profile (distributed)

One session per profile holds a lease on the bus. Non-leader sessions can
send but cannot consume. The leader forwards relevant messages to other
local sessions via an IPC mechanism (shared file, Unix socket, etc.)
based on session-tagged routing.

- ✅ Strong semantic fix: "1 profile = 1 inbox consumer" is restored.
- ✅ No duplicate wakeups.
- ❌ Lease management is non-trivial (stale leases, liveness, failover).
- ❌ Requires changes to both the bridge and Hermes gateway.
- ❌ What does "leader" mean when the user is actively chatting on
  Telegram but a webhook wake-up arrives? Route to Telegram session
  always? Then webhook path is degraded.

### 3.1-bis Option A′ — Leader-at-gateway-level (exploit existing singleton)

Refinement of A that avoids distributed lease management by observing
that a leader **already exists and is already singleton**: the Hermes
gateway process itself. There is exactly one gateway per profile per
machine.

Design:

1. The gateway is the sole subscriber to the A2A bus for its profile. It
   runs a lightweight background loop that calls
   `agent_inbox(unread_only=True)` (the atomic read) and writes each
   message into a local per-profile cache
   (`~/.hermes/profiles/<id>/inbox-cache/`) with a monotonic
   `seen_at` timestamp.
2. Spawned agent sessions **never** call `agent_inbox` directly against
   the bridge. Instead, they read the local cache at session start
   (full snapshot since `last_seen_ts`) and incrementally on each turn
   (delta since the session's own `last_seen_ts`).
3. When a session performs a "consuming" action (ack, archive, reply),
   it updates a `handled_by` marker in the cache entry. Sibling
   sessions see this on their next delta read and know the thread is
   closed — no duplicate response.
4. `agent_send` from a spawned session still goes straight to the
   bridge (writes are not contended — they are always safe).

- ✅ No distributed lease: exploits the fact that the gateway is
  already a process-level singleton. No bespoke election protocol.
- ✅ Single authoritative read of the bus per profile — no message
  theft between sessions. Matches the semantics the bridge implicitly
  promises.
- ✅ Context cost stays bounded: sessions read a **pre-filtered delta**
  (new messages + recently handled markers), not the raw bus, not the
  full skills tree, not every sibling turn. Typical delta per turn is
  ≤ the size of what actually changed.
- ✅ Survives session crashes independently of the bus.
- ✅ Works with N sessions without changing their internal logic — the
  indirection is purely at the inbox layer.
- ❌ Requires non-trivial Hermes-side work: the gateway needs a
  long-running inbox loop, a cache format, and a session ↔ cache
  read path.
- ❌ Gateway becomes a load-bearing component for A2A delivery. If the
  gateway crashes, sessions go inbox-blind until restart. (Mitigation:
  the bus itself remains the source of truth; a recovery path reads
  directly from the bus when the cache is missing.)
- ❌ Cross-machine scenarios still need a per-machine gateway each
  maintaining its own cache. Fine for the current deployment model
  but something to keep in mind if multi-host profiles appear later.

### 3.2 Option B — Convergence-by-refresh

Keep N sessions, but make every session re-read shared state **before each
user-visible turn**:

- Re-read `agent_inbox(unread_only=False, since=last_turn_ts)` to surface
  messages other sessions consumed.
- Re-read skills / memory from disk (no RAM cache).
- Serialize file writes via filesystem locks (`flock`).

Sessions still race, but each session sees a recent-enough convergent
view before it speaks. Mutations from other sessions become visible
within 1 turn.

- ✅ No single point of failure.
- ✅ Small blast radius of changes (bridge + Hermes side-car).
- ✅ Survives crashed sessions.
- ❌ Turn latency increases (extra reads per turn).
- ❌ Does not prevent context theft *at the moment it happens* — only
  makes it visible on the next turn.
- ❌ Mutation conflicts on skills/memory still need a merge strategy.
- ❌ **Context-window cost scales with sibling activity.** Each turn re-injects
  whatever siblings consumed since the last turn (inbox peek payload +
  re-read skills if they mutated). On a profile with an active webhook
  session running in parallel to a long Telegram conversation, the
  average per-turn context can double or triple. On pay-per-token
  providers (OpenRouter etc.) this turns silent concurrency into silent
  cost drift.

### 3.3 Option C — Accept the chaos, document and mitigate

Acknowledge multi-session as a property of the system. Document it
explicitly. Mitigate the worst cases:

- File locks for skill / memory / Obsidian writes (`flock`).
- Session-tagged log lines (session UUID in every log record).
- A `session_list` MCP tool to enumerate live sessions per profile.
- Deduplicate user-facing side-effects (e.g. "has this message_id been
  acked in the last 60s?" cache).
- Warn downstream agents in the tool docstring that `agent_id` identifies
  a *profile*, not a *conversation*.

- ✅ Zero coordination cost.
- ✅ Matches the reality of what the system already does.
- ❌ Does not fix context theft.
- ❌ Does not fix silent skill/memory mutations — only makes conflict
  less destructive.

### 3.4 Option D — Per-session identity

Each spawned session gets a unique identity like
`vlbeau-glm51/session-<uuid>`. `agent_list` returns both the profile and
its live sessions. Senders target a specific session when they want
conversational continuity, and the profile (load-balanced) when they do
not.

- ✅ Semantically honest: identity = the actor, not the label.
- ✅ Enables conversation-pinned routing.
- ❌ Biggest downstream change: every caller now has to reason about
  session identity.
- ❌ Session lifecycle becomes part of the public protocol.

## 4. Decision

**Adopt Option A′ (leader-at-gateway-level) as the v0.5 target, with Option B
kept as a degraded fallback if the Hermes-side work turns out heavier
than expected.**

A′ is preferred because:

- It directly fixes the root cause (message theft between sessions) rather
  than making it visible after the fact.
- It does not inflate the per-turn context window — a concern for Option B
  that is real on pay-per-token providers and grows with sibling activity.
- It exploits a singleton that already exists (the gateway) instead of
  inventing a new distributed coordination primitive.

Concretely, the v0.5 milestone spans both this bridge and the Hermes
gateway:

**Bridge-side (this repo)**

1. **`agent_inbox_peek(since_ts)`** — new read-only tool that returns
   messages whose `read_at` falls after a given timestamp, with no
   mark-as-read side-effect. Used by the gateway cache for recovery
   and by tooling that wants a global view without consuming. Also
   covers the B fallback should we need it.
2. **Optional `session_id` metadata on `agent_send`** — propagates into
   `agent_inbox` / cache entries so recipients can correlate a reply
   with the exact sender session.
3. **Session-tagged logs** — every log line carries the caller's
   `session_id` when provided.
4. **Clarified tool docstrings** — `agent_id` is documented as "a
   profile, potentially served by multiple concurrent sessions behind
   a local gateway cache", with a pointer to this ADR.

**Gateway-side (Hermes repo, tracked separately)**

5. Long-running inbox loop per profile that drains the bus and writes
   to `~/.hermes/profiles/<id>/inbox-cache/`.
6. Cache read API for spawned sessions (snapshot at spawn, delta per
   turn).
7. `handled_by` / `ack` markers so siblings know when a thread is
   closed.
8. Recovery path: if the cache is missing or lagging, sessions may
   fall back to `agent_inbox_peek` directly against the bridge.

If the gateway-side work slips, the bridge-side primitives (1–4)
degrade gracefully into Option B: sessions call `agent_inbox_peek` at
each turn and reconcile via the bridge. It is a worse UX and a higher
context cost, but it is functional without gateway changes.

Option A (generic distributed leader election) and Option D (per-session
identity exposed on the bus) are deferred. They may become relevant if
we introduce multi-host profiles or conversation-pinned routing.

## 5. Consequences

### 5.1 Positive

- External users and downstream agents get honest documentation instead
  of an implicit guarantee the bridge does not actually offer.
- The v0.5 additions are incremental and backward-compatible.
- Under A′, context theft is **prevented at source** (single bus reader
  per profile) rather than merely recoverable after the fact.
- Per-turn context cost on spawned sessions stays bounded and
  proportional to actual new activity, not to sibling chatter.

### 5.2 Negative

- A′ makes the Hermes gateway a load-bearing component of A2A delivery.
  The bus remains the source of truth, so crashes are recoverable, but
  any latency or bug in the gateway's inbox loop becomes visible as
  delivery lag to all sessions under that profile.
- Hermes-side work (inbox loop, cache format, session read API) is the
  critical path for v0.5. Without it, the bridge-side primitives only
  unlock the degraded B fallback.
- Cross-machine profiles (same `agent_id` served on two hosts) are not
  covered by A′ and would need Option A or D later.

### 5.3 Open questions

- Is `session_id` generated by the gateway, the agent runtime, or the
  bridge on first call? Leaning: **gateway** (it owns session spawn and
  the cache).
- Cache schema: flat JSON files per message, SQLite, or append-only
  log? Leaning: **SQLite** in the profile directory — matches the
  bridge's own design and keeps `handled_by` updates cheap.
- What is the retention policy for the gateway cache? Leaning: **bounded
  by message count** (e.g. last 1000 per profile) with a secondary TTL
  so stale threads eventually age out.
- Should `agent_subscribe` also report sibling activity, or only new
  messages? Leaning: **only new messages** (keep the primitive simple;
  siblings are a gateway-cache concern, not a bus concern).
- What happens if the gateway is down when a wake-up webhook fires?
  Leaning: the webhook spawns a session as today; that session falls
  back to `agent_inbox_peek` directly. Degraded but functional.

## 6. References

- Discovery conversation: 2026-04-22 Telegram chat, VLBeauClaudeOpus ↔
  Vincent, re "root cause #9: cross-session interleave".
- Bridge v0.4.4 release notes (webhook wake-up mechanism that enabled
  the concurrency to become routine rather than accidental).
- Related Hermes topic: skills and memory are per-profile but have no
  in-process locking; discussed in the `byterover` skill gotchas.
