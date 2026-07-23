import sqlite3
import os

DB_PATH = os.environ.get("SISYPHUS_DB", os.path.join(os.environ.get("WORKSPACE_ROOT", "/workspace"), "sisyphus.db"))

def get_readonly_conn() -> sqlite3.Connection:
    """Return a read-only SQLite connection (URI mode)."""
    uri = f"file:{DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn

def get_conn():
    if os.environ.get("SISYPHUS_DB_READONLY") == "1":
        return get_readonly_conn()
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn

def init_db():
    if os.environ.get("SISYPHUS_DB_READONLY") == "1":
        return  # Read-only mode — skip DB initialization
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS cards (
            id TEXT PRIMARY KEY, pipeline_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'created',
            name TEXT DEFAULT '',
            repo_name TEXT DEFAULT '',
            description TEXT, jira_url TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, card_id TEXT NOT NULL REFERENCES cards(id),
            status TEXT NOT NULL DEFAULT 'running', loop_count INTEGER DEFAULT 0,
            started_at TEXT NOT NULL, finished_at TEXT, error TEXT
        );
        CREATE TABLE IF NOT EXISTS steps (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
            agent_name TEXT NOT NULL, step_order INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', loop_count INTEGER DEFAULT 0,
            started_at TEXT, completed_at TEXT, result_json TEXT, error TEXT
        );
        CREATE TABLE IF NOT EXISTS resources (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
            step_id TEXT NOT NULL REFERENCES steps(id), filename TEXT NOT NULL,
            resource_type TEXT NOT NULL DEFAULT 'agent_output', step_order INTEGER DEFAULT 0, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS activities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id TEXT NOT NULL REFERENCES cards(id),
            run_id TEXT REFERENCES runs(id),
            step_id TEXT REFERENCES steps(id),
            author TEXT NOT NULL DEFAULT '',
            author_type TEXT NOT NULL DEFAULT 'agent' CHECK(author_type IN ('agent','human')),
            text TEXT NOT NULL,
            resources TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_activities_card ON activities(card_id, created_at);
    """)
    try:
        conn.execute("ALTER TABLE cards ADD COLUMN repo_name TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        conn.execute("ALTER TABLE cards ADD COLUMN name TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        conn.execute("ALTER TABLE runs ADD COLUMN pid INTEGER")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.commit()
    conn.close()
