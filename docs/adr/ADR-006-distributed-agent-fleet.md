# ADR-006 — Distributed Agent Fleet — Multi-Node Hermes Deployment

- **Status:** Proposed
- **Date:** 2026-04-26
- **Authors:** VLBeauClaudeOpus (architect, vlbeau-opus), Vincent Lefebvre

## 1. Context

ADR-001 established the **leader-at-gateway** pattern and atomic mark-as-read
inbox semantics on a single-machine A2A bus (SQLite at `~/.a2a-bus.sqlite`).
ADR-002 introduced the `intent` enum on `agent_send` with its binary wake axis
(`intent=fyi` skips webhook, all others trigger it). ADR-005 built
fire-and-wait orchestration on top of these primitives.

All of this assumes a **single filesystem**: all nine Hermes gateways share
one VPS, one SQLite file, one signal directory, and `localhost` webhook
endpoints.

### 1.1 Current state — mono-VPS

```
┌──────────────────────────────────────────────────────────────────┐
│  Hetzner VPS (Ubuntu 24.04, 8 GB RAM) — single node              │
│                                                                  │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐        ┌──────────────┐  │
│  │ gateway  │ │ gateway  │ │ gateway  │  ...   │ gateway      │  │
│  │ opus :P1 │ │ sonnet46 │ │ gemini   │        │ magent   :P9 │  │
│  │   :P1/wake│ │  :P2:8099│ │glm51:8100│        │              │  │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘        └──────┬───────┘  │
│       │            │            │                      │          │
│       └────────────┴────────────┴────────────┬─────────┘          │
│                                              │                    │
│                               ┌──────────────▼──────┐             │
│                               │  A2A SQLite Bus       │             │
│                               │  ~/.a2a-bus.sqlite    │             │
│                               │  (WAL, single-writer) │             │
│                               └──────────────────────┘             │
│                                                                    │
│  ┌──────────────────────────────┐                                  │
│  │  Reverse SSH Tunnel           │                                  │
│  │  Mac ⟵──ssh -R─── VPS        │                                  │
│  │  localhost:9001 ⟶ Mac:8008   │                                  │
│  │  (vlbeau-qwen36 inference)   │                                  │
│  └──────────────────────────────┘                                  │
└────────────────────────────────────────────────────────────────────┘
```

The Qwen 3.6 model runs on a Mac (local, MPS, 24 GB+ VRAM), reached via a
reverse SSH tunnel. The `vlbeau-qwen36` gateway on the VPS calls
`http://localhost:9001`, which is proxified to `Mac:8008`.

### 1.2 Why distribute now

| Driver | Detail |
|--------|--------|
| **GPU isolation** | Qwen 3.6 and future heavy models (70B+, image generation) require GPU/MPS hardware the VPS lacks. Colocating inference on the VPS is impossible (§9). |
| **Cost elasticity** | GPU cloud instances (RunPod, Lambda) are economical only when billed per-task via spot pricing. A fleet model lets us spin nodes on demand. |
| **Resilience** | Today, VPS failure = total fleet outage. Separate nodes allow partial operation. |
| **Latency for local tools** | Heavy tools (terminal, browser) benefit from running on the same node as the agent that wields them. |
| **VPS RAM limits** | 8 GB constrains concurrent sessions. Offloading profiles to other nodes reduces pressure. |

### 1.3 Invariants to preserve

The distributed architecture **must not break** existing ADR contracts:

- **[ADR-001](./ADR-001-multi-session-concurrency.md)** — leader-at-gateway, atomic mark-as-read,
  `agent_inbox_peek(since_ts)`. Profiles remain unique identities regardless of
  node placement.
- **[ADR-002](./ADR-002-wake-intent-coupling.md)** — `intent` enum semantics, binary wake axis.
  `intent=fyi` must still skip wake-up cross-node.
- **[ADR-005](./ADR-005-fire-and-wait-orchestration.md)** — fire-and-wait pattern.
  `agent_send(execute)` → wake → subscribe loop must function across node boundaries.

This is an **infrastructure evolution**, not a protocol change. The A2A bus
remains a logical abstraction; only its transport layer changes.

## 2. Decision

Enable Hermes agents to run across N machines while sharing a single
logical A2A bus. The approach has three pillars:

1. **Shared bus across nodes** — initially via HTTP API façade over single-writer
   SQLite (Option 3A), evolving to managed PostgreSQL (Option 3B) if N > 3
   or API latency becomes bottlenecks.
2. **Cross-NAT wake delivery** — default to long-poll via `agent_subscribe`
   (Option 4B) for NAT-ed nodes; reverse SSH tunnels (Option 4A) as opt-in
   fallback for latency-sensitive agents.
3. **Tool locality model** — each profile declares a **home node** that
   determines which tools run natively. Cross-node tool calls are out-of-scope
   (tool proxy RPC deferred to future ADR).

## 3. Shared bus across nodes

The current SQLite bus works because every gateway reads/writes the same
local file. Distributed nodes need a transport layer that exposes the same
semantics (single logical writer, atomic mark-as-read, replay via
`since_ts`).

### 3.1 Options

| Option | Transport | Pros | Cons | Latency | Ops cost |
|--------|-----------|------|------|---------|----------|
| **A: SQLite + HTTP API façade** | REST over TLS; single node owns SQLite, others call API | No schema migration. Keeps ADR-001 semantics. Simplest step 1. | Single-writer bottleneck. API availability = bus availability. | ~5-20 ms per `agent_send` vs local | Low: one extra process on VPS |
| **B: PostgreSQL managed** | Postgres shared db (Supabase, RDS, self-hosted) | Multi-writer safe. Native triggers for notify. Proven at scale. | Requires schema migration. External dependency. Adds infra complexity. | ~1-5 ms (direct driver) | Medium: managed DB $10-30/mo |
| **C: Message broker (NATS/MQTT 5)** | Pub/sub broker; SQLite for replay history only | Horizontally scalable. JetStream persistence. Decoupled topology. | Over-engineered for N ≤ 5. Different consistency model (at-least-once vs exactly-once). New failure modes. | < 1 ms (local broker) | Medium-High: broker infra + monitoring |

### 3.2 Recommendation: Option A → Option B

**Recommendation:** Option A for Step 1 (Mac as first remote node),
Option B as long-term target if N > 3 or API latency proves problematic.

Rationale:
- Our current message volume (~tens per hour) makes Option C's scalability
  benefits irrelevant. NATS JetStream adds operational complexity without
  addressing a real bottleneck.
- Option A preserves the SQLite file as the single source of truth — all
  ADR-001 invariants hold without code changes. The HTTP façade is thin
  (wraps existing bridge internals).
- Option B becomes necessary only when write-contention on the façade or
  cross-node API latency (Step 2, cloud GPU nodes) degrades UX.

**Option C rejected:** Over-engineered for a single-user fleet of ≤ 5 nodes.
A full pub/sub system introduces ordering guarantees, consumer groups, and
ack semantics that our ADR-001/002 model does not need. The bridge's
current design (SQLite as bus + signal file) is simpler and sufficient.

## 4. Cross-NAT webhook wake-up

ADR-002's wake-up webhook POSTs to `http://localhost:PORT/wake`. This works
when sender and receiver share a loopback interface. It breaks when the
receiver is on a different machine behind NAT (Mac, cloud GPU with ephemeral
IP).

### 4.1 Options

| Option | Mechanism | Pros | Cons | Worst-case latency | Setup cost |
|--------|-----------|------|------|--------------------|------------|
| **A: Reverse SSH tunnel** | Extend Qwen tunnel pattern: `ssh -R VPS:PORT:MAC:PORT` per node | Encrypted. No third-party. Already proven for Qwen inference. | Fragile — SSH drop = no wakes. One tunnel per node. Requires always-on SSH session. | < 1 ms + tunnel hop | Moderate: SSH key management, autossh/heartbeat |
| **B: Long-poll / agent_subscribe** | NAT-ed nodes replace webhook wait with `agent_subscribe(timeout=55s)` loops | Zero new infra. Already implemented (ADR-001, ADR-005). No tunnel to manage. | Worst-case 55 s wake delay (server-capped). Poller keeps a session alive (token cost). | ≤ 55 s (acceptable for async) | None |
| **C: Broker-driven** | Node subscribes to NATS/MQTT topic; broker pushes wake | No webhooks needed. Scales to many nodes. | Tied to Option 3C. New infra. | < 100 ms | High (broker + subscriptions) |

### 4.2 Recommendation: Option B primary, Option A opt-in

**Recommendation:** Option B (long-poll via `agent_subscribe`) as default
for NAT-ed nodes. Option A (reverse SSH tunnel) as opt-in for
latency-sensitive agents requiring < 1 s wake.

Rationale:
- `agent_subscribe(timeout=55s)` is already the primary blocking primitive
  in ADR-005's fire-and-wait pattern. It is battle-tested.
- A 55 s worst-case wake delay is acceptable for background task execution
  (fire-and-wait, `intent=fyi` status updates). Interactive UX agents (Opus,
  CLI-facing) stay on the VPS with local webhooks.
- Reverse SSH tunnels add failure surface (SSH connection drops, heartbeat
  restarts, port conflicts). They remain viable for specific agents like
  `vlbeau-qwen36` which already need tunnel connectivity for inference URLs.

### 4.3 Wake path comparison

```
Current (same node):
  sender─agent_send─▶bridge─SQLite write─▶signal touch─▶webhook POST─▶gateway wakes

Long-poll (cross-NAT):
  sender─agent_send─▶bus─SQLite write─▶signal/API notification─▶subscriber unblocks
  (no webhook; receiver was already polling)

Tunnel (cross-NAT):
  sender─agent_send─▶bus─SQLite write─▶signal touch─▶webhook POST─▶tunnel─▶gateway
  (one extra network hop through SSH)
```

## 5. Tool locality

Hermes tools have hard locality constraints: filesystem access, GPU memory,
network egress endpoints, and secrets cannot be assumed available on every
node.

### 5.1 Tool × node matrix

| Tool family | VPS node | Mac node | GPU cloud node | Proxifiable? |
|-------------|:--------:|:--------:|:--------------:|:------------:|
| `terminal` | Native | Native | Native | No — FS and process namespace are node-local |
| `file` / `read_file` | Native | Native | Native | No — filesystem paths are node-local |
| `patch` / `write_file` | Native | Native | Native | No — write locality matters (which FS?) |
| `browser` (headless) | Native | Native | Native | Partial — can proxy a Chromium instance |
| `image_generate` (SD) | N/A | Partial (MPS) | Native | No — requires local GPU + model weights |
| `LLM inference` (vllm, llama.cpp) | N/A (no GPU) | Native (MPS, high VRAM) | Native (CUDA) | No — requires local model inference |
| `delegate_task` (spawn subagent) | Native | Native | Native | No — subagent inherits parent's node |
| `a2a` tools (agent_send, inbox, ping) | Cross-node via bus | Cross-node via bus | Cross-node via bus | **Natively cross-node** |
| `webhook` / `send_message` (Telegram/Discord) | Native (public IP) | NAT-ed (needs tunnel) | Ephemeral IP | Partial — needs persistent endpoint |

### 5.2 Home node concept

Each Hermes profile declares a **home node** in its configuration:

```yaml
# vlbeau-qwen36 config
home_node: mac-lan
inference_url: http://localhost:8008

# vlbeau-opus config
home_node: vps-hetzner
inference_url: https://openrouter.ai/api/v1/chat/completions

# vlbeau-heavy config (future)
home_node: cloud-gpu-runpod
inference_url: http://localhost:8080
```

The home node determines:
- Which tools are available natively (filesystem, terminal, browser).
- Which wake delivery mechanism applies (§4 — local webhook vs long-poll).
- The **tool locality guarantee**: if an agent needs GPU access, schedule
  it on the profile whose `home_node` has GPU hardware.

### 5.3 Future: tool proxy RPC

A **tool proxy** could expose tools from one node to another via
JSON-RPC over the shared bus (e.g., `vlbeau-opus` on VPS calls
`terminal` on `mac-lan` through a proxied request). This is deferred to a
future ADR — it introduces latency, authentication, and sandboxing
challenges not required for the initial fleet.

## 6. Security

Distributing agents across nodes widens the attack surface: each node adds
a new network endpoint, process to patch, and secret rotation cycle.

### 6.1 Inter-node authentication

Two approaches considered:

| Mechanism | Detail | Tradeoff |
|-----------|--------|----------|
| **mTLS / Tailscale** | Each node runs a Tailscale or Wireguard mesh agent. Inter-node HTTP API uses mTLS certificates. Network-level identity enforcement. | Simple for private fleet (Tailscale is free for ≤ 20 devices). Certificates need rotation. |
| **JWT signed by gateway** | Each `agent_send` on the API façade carries a JWT signed by the sending gateway's private key. Receiver validates signature. | Application-layer auth. No network overlay needed. Requires key distribution. |

**Recommendation:** Tailscale for the initial fleet (Opus, Mac, GPU cloud).
It provides mTLS, NAT traversal, and node discovery in one package. JWT
validation can be layered on top for message-level provenance.

### 6.2 Encryption in transit

| Channel | Encryption |
|---------|------------|
| HTTP API façade (bus) | TLS (Tailscale provides this implicitly) |
| SSH tunnels | SSH protocol (AES-256-GCM) |
| SQLite bus at-rest | Currently unencrypted (today's baseline). Future: SQLCipher if needed. |
| Broker connections (if Option 3C) | WSS / TLS |

SQLite at-rest encryption is explicitly out-of-scope for this ADR. The VPS
disk is already encrypted (Hetzner default), and the bus contains no PII
(agent messages are operational, not user data).

### 6.3 Agent authenticity on the bus

Today, `agent_id` is purely declarative — any process can claim to be
`vlbeau-opus` and call `agent_send` or `agent_inbox`. This was acceptable
on localhost. On a multi-node bus, it is not.

**Proposed (future ADR):** embed a gateway-signed token in every
`agent_send` and `agent_inbox` call. The bus adapter validates the token
against a registry of known gateway public keys.

Grace period: tolerate unsigned calls for 30 days after rollout, logging
a `WARN` for each. After grace period, reject unsigned calls.

### 6.4 Trust domains

Nodes have different trust levels:

| Node | Trust level | Rationale |
|------|:-----------:|-----------|
| `vps-hetzner` | High | VPS is the production baseline. Controlled access, monitoring. |
| `mac-lan` | Medium | Vincent's personal machine. Physically secured but less monitored. |
| `cloud-gpu-*` | Low | Spot instances on shared infrastructure. Ephemeral, untrusted hypervisor. |

**Proposed rule:** a low-trust node cannot send `intent='execute'` to a
high-trust agent without a pre-authorized token mapping in the bus adapter.
`intent='fyi'` messages flow freely (read-only impact). This prevents a
compromised spot instance from commanding production agents.

## 7. Progressive migration plan

### 7.1 Step 0 (baseline — now)

VPS-only, Qwen inference via reverse SSH tunnel. No changes.

### 7.2 Step 1 — Mac as first remote node

**Prerequisites:**
- Deploy `a2a-mcp-bridge` binary on Mac (macOS ARM64).
- Configure Hermes gateway with profile `vlbeau-qwen36`, `home_node: mac-lan`.
- Deploy HTTP API façade on VPS (wraps SQLite bus).
- Mac connects to bus API via Tailscale.

**Scope of change:**
- **Code:** `a2a-mcp-bridge` — new `--bus-url` flag to point to remote bus API instead of local SQLite path. SQLite read path unchanged (façade proxies it).
- **Config:** Mac gateway config sets `bus_url: https://vps-hetzner:8443/bus` and `wake_mode: long_poll`.
- **Skills:** No changes to skills. ADR-001/002/005 contracts hold as-is.

**Wake delivery:** `agent_subscribe(timeout=55s)` long-poll loop.

**Success criteria:**
1. `agent_send("vlbeau-qwen36", intent='execute', ...)` from `vlbeau-opus`
   on VPS triggers a wake on Mac within ≤ 55 s.
2. Mac agent executes task, sends reply via `intent='fyi'` back through bus.
3. Opus receives reply in its `agent_subscribe()` loop.
4. End-to-end wall clock < 10 s (excluding model inference time).
5. No changes to ADR-001/002/005 contracts.

**Rollback:** Revert Qwen gateway to VPS config (localhost tunnel). Single
config flag change.

**Observability:** Log `bus_api_latency_ms` on each `agent_send` /
`agent_inbox`. Alert if p99 > 50 ms.

### 7.3 Step 2 — N-node generalization

**Prerequisites:**
- Step 1 validated, < 10 ms API latency median.
- Identify profile candidate for GPU offload (e.g., `vlbeau-heavy`).

**Scope of change:**
- Extract "gateway + bus-adapter" as a **deployable template**: systemd unit
  file + `config.yaml` skeleton. One-click deploy to new node.
- Provision first GPU cloud node (RunPod spot, A10G or L4).
- Introduce `node_trust_level` in bus adapter config (§6.4).

**Observability additions:**
- Node registry in bus: `{"node_id", "status", "last_heartbeat", "trust_level"}`.
- Per-node message counters (sent/received).
- Latency histogram: local SQLite write vs API façade write.

**Rollback:** Stop GPU node, reassign profile to VPS or Mac.

### 7.4 Step 3 (conditional, N > 3 nodes)

**Trigger:** Step 2 metrics show ≥ 20 ms p99 API latency, or write-contention
errors on the SQLite façade, or operational load from managing > 3 nodes.

**Scope:** Migrate bus from Option 3A (SQLite + API) to Option 3B
(PostgreSQL). Requires:
- Schema migration (SQLite → Postgres DDL).
- Bridge config update: `db_url: postgres://...`.
- Downtime window: ~5 min (read-only mode during migration).

**Evaluation:** Run Step 3 only after collecting ≥ 2 weeks of Step 2 metrics.
If latency is acceptable and node count stays ≤ 3, defer indefinitely.

## 8. Cost and latency impact

### 8.1 Latency per operation

| Operation | Current (local SQLite) | Step 1 (HTTP API façade) | Step 2 (cloud GPU, long-poll) |
|-----------|------------------------|--------------------------|-------------------------------|
| `agent_send` write | ~1 ms | ~5-20 ms (TLS + API round-trip) | ~5-20 ms (same path) |
| `agent_inbox` read | ~1 ms | ~5-20 ms | ~5-20 ms |
| Wake delivery | < 1 ms (webhook localhost) | ≤ 55 s (long-poll cycle) | ≤ 55 s (same) |
| SSH tunnel wake (opt-in) | N/A | < 1 ms + tunnel overhead | < 1 ms + tunnel overhead |

The HTTP API façade adds ~5-20 ms per bus operation. This is negligible
for our current message volume but could compound in fire-and-wait loops
with many round-trips (see ADR-005 §9.1 on observed behavior).

### 8.2 Infrastructure cost

| Step | Component | Cost estimate |
|------|-----------|---------------|
| 0 (baseline) | Hetzner VPS (included) | Included in existing infra |
| 1 (Mac node) | Mac already running | $0 marginal |
| 1 (Tailscale) | Free tier (≤ 20 devices) | $0 |
| 2 (GPU cloud) | RunPod spot A10G/L4 | $0.40-1.20/h, billed only during heavy tasks |
| 2 (managed DB, future) | Supabase/RDS PostgreSQL | $10-30/mo |
| 3 (broker, Option 3C) | Self-hosted NATS | Operator time only, no license cost |

### 8.3 Tail latency considerations

A worst-case long-poll of 55 s is **acceptable** for:
- Background task execution (fire-and-wait, ADR-005).
- Status updates (`intent='fyi'`).
- Deferred task queue processing.

It is **unacceptable** for:
- Interactive chat (Opus responding to Telegram user).
- Review cycles requiring rapid back-and-forth.

Interactive agents remain on the VPS with local webhook wake-up. The
distributed model is designed for **asynchronous task delegation**,
not synchronous conversation.

## 9. Rejected alternatives

| Alternative | Verdict | Rationale |
|-------------|---------|-----------|
| **Full P2P gossip bus** (libp2p) | Rejected | Topology dynamics would make debugging a nightmare. Overly complex for N ≤ 5. |
| **Kubernetes + service mesh** | Rejected | Massive opex for 2-5 nodes and a single user. Istio/Linkerd add latency and complexity with no ROI. |
| **Colocate Qwen on VPS** | Rejected | Hetzner VPS has no GPU. Qwen 3.6 quantized requires > 8 GB RAM for context window alone. This is the starting problem, not a solution. |
| **Single Mac everything** | Rejected | Mac lacks stable public IP — webhook delivery and outbound send_message to Telegram/Discord are impossible without persistent tunnel. Mac is not always powered on. |

## 10. Open questions

| # | Question | Impact | Owner |
|---|----------|--------|-------|
| 1 | **Dynamic node discovery** — should the bus maintain a central node registry (updated by heartbeat), or is a static config file propagated to all nodes sufficient? | Affects Step 1 config complexity. | Infrastructure |
| 2 | **Heartbeat visibility** — should `agent_list` reflect node host status (alive/down) in addition to agent profile `last_seen_at`? Requires a bus-level heartbeat protocol. | Observability and routing decisions. | Bridge |
| 3 | **Offline message expiry** — when a message is sent to an agent whose node is down, should it persist indefinitely (current SQLite behavior) or acquire a TTL? | Storage growth and stale message handling. | Bridge |
| 4 | **ADR-003 worktree isolation** — if the same profile has two instances on two nodes, do git identity and working tree races occur? The current ADR-003 assumes one profile = one worktree. | Worktree integrity. | Skills / Config |
| 5 | **Secret rotation** — how to rotate bus API keys across nodes without downtime? | Operational security. | Infrastructure |
| 6 | **Cross-node signal file** — the `A2A_SIGNAL_DIR` mechanism (ADR-005) only works on a shared filesystem. How does a remote node trigger wake on the VPS? | Already addressed: long-poll replaces signal file on remote nodes. | Confirmed: long-poll (§4.2) |

## 11. References

- **[ADR-001](./ADR-001-multi-session-concurrency.md)** — Leader-at-gateway, multi-session concurrency, atomic mark-as-read, `agent_inbox_peek`.
- **[ADR-002](./ADR-002-wake-intent-coupling.md)** — Intent enum, `intent=fyi` skips wake webhook, binary wake axis.
- **[ADR-003](./ADR-003-worktree-isolation-and-git-identity.md)** — Worktree isolation and git identity (referenced in §10, question 4).
- **[ADR-005](./ADR-005-fire-and-wait-orchestration.md)** — Fire-and-wait orchestration, `agent_subscribe` long-poll pattern.
- **NATS JetStream docs** — <https://docs.nats.io/nats-concepts/jetstream> (referenced for Option 3C comparison).
- **Tailscale** — <https://tailscale.com> (recommended for inter-node mesh networking).
- **WireGuard** — <https://www.wireguard.com> (alternative to Tailscale).
