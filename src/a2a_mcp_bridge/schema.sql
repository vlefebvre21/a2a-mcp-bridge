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
    FOREIGN KEY (sender_id) REFERENCES agents(id),
    FOREIGN KEY (recipient_id) REFERENCES agents(id)
);

CREATE INDEX IF NOT EXISTS idx_messages_recipient_unread
    ON messages(recipient_id, read_at);

CREATE INDEX IF NOT EXISTS idx_messages_created_at
    ON messages(created_at);
