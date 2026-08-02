"""SQLite snapshot backups — consistent copies via ``VACUUM INTO``, plus restore.

Each snapshot is one self-contained ``.db`` file under ``{workspace}/backups``.
Safe to run while the daemon is live: ``VACUUM INTO`` takes a consistent snapshot
inside a read transaction, so it never captures a torn WAL state (unlike a plain
``cp`` of the -wal/-shm split). Restore first takes a safety snapshot of the
current DB, so a restore is itself reversible.
"""
import logging
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

import core.db
from core.db import get_conn

logger = logging.getLogger(__name__)

RETENTION = 14  # keep the most recent N snapshots; older ones are pruned


def _db_path() -> str:
    # Read dynamically (not captured at import) so it honours a DB_PATH that the
    # daemon — or a test fixture — sets after this module is imported.
    return core.db.DB_PATH


def _backup_dir() -> Path:
    d = Path(_db_path()).parent / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _info(path: Path) -> dict:
    st = path.stat()
    label = ""
    stem = path.stem  # sisyphus-YYYYMMDD-HHMMSS[-label]
    parts = stem.split("-", 3)
    if len(parts) == 4:
        label = parts[3]
    return {
        "name": path.name,
        "size": st.st_size,
        "size_h": _human(st.st_size),
        "created_at": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        "label": label,
    }


def _integrity_ok(path: Path) -> bool:
    c = sqlite3.connect(str(path))
    try:
        return c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        c.close()


def _safe_name(name: str) -> bool:
    return name.startswith("sisyphus-") and name.endswith(".db") and "/" not in name and ".." not in name


def snapshot(label: str = "", rotate: bool = True) -> dict:
    """Create one consistent snapshot. Returns its info dict. Raises on a failed
    integrity check (and removes the bad file)."""
    d = _backup_dir()
    suffix = f"-{label}" if label else ""
    base = f"sisyphus-{_ts()}"
    path = d / f"{base}{suffix}.db"
    n = 2
    while path.exists():  # more than one in the same second — keep names unique
        path = d / f"{base}_{n}{suffix}.db"
        n += 1
    conn = get_conn()
    try:
        conn.execute("VACUUM INTO ?", (str(path),))
    finally:
        conn.close()
    if not _integrity_ok(path):
        path.unlink(missing_ok=True)
        raise RuntimeError("snapshot failed its integrity check")
    if rotate:
        _rotate(d)
    logger.info("backup: created %s (%s)", path.name, _human(path.stat().st_size))
    return _info(path)


def list_backups() -> list:
    return sorted(
        (_info(p) for p in _backup_dir().glob("sisyphus-*.db")),
        key=lambda x: x["name"],
        reverse=True,
    )


def _rotate(d: Path) -> None:
    files = sorted(d.glob("sisyphus-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files[RETENTION:]:
        p.unlink(missing_ok=True)


def _checkpoint() -> None:
    try:
        c = get_conn()
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.close()
    except Exception:
        pass


def restore(name: str) -> dict:
    """Replace the live DB with the chosen snapshot. Takes a safety snapshot of
    the current DB first (so this is reversible), verifies the chosen snapshot,
    then swaps the file and clears stale WAL side-files."""
    if not _safe_name(name):
        raise ValueError("invalid backup name")
    src = _backup_dir() / name
    if not src.exists():
        raise FileNotFoundError(name)
    if not _integrity_ok(src):
        raise RuntimeError("selected backup is corrupt — not restoring")

    # Hold the chosen snapshot aside first, so rotation during the safety
    # snapshot can never delete the file we are about to restore from.
    staged = _backup_dir() / f".restoring-{_ts()}.db"
    shutil.copyfile(src, staged)
    try:
        safety = snapshot(label="pre-restore")
        _checkpoint()
        db = Path(_db_path())
        shutil.copyfile(staged, db)
        for ext in ("-wal", "-shm"):
            p = Path(str(db) + ext)
            if p.exists():
                p.unlink()
    finally:
        staged.unlink(missing_ok=True)
    logger.warning("backup: restored DB from %s (safety copy: %s)", name, safety["name"])
    return {"restored": name, "safety": safety["name"]}


def delete_backup(name: str) -> bool:
    if not _safe_name(name):
        return False
    p = _backup_dir() / name
    if p.exists():
        p.unlink()
        logger.info("backup: deleted %s", name)
        return True
    return False
