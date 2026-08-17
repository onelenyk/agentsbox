"""Reading a task's run logs — the record of what a worker's CLI actually did.

core.launcher writes every run's stdout and stderr to
``runs/<task>/logs/<task>-<role>.log``. Those files answered essentially every
question during the runner debugging of 2026-08-15..17 — a hung CLI, a wrapper
script that could not find its binary, an agent that ran and never reported —
and every one of those answers required someone at a terminal. Nothing in the
app could read them.

The lookup is deliberately narrow. A log name arrives from a URL, so this
module never joins caller input onto a path: it lists the directory itself and
matches against what is really there.
"""
import os
from pathlib import Path
from typing import List, Optional

# Enough to see how a run ended without shipping a 10MB buffer to a browser.
TAIL_BYTES = 200_000


def _logs_dir(task_id: str) -> Path:
    ws = os.environ.get("WORKSPACE_ROOT", "/workspace")
    return Path(ws) / "runs" / task_id / "logs"


def list_logs(task_id: str) -> List[dict]:
    """Every log for *task_id*: {name, role, size, modified}, newest first.

    *role* is the readable part — the file is named ``<task>-<role>.log`` — so
    the UI can label a log "reviewer" rather than repeating the task id.
    """
    d = _logs_dir(task_id)
    if not d.is_dir():
        return []
    out = []
    for p in d.iterdir():
        if not p.is_file() or p.suffix != ".log":
            continue
        stem = p.stem
        role = stem[len(task_id) + 1:] if stem.startswith(f"{task_id}-") else stem
        try:
            st = p.stat()
        except OSError:
            continue
        out.append({"name": p.name, "role": role or stem,
                    "size": st.st_size, "modified": st.st_mtime})
    out.sort(key=lambda r: r["modified"], reverse=True)
    return out


def read_log(task_id: str, name: str) -> Optional[str]:
    """The tail of one log, or None if there is no such log for this task.

    *name* is matched against the directory listing rather than joined onto the
    path — a name arriving from a URL must never be able to address a file
    outside this task's own log directory.
    """
    if name not in {r["name"] for r in list_logs(task_id)}:
        return None
    p = _logs_dir(task_id) / name
    try:
        size = p.stat().st_size
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            if size > TAIL_BYTES:
                fh.seek(size - TAIL_BYTES)
                # The seek lands mid-line; drop the fragment so the view starts clean.
                fh.readline()
                return f"… truncated, showing the last {TAIL_BYTES // 1000}KB …\n" + fh.read()
            return fh.read()
    except OSError:
        return None
