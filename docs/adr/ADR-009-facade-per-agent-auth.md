# ADR-009 — Facade per-agent authentication: shared bearer accepted, migration path defined

- **Status:** Accepted — 2026-06-19 (Option D adopted; Option A documented as migration target)
- **Date:** 2026-06-19
- **Last updated:** 2026-06-19
- **Context window:** post v0.11.0 bridge review
- **Authors:** VLBeauGLM51 (vlbeau-glm51), vlefebvre21
- **Related:** [ADR-006.1 — HTTP Bus Facade](ADR-006.1-http-bus-facade.md), [ADR-007 — File Transfer Primitive](ADR-007-file-transfer-primitive.md)
- **Review finding:** B5 (ACL transferts Phase C basée sur header déclaratif)

## 1. Context

ADR-006.1 introduced the HTTP facade with a **shared Bearer token** authentication
model: one `A2A_FACADE_API_KEY` environment variable, shared between the server
and all authorized clients. ADR-007 Phase C extended the facade with file
transfer endpoints (`/transfers/<id>`) that enforce recipient ACL via a
self-declared `X-Agent-Id` header.

### 1.1 The gap

The ACL on `GET /transfers/<id>` checks that the requesting `agent_id` matches
`transfers.recipient_id`. But `agent_id` is read from the `X-Agent-Id` HTTP
header (`facade.py:474,514`), which is **self-declared by the client**. Since
the Bearer token is shared, any client holding the key can set `X-Agent-Id` to
any value and impersonate any agent.

```python
# facade.py — current ACL (advisory, not enforced)
agent_id = request.headers.get("X-Agent-Id") or request.query_params.get("agent_id", "")
# ... later ...
if agent_id != transfer.recipient_id:
    raise HTTPException(403)
```

The Bearer token authenticates **"you have access to the bus"**, not **"you are
agent X"**. The `X-Agent-Id` header is an assertion, not a proof.

### 1.2 Current threat model

The VLBeau deployment is:

- **Single operator** (Vincent) running all 10 profiles.
- **Private network**: 9 agents on one VPS (localhost), 1 agent on a Mac via
  SSH tunnel. The facade is never internet-exposed.
- **No malicious agents**: all profiles belong to the same trust boundary.
- **Accidental cross-agent reads** are the realistic risk (agent A fetches a
  transfer meant for agent B because of a misconfigured `X-Agent-Id`).

### 1.3 What ADR-006.1 and ADR-007 already say

ADR-006.1 §Authentication model:
> Bearer token is a shared secret — no per-agent ACL. Any client with the token
> can read any agent's inbox. Acceptable for the current trust model (single
> operator, private network).

ADR-006.1 §Future work:
> Per-agent ACL / API keys instead of shared Bearer token.

ADR-007 §4.2 item 7:
> ACL on cross-machine path (C). The `transfers` row is the source of truth;
> the façade refuses `GET /transfers/<id>` unless the `Authorization: Bearer`
> token is valid AND the requesting agent_id matches `transfers.recipient_id`.

ADR-007 §3.1 risk #5:
> Auth bypass — Any process on the same machine reads staged files regardless
> of which agent they target.

The risk was known and documented when the features shipped. This ADR
formalizes the decision: **accept it permanently for the current trust model,
or invest in per-agent auth now?**

## 2. Problem statement

Decide whether to:

1. **Accept the shared Bearer model as permanent** for the current
   single-operator deployment, with documented trigger conditions for
   revisiting.
2. **Invest in per-agent authentication now**, closing the impersonation gap
   proactively before the trust model changes.

### 2.1 Risks of the status quo

| # | Risk | Severity | Scenario |
|---|------|----------|----------|
| 1 | **Accidental cross-agent file access** | Medium | Agent A's `agent_fetch_file` sends wrong `X-Agent-Id` (stale config, copy-paste error) and fetches agent B's transfer. B's data leaks into A's context. |
| 2 | **No audit trail per agent** | Low | The facade logs "authorized request" but cannot log "agent X fetched transfer Y" because the identity is self-declared. Incident forensics are weaker. |
| 3 | **Privilege escalation if facade is exposed** | High (if exposed) | If the facade is accidentally exposed to the internet (misconfigured reverse proxy, `--host 0.0.0.0` without `--api-key`), any attacker with the shared key can read every agent's inbox and fetch every transfer. Per-agent keys would limit blast radius. |
| 4 | **Blocks multi-operator evolution** | Low | If a second operator joins (shared VPS, collaborator), the shared key model cannot enforce isolation between their agents and Vincent's. |

## 3. Options considered

### 3.1 Option A — Per-agent API keys (key-per-agent)

Each agent gets its own API key. The facade maintains a mapping
`{key_hash → agent_id}` (in a JSON config file or a SQLite table). The Bearer
token authenticates **and identifies** the agent. The `X-Agent-Id` header
becomes optional (used only for logging confirmation, not for ACL).

**Design sketch:**

- New config: `~/.a2a-facade-keys.json` — `{agent_id: {key_hash, created_at}}`.
- CLI command: `a2a-mcp-bridge facade keygen <agent_id>` — generates a key,
  prints it once, stores the hash.
- `_check_auth` returns the authenticated `agent_id` instead of just
  `True/False`. ACL endpoints use the authenticated identity, not the header.
- Backward compat: if `A2A_FACADE_API_KEY` is set and no key file exists, falls
  back to the shared-bearer model (deprecation warning logged).

**✅ Pros:**

- Closes the impersonation gap. ACL is enforced, not advisory.
- Audit trail: every request is attributable to a specific agent.
- Blast radius: compromising one agent's key doesn't compromise the fleet.
- Forward-compatible with multi-operator deployments.

**❌ Cons:**

- **Operational overhead**: key generation, distribution to 10 gateways, rotation
  policy. Each agent's `config.yaml` needs its own key env var instead of one
  shared `A2A_FACADE_API_KEY`.
- **~150-200 LOC** implementation (key store, CLI, auth refactor, tests,
  migration path, backward-compat shim).
- **No immediate benefit** for the current single-operator private-network
  deployment — the risk it mitigates (malicious impersonation) doesn't exist
  in the current trust model.
- **Key distribution to the Mac** (`vlbeau-macqwen36`) requires a secure channel
  beyond the existing SSH tunnel (the key would travel in the agent's env file).

### 3.2 Option B — HMAC-signed agent identity (webhook wake-up pattern)

Reuse the wake-up HMAC pattern from `wake.py`: each request carries a
signature computed over `(agent_id, timestamp, body)` using the agent's shared
webhook secret (already in `~/.a2a-wake-registry.json`). The facade verifies
the signature and extracts the agent_id.

**✅ Pros:**

- Reuses existing key infrastructure (wake-registry secrets already exist per
  agent).
- No new key store needed.
- Request-level authentication (signature covers the body, preventing replay
  with a different payload).

**❌ Cons:**

- **Higher per-request overhead**: HMAC computation + timestamp validation +
  replay window management on every request.
- **Client-side complexity**: every `HttpBusStore` and `_facade_download` call
  must sign the request. The stdlib `urllib.request` path in `tools.py` would
  need a signing wrapper — fragile.
- **Wake-registry secrets are gateway-side**, not agent-side. The agent process
  doesn't inherently have its webhook secret; it would need to be injected via
  env, which is the same distribution problem as Option A.
- **Mismatched purpose**: the wake-registry secret is for gateway-to-gateway
  webhook authentication, not for agent-to-facade authentication. Overloading
  it conflates two trust domains.

### 3.3 Option C — mTLS (mutual TLS client certificates)

Each agent presents a client certificate. The facade validates the certificate
and extracts the agent identity from the CN (Common Name) or SAN.

**✅ Pros:**

- Strong, standard, transport-level authentication.
- No application-level key management.
- Certificates can carry expiration, revocation, and chain-of-trust.

**❌ Cons:**

- **Massive operational overhead**: CA setup, cert generation per agent, cert
  distribution, rotation, revocation infrastructure.
- **Overkill** for a single-operator private network with 10 agents.
- **Breaks the SSH tunnel pattern**: the Mac agent connects via localhost SSH
  tunnel; mTLS would need to be terminated differently.
- **No incremental value** over Option A for the current deployment.

### 3.4 Option D — Accept shared bearer, document the risk (status quo)

Keep the current model. Document the limitation in the README and this ADR.
Define explicit trigger conditions that would motivate migration to Option A.

**✅ Pros:**

- Zero implementation cost.
- Matches the current threat model (single operator, private network,
  no malicious agents).
- Consistent with ADR-004's pragmatic approach (accept limitations when the
  fix cost exceeds the benefit for the current use case).
- The `X-Agent-Id` header remains as an advisory ACL that prevents accidental
  cross-agent access in the common case (agent sends its own ID correctly).

**❌ Cons:**

- The impersonation gap remains. If the trust model changes (multi-operator,
  internet-exposed facade), this must be revisited.
- Audit trail remains weak (self-declared identity).
- Adds a permanent item to the "known limitations" list.

## 4. Decision

**Adopt Option D (accept shared bearer, document the risk).**

### Rationale

The cost-benefit analysis mirrors ADR-004's logic:

1. **The threat is theoretical.** All 10 profiles belong to the same operator.
   No agent has incentive to impersonate another. The realistic risk
   (accidental misconfiguration) is already mitigated by the `X-Agent-Id` header
   in the common case — agents send their own ID by default.

2. **The fix cost is non-trivial.** Option A requires ~150-200 LOC, a key
   store, CLI commands, distribution of 10 keys to 10 gateways (including the
   Mac via SSH tunnel), a rotation policy, and a backward-compat shim. This is
   1-2 days of work for a security improvement that has no immediate effect on
   the current deployment.

3. **The blast radius is contained.** The facade binds to `127.0.0.1` by
   default and refuses to start on a non-localhost interface without
   `--api-key` (M3 fix, v0.11.0). The Mac agent connects via SSH tunnel. The
   facade is never internet-exposed in the current topology.

4. **Precedent.** ADR-004 accepted ADR-001 §2.1 risks #1-#7 as permanent
   under Option W, with the rationale that the fix cost (8 days of upstream
   work) was disproportionate to the benefit for a secondary problem. The same
   logic applies here: the impersonation risk is secondary (accidental, not
   malicious), and the fix cost is disproportionate to the current threat.

### Trigger conditions for migration to Option A

This decision is **not permanent**. It should be revisited when **any** of the
following conditions become true:

| # | Trigger | Why it changes the calculus |
|---|---------|-----------------------------|
| 1 | **Second operator joins** | Shared key cannot enforce isolation between operators. Per-agent keys become necessary. |
| 2 | **Facade exposed to internet** | Even behind a reverse proxy, a shared key means full compromise on leak. Per-agent keys limit blast radius. |
| 3 | **Regulatory / compliance requirement** | If audit attribution per agent becomes a requirement (e.g. for a client project), self-declared identity is insufficient. |
| 4 | **Cross-agent data leak incident** | If an accidental cross-agent file access is observed in production, the advisory ACL has failed and must be hardened. |
| 5 | **Cost of Option A drops** | If the key management infrastructure is already built for another purpose (e.g. the `capability_announce` registry grows a key store), the marginal cost of reusing it for facade auth becomes negligible. |

### What this means for the codebase

- **No code change.** The shared Bearer model in `facade.py` stays as-is.
- **README update.** The "Authentication model" section should explicitly
  cross-reference this ADR and state that the shared-key limitation is a
  **deliberate decision**, not an oversight.
- **ADR-006.1 cross-reference.** ADR-006.1 §Future work ("Per-agent ACL / API
  keys") now points to this ADR for the decision context.
- **ADR-007 §4.2 item 7.** The ACL on `GET /transfers/<id>` remains
  header-based (`X-Agent-Id`), documented as **advisory under the current
  trust model**. The `transfers.recipient_id` check prevents accidental access;
  it does not prevent deliberate impersonation by a holder of the shared key.

## 5. Consequences

### 5.1 Positive

- No implementation cost. The team can focus on features and robustness.
- Honest documentation: the limitation is now an explicit decision with trigger
  conditions, not a silent gap.
- Consistent with ADR-004's pragmatic philosophy.

### 5.2 Negative

- The impersonation gap is permanent until a trigger condition fires.
- The README and ADR-006.1 must be kept in sync with this decision.
- New contributors or operators must read this ADR to understand why the ACL
  is advisory — it is not self-evident from the code alone.

### 5.3 Open questions

- **Should the facade log a WARNING when `X-Agent-Id` is absent on ACL
  endpoints?** Leaning: **yes** — a one-line log helps diagnose accidental
  misconfiguration without adding enforcement. Low cost, high diagnostic value.
  This is a code change, not an ADR-level decision; it can ship as a minor fix.
- **Should `_check_auth` return the `X-Agent-Id` for logging even though it
  doesn't enforce it?** Leaning: **yes** — same rationale. Attributable logs
  without enforcement are still better than anonymous logs.

## 6. References

- [ADR-006.1 — HTTP Bus Facade](ADR-006.1-http-bus-facade.md) — introduced the
  shared Bearer model and flagged per-agent ACL as future work.
- [ADR-007 — File Transfer Primitive](ADR-007-file-transfer-primitive.md) —
  Phase C ACL on `/transfers/<id>` uses `X-Agent-Id` header (§4.2 item 7, §8.2).
- [ADR-004 — Session identity](ADR-004-session-identity-vs-queue.md) — precedent
  for accepting structural limitations when fix cost exceeds benefit.
- Bridge review 2026-06-19 — finding B5 ("ACL transferts Phase C basée sur
  header déclaratif").
- `src/a2a_mcp_bridge/facade.py:118-128` — `_check_auth` (shared Bearer).
- `src/a2a_mcp_bridge/facade.py:474,514` — `X-Agent-Id` header read for ACL.
