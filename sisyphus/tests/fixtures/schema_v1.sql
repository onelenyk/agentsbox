CREATE TABLE IF NOT EXISTS tasks (
    id         TEXT PRIMARY KEY,
    goal       TEXT NOT NULL,
    repo       TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL REFERENCES tasks(id),
    seq        INTEGER NOT NULL,
    author     TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'note',
    text       TEXT NOT NULL DEFAULT '',
    attachment TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(task_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_messages_task ON messages(task_id, seq);
CREATE TABLE IF NOT EXISTS merge_requests (
    id            TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL,
    repo          TEXT NOT NULL DEFAULT '',
    source_branch TEXT NOT NULL,
    target_branch TEXT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'open',
    bare_path     TEXT NOT NULL DEFAULT '',
    base_branch   TEXT NOT NULL DEFAULT '',
    source_path   TEXT NOT NULL DEFAULT '',
    merge_sha     TEXT,
    merged_at     TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mr_task ON merge_requests(task_id);
