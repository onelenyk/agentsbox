"""Task + Thread store — the entire domain model of the orchestrator system.

Two entities, no state machines:

    tasks    { id, goal, repo, status, created_at, updated_at }
    messages { id, task_id, seq, author, kind, text, attachment, created_at }

A Task is a goal plus a thread of messages. Everyone — orchestrator, workers,
humans — does exactly one thing: post a message to a task. The thread is the
single source of truth: it is the orchestrator's memory, the human channel, the
worker coordination surface, and the record of produced artifacts.

Status is a tiny enum the orchestrator sets directly (single writer → no FSM).
"""
import json
import re
from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4

from .db import get_conn

# A task's whole lifecycle. No transition table — the orchestrator owns it.
VALID_STATUSES = ("active", "waiting", "done", "failed")

_SCHEMA = """
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
    seq        INTEGER NOT NULL,          -- 1..N monotonic per task
    author     TEXT NOT NULL,             -- orchestrator | <role> | human | system
    kind       TEXT NOT NULL DEFAULT 'note',  -- note|question|answer|result|worker_started|worker_finished
    text       TEXT NOT NULL DEFAULT '',
    attachment TEXT,                       -- optional filename on disk in the task dir
    created_at TEXT NOT NULL,
    UNIQUE(task_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_messages_task ON messages(task_id, seq);
"""


def init_store(conn) -> None:
    """Create the tasks/messages schema on an existing connection (called from
    ``core.db.init_db``)."""
    conn.executescript(_SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dict(row) -> Optional[dict]:
    return dict(row) if row is not None else None


# ── Tasks ───────────────────────────────────────────────────────────────


def create_task(goal: str, repo: str = "", status: str = "active") -> dict:
    task_id = f"task-{uuid4().hex[:8]}"
    now = _now()
    conn = get_conn()
    conn.execute(
        "INSERT INTO tasks (id, goal, repo, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, goal, repo, status, now, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return _dict(row)


def get_task(task_id: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return _dict(row)


def list_tasks(status: Optional[str] = None) -> List[dict]:
    conn = get_conn()
    if status:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC", (status,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_status(task_id: str, status: str) -> None:
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status {status!r}; must be one of {VALID_STATUSES}")
    conn = get_conn()
    conn.execute(
        "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
        (status, _now(), task_id),
    )
    conn.commit()
    conn.close()


def delete_task(task_id: str) -> bool:
    """Delete a task and its whole thread. Returns True if the task existed.
    Disk (worktrees, run logs) is reclaimed by the caller via
    core.worker.cleanup_task — this only touches the database."""
    conn = get_conn()
    conn.execute("DELETE FROM messages WHERE task_id = ?", (task_id,))
    cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    existed = cur.rowcount > 0
    conn.close()
    return existed


# ── Messages (the thread) ───────────────────────────────────────────────


def post_message(
    task_id: str,
    author: str,
    kind: str = "note",
    text: str = "",
    attachment: Optional[str] = None,
) -> dict:
    """Append one message to a task's thread. ``seq`` is allocated atomically in
    a single statement under SQLite's write lock (no read-then-write race)."""
    now = _now()
    conn = get_conn()
    cur = conn.execute(
        """
        INSERT INTO messages (task_id, seq, author, kind, text, attachment, created_at)
        VALUES (?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE task_id = ?), ?, ?, ?, ?, ?)
        """,
        (task_id, task_id, author, kind, text, attachment, now),
    )
    conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (now, task_id))
    conn.commit()
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone()
    conn.close()
    return _dict(row)


def get_thread(task_id: str) -> List[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM messages WHERE task_id = ? ORDER BY seq ASC", (task_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def render_thread(task_id: str) -> str:
    """Compact transcript of a task, for feeding the orchestrator's LLM decision.

    The board *is* the orchestrator's memory — this is how it re-reads it."""
    task = get_task(task_id)
    if not task:
        return f"(unknown task {task_id})"
    lines = [f"Task: {task['goal']}", f"Status: {task['status']}", "---"]
    for m in get_thread(task_id):
        att = f"  [attachment: {m['attachment']}]" if m["attachment"] else ""
        lines.append(f"{m['seq']}. [{m['author']}/{m['kind']}] {m['text']}{att}")
    if len(lines) == 3:
        lines.append("(no messages yet)")
    return "\n".join(lines)


_URL_RE = re.compile(r"https?://\S+")


def result_view(task_id: str) -> dict:
    """A structured, actionable summary derived from the thread — for the UI's
    Result card. No new schema; just reads the messages workers already post:
    the final result summary, any commits, and the change (a forge MR/PR URL or
    a local diff patch)."""
    task = get_task(task_id)
    thread = get_thread(task_id)
    results = [m for m in thread if m["kind"] == "result"]
    commits = [m["text"] for m in thread if m["kind"] == "commit"]
    mrs = [m for m in thread if m["kind"] == "mr"]

    change = None
    if mrs:
        m = mrs[-1]
        url = _URL_RE.search(m["text"] or "")
        change = {
            "text": m["text"],
            "attachment": m["attachment"],           # local diff patch, if any
            "url": url.group(0) if url else None,     # forge MR/PR link, if any
        }

    return {
        "status": task["status"] if task else None,
        "summary": results[-1]["text"] if results else None,
        "commits": commits,
        "change": change,
    }
