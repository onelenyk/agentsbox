"""The runner's own session store — kept to a size, because nothing else does.

Every worker run is one non-interactive CLI session (`opencode run …`), and
opencode records each one — its messages, every tool result, and an event log —
in a SQLite database under the worker HOME (core.runner_home). Sisyphus never
continues a session: continuity comes from the task thread, re-rendered into
each new run (see core/orchestrator.py). So every one of those sessions is
write-only from the day it ends.

Nothing pruned them. Measured on 2026-09-22, five months in: 3262 sessions,
80588 messages, 518031 events — a 6.6 GB file, of which the event log alone was
5.3 GB, for work whose record already lives in the task threads.

This module is the pruner. It deletes sessions older than a cutoff and reclaims
the file; the schema's own ON DELETE CASCADE takes their messages, parts, todos
and events with them. It never touches credentials (auth.json is a separate
file) or any session still in use.
"""
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_KEEP_DAYS = 7
DEFAULT_INTERVAL_SECONDS = 86400
_CHUNK = 200                      # sessions per transaction


def store_path(root: Optional[str] = None) -> Path:
    """The worker HOME's opencode database. The user's own store, under their
    real HOME, is theirs and is never touched."""
    ws = root or os.environ.get("WORKSPACE_ROOT", "/workspace")
    return Path(ws) / "runner-home" / ".local" / "share" / "opencode" / "opencode.db"


def _size(path: Path) -> int:
    total = 0
    for p in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


def stats(root: Optional[str] = None) -> dict:
    """What the store holds right now: {path, exists, bytes, sessions, oldest}."""
    path = store_path(root)
    out = {"path": str(path), "exists": path.is_file(), "bytes": _size(path),
           "sessions": 0, "oldest": None}
    if not out["exists"]:
        return out
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            out["sessions"] = conn.execute("SELECT COUNT(*) FROM session").fetchone()[0]
            oldest = conn.execute("SELECT MIN(time_created) FROM session").fetchone()[0]
            out["oldest"] = int(oldest) // 1000 if oldest else None
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.debug("could not read the runner store: %s", exc)
    return out


def prune(keep_days: int = DEFAULT_KEEP_DAYS, root: Optional[str] = None,
          vacuum: bool = True) -> dict:
    """Delete sessions last touched more than *keep_days* ago and reclaim the
    space. Returns what happened; never raises.

    Skipped entirely while a worker is running: its session is being written to,
    and a VACUUM would block it. The next tick catches up.
    """
    path = store_path(root)
    result = {"path": str(path), "removed": 0, "kept": 0, "before": _size(path),
              "after": _size(path), "freed": 0, "skipped": ""}
    if not path.is_file():
        result["skipped"] = "no store yet"
        return result
    try:
        from . import sessions
        if sessions.list_active():
            result["skipped"] = "a worker is running"
            return result
    except Exception:  # noqa: BLE001 — pruning must not depend on the session registry
        logger.debug("could not read the live worker list", exc_info=True)

    cutoff_ms = int((time.time() - max(keep_days, 0) * 86400) * 1000)
    started = time.monotonic()
    try:
        conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA foreign_keys = ON")
            old = [r[0] for r in conn.execute(
                "SELECT id FROM session WHERE time_updated < ?", (cutoff_ms,)).fetchall()]
            for i in range(0, len(old), _CHUNK):
                chunk = old[i:i + _CHUNK]
                marks = ",".join("?" * len(chunk))
                conn.execute("BEGIN")
                # The event log hangs off event_sequence, not off session, so it
                # needs saying out loud — and it is the biggest thing here.
                conn.execute(f"DELETE FROM event_sequence WHERE aggregate_id IN ({marks})", chunk)
                conn.execute(f"DELETE FROM session WHERE id IN ({marks})", chunk)
                conn.execute("COMMIT")
                result["removed"] += len(chunk)
            result["kept"] = conn.execute("SELECT COUNT(*) FROM session").fetchone()[0]
            if vacuum and result["removed"]:
                # Deleting rows only frees pages inside the file; the 6.6 GB on
                # disk goes away here.
                conn.execute("VACUUM")
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("pruning the runner store failed: %s", exc)
        result["skipped"] = str(exc)
    result["after"] = _size(path)
    result["freed"] = max(result["before"] - result["after"], 0)
    result["seconds"] = round(time.monotonic() - started, 1)
    return result


async def prune_forever() -> None:
    """Daemon loop: prune at boot, then once a day. Off when
    runner_store.keep_days is 0."""
    import asyncio

    from . import heartbeat
    from .config import get_config

    keep = int(get_config("runner_store.keep_days", DEFAULT_KEEP_DAYS) or 0)
    interval = int(get_config("runner_store.interval_seconds", DEFAULT_INTERVAL_SECONDS) or 0)
    heartbeat.register("runner-store", every=interval, note="pruning old runner sessions")
    if keep <= 0 or interval <= 0:
        heartbeat.mark_off("runner-store", "runner_store.keep_days is 0 — pruning off")
        return
    while True:
        try:
            res = await asyncio.to_thread(prune, keep)
            if res.get("skipped"):
                heartbeat.beat("runner-store", note=f"skipped: {res['skipped']}")
            else:
                heartbeat.beat("runner-store",
                               note=f"removed {res['removed']} session(s), freed {human_size(res['freed'])}")
        except Exception:  # noqa: BLE001
            logger.exception("runner-store: pass failed")
        await asyncio.sleep(interval)


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"
