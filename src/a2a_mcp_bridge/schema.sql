CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    metadata TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    sender_id TEXT NOT NULL,
    recipient_id TEXT NOT NULL,
    body TEXT NOT NULL,
    metadata TEXT,
    created_at TEXT NOT NULL,
    read_at TEXT,
    sender_session_id TEXT,
    intent TEXT NOT NULL DEFAULT 'triage',
    FOREIGN KEY (sender_id) REFERENCES agents(id),
    FOREIGN KEY (recipient_id) REFERENCES agents(id)
);

CREATE INDEX IF NOT EXISTS idx_messages_recipient_unread
    ON messages(recipient_id, read_at);

CREATE INDEX IF NOT EXISTS idx_messages_created_at
    ON messages(created_at);

-- Capability Registry (centralized, ADR-008)
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
