---
status: Accepted (2026-05-14)
author: VLBeauBot
related-to:
  - Issue #53
  - PR #50 (v0.8.0, Capability Registry initial)
---

# ADR-008: Centralization of Capability Registry into Shared Bus SQLite

## Context

PR #50 (merged 2026-05-05, shipped in v0.8.0) delivered the Capability Registry feature, but with a design flaw: each bridge instance creates its own SQLite file at `{db_path}.registry.db`, resulting in isolated per-instance registries. `capability_find_best` operates purely locally and cannot discover skills registered on other bridges.

## Decision

Option B: Migrate the capability registry from `{db_path}.registry.db` into the shared `a2a-bus.sqlite` database.

### Schema Design

New table `capabilities` in `a2a-bus.sqlite`:
```sql
CREATE TABLE IF NOT EXISTS capabilities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL CHECK(length(agent_id) >= 1),
    skill_id TEXT NOT NULL,
    domain TEXT DEFAULT 'general',
    description TEXT,
    monetary_cost_usd FLOAT CHECK(monetary_cost_usd IS NULL OR monetary_cost_usd >= 0),
    tokens_per_call INTEGER DEFAULT 0,
    announced_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(agent_id, skill_id)
);

CREATE INDEX IF NOT EXISTS idx_capabilities_skill ON capabilities(skill_id);
CREATE INDEX IF NOT EXISTS idx_capabilities_agent ON capabilities(agent_id);
```

Key choices: native columns (not JSON blob), UNIQUE(agent_id, skill_id), nullable monetary_cost_usd.

## Consequences

Positive: fleet-wide consistency, simplified operations (1 DB to backup), atomic updates.
Negative: single point of contention (mitigated by WAL mode), schema coupling with bus DB.
Operational: auto-migrate legacy .registry.db at first boot, version bump to 0.9.0.

## Alternatives Considered

Option A (status quo): rejected — violates centralized registry requirement.
Option C (dedicated microservice): rejected — overkill for <10K capabilities.

## References

- Issue #53
- PR #50 (v0.8.0)
- ADR-001 (shared bus DB rationale)
