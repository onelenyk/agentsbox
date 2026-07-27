"""Durable per-task attachment store.

Files a worker attaches (and the local-diff patch) are copied here at call
time — under ``{workspace}/tasks/<task_id>/attachments/`` — so they survive the
task-terminal worktree cleanup and are servable to the UI. Removed only when a
task is deleted (see core.worker.cleanup_task deep=True).
"""
import os
import shutil
from pathlib import Path
from typing import Optional


def _dir(task_id: str) -> Path:
    ws = os.environ.get("WORKSPACE_ROOT", "/workspace")
    return Path(ws) / "tasks" / task_id / "attachments"


def _is_bare(name: str) -> bool:
    return bool(name) and "/" not in name and "\\" not in name and not name.startswith(".")


def save_from_worktree(task_id: str, worktree: str, filename: str) -> bool:
    """Copy *filename* out of the worker's worktree into the task's store.
    Returns False if the name is unsafe or the file isn't there."""
    if not _is_bare(filename) or not worktree:
        return False
    src = Path(worktree) / filename
    if not src.is_file():
        return False
    d = _dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, d / filename)
    return True


def save_text(task_id: str, filename: str, text: str) -> str:
    """Write *text* as an attachment (used for the generated diff patch)."""
    d = _dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    p = d / filename
    p.write_text(text, encoding="utf-8")
    return str(p)


def read(task_id: str, filename: str) -> Optional[str]:
    """Return an attachment's text, or None if the name is unsafe/missing."""
    if not _is_bare(filename):
        return None
    p = _dir(task_id) / filename
    return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None
