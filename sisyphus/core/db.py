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
    from .store import init_store
    init_store(conn)
    conn.commit()
    conn.close()
