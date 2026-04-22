# ADR-001 — Multi-session concurrency model for Hermes profiles on A2A

- **Status:** Accepted (documenting reality), Resolution pending
- **Date:** 2026-04-22
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

### 3.1 Option A — Leader election per profile

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

**Adopt a hybrid of B + C as the v0.5 path, keeping A and D on the roadmap
for later.**

Concretely, v0.5 of the bridge will:

1. **Per-resource advisory locks** for the mutating tools that plausibly
   race across sessions. Out of scope for the bridge itself (skills,
   memory, Obsidian live in Hermes), but documented as a prerequisite in
   the integration guide.
2. **`agent_inbox_peek(since_ts)`** — a new read-only tool that returns
   messages whose `read_at` falls after a given timestamp, so a session
   that woke up mid-conversation can surface what its siblings consumed.
   No mark-as-read side-effect. Callers can use it to reconstruct a
   merged view.
3. **`session_id` metadata in `agent_send`** — optional but recommended.
   Propagates into `agent_inbox` payloads so recipients can correlate a
   reply with the exact sender session.
4. **Session-tagged logs in the bridge** — every log line carries the
   caller's `session_id` when provided.
5. **Clarified tool docstrings** — `agent_id` is documented as "a profile,
   potentially served by multiple concurrent sessions", with a pointer
   to this ADR.
6. **README "Known limitations" section** — this ADR is surfaced on the
   project landing page so external users are not blindsided.

Option A (leader election) and D (per-session identity) are deferred
until we have real traffic patterns that justify their added complexity.

## 5. Consequences

### 5.1 Positive

- External users and downstream agents get honest documentation instead
  of an implicit guarantee the bridge does not actually offer.
- The v0.5 additions are incremental and backward-compatible.
- Context theft becomes recoverable (via `agent_inbox_peek`) even when
  it is not prevented.

### 5.2 Negative

- The bridge ships v0.5 with a known semantic gap. Users building
  conversation-critical flows on top of it must design around it.
- Hermes-side work is required to exploit the new primitives (inbox peek
  integration, file locking on mutations). The bridge alone cannot close
  the gap.

### 5.3 Open questions

- Is `session_id` generated by the gateway, the agent runtime, or the
  bridge on first call? Leaning: **gateway** (it owns session spawn).
- What is the retention policy for peek history? Leaning: **same as
  inbox** (bounded by SQLite size; no separate TTL).
- Should `agent_subscribe` also report sibling activity, or only new
  messages? Leaning: **only new messages** (keep the primitive simple;
  siblings are explicitly queried via peek).

## 6. References

- Discovery conversation: 2026-04-22 Telegram chat, VLBeauClaudeOpus ↔
  Vincent, re "root cause #9: cross-session interleave".
- Bridge v0.4.4 release notes (webhook wake-up mechanism that enabled
  the concurrency to become routine rather than accidental).
- Related Hermes topic: skills and memory are per-profile but have no
  in-process locking; discussed in the `byterover` skill gotchas.
