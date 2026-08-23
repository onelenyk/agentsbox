import sqlite3
import os

DB_PATH = os.environ.get("SISYPHUS_DB", os.path.join(os.environ.get("WORKSPACE_ROOT", "/workspace"), "sisyphus.db"))

def get_readonly_conn() -> sqlite3.Connection:
    """Return a read-only SQLite connection (URI mode)."""
    uri = f"file:{DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn

def _write_protected() -> bool:
    """Whether THIS process may write the database it is pointed at.

    Two guards, an accident fence in both cases (Seatbelt cannot cover the DB):
      SISYPHUS_DB_READONLY=1   blanket — every connection is read-only.
      SISYPHUS_DB_PROTECT=<p>  path-scoped — read-only only when DB_PATH IS
                               that file. This is what workers and checks get:
                               it keeps them off the daemon's database while a
                               test suite they run stays free to write its own
                               per-test tmp databases (the blanket flag made
                               every such suite fail with ~540 sqlite errors,
                               which each engineer then had to re-diagnose).
    """
    if os.environ.get("SISYPHUS_DB_READONLY") == "1":
        return True
    protect = (os.environ.get("SISYPHUS_DB_PROTECT") or "").strip()
    if not protect:
        return False
    try:
        return os.path.realpath(DB_PATH) == os.path.realpath(protect)
    except OSError:
        return True   # cannot tell — protect

def get_conn():
    if _write_protected():
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
    if _write_protected():
        return  # write-protected — skip DB initialization
    conn = get_conn()
    from .store import init_store
    init_store(conn)
    conn.commit()
    conn.close()
