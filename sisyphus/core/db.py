import sqlite3
import os
import shutil
import tempfile

DB_PATH = os.environ.get("SISYPHUS_DB", os.path.join(os.environ.get("WORKSPACE_ROOT", "/workspace"), "sisyphus.db"))

def _connect_readonly(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)

def _probe(conn: sqlite3.Connection) -> None:
    """Force the WAL -shm to be created now, so its failure surfaces here.

    A mode=ro open of a WAL database defers the -shm creation until the first
    read. In a directory the process cannot write to, connect() itself succeeds
    and the *first query* raises OperationalError — the failure the sandbox
    reports from goal_report.py's data functions. Running a trivial read right
    away moves that failure to this call, where the fallback can catch it.
    """
    conn.execute("SELECT count(*) FROM sqlite_master")

def get_readonly_conn() -> sqlite3.Connection:
    """Return a read-only SQLite connection (URI mode).

    The daemon's database runs in WAL mode, and even a mode=ro open must
    create the -shm file beside it, so it fails in any directory the process
    may not write to — every worker sandbox, which is where reviewers run the
    report. Fall back to a snapshot: the DB file plus its -wal (-shm if
    present) are copied into a fresh temp dir and the copy is opened mode=ro,
    so the WAL contents stay visible. The live DB is never opened, writable or
    otherwise — only read as bytes. The snapshot is as of the moment of the
    copy, so it may be slightly stale.
    """
    try:
        conn = _connect_readonly(DB_PATH)
        _probe(conn)
    except sqlite3.OperationalError:
        try:
            conn.close()
        except Exception:
            pass
        conn = _connect_readonly(_snapshot_copy())
    conn.row_factory = sqlite3.Row
    return conn

def _snapshot_copy() -> str:
    """Copy DB_PATH (+ -wal, + -shm) into a temp dir and return the copy's path.

    A plain file copy, taken while the daemon may be writing — the snapshot is
    read-only, so it cannot hurt the live database. Temp dirs stay writable
    where the DB's own directory is not (the sandbox denies writes only to the
    protected repo and the daemon state paths).
    """
    if not os.path.exists(DB_PATH):
        raise sqlite3.OperationalError(f"unable to open database file: {DB_PATH}")
    tmp = tempfile.mkdtemp(prefix="sisyphus-readonly-")
    dst = os.path.join(tmp, os.path.basename(DB_PATH))
    shutil.copyfile(DB_PATH, dst)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(DB_PATH + suffix):
            shutil.copyfile(DB_PATH + suffix, dst + suffix)
    return dst

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
