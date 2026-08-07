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
    parent_id  TEXT REFERENCES tasks(id),   -- NULL = top-level; set = child (a tree)
    agent      TEXT,                          -- pinned handling agent; NULL = orchestrator dispatches
    base_branch TEXT,                         -- branch to cut the task branch from (NULL = repo default)
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
    _migrate(conn)


def _migrate(conn) -> None:
    """Additive column migrations for DBs created before a field existed.
    SQLite ADD COLUMN is a no-op-safe way to evolve the schema; each guarded by
    a column-presence check so it's idempotent."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if "parent_id" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN parent_id TEXT REFERENCES tasks(id)")
    if "agent" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN agent TEXT")
    if "base_branch" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN base_branch TEXT")
    if "title" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN title TEXT")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dict(row) -> Optional[dict]:
    return dict(row) if row is not None else None


# ── Tasks ───────────────────────────────────────────────────────────────


def task_display_title(task: dict) -> str:
    """Return the human-readable display title for a task."""
    if task.get("title"):
        return task["title"]
    goal = task.get("goal", "")
    first_line = goal.split("\n", 1)[0].strip()
    if len(first_line) > 80:
        return first_line[:80] + "…"
    return first_line


def create_task(goal: str, repo: str = "", status: str = "active",
                parent_id: Optional[str] = None, agent: Optional[str] = None,
                base_branch: Optional[str] = None,
                title: Optional[str] = None) -> dict:
    task_id = f"task-{uuid4().hex[:8]}"
    now = _now()
    conn = get_conn()
    conn.execute(
        "INSERT INTO tasks (id, goal, repo, status, parent_id, agent, base_branch, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, goal, repo, status, parent_id, agent or None, base_branch or None, title or None, now, now),
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


def list_children(parent_id: str) -> List[dict]:
    """Child tasks of a fan-out parent, oldest first. Empty for a leaf task."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM tasks WHERE parent_id = ? ORDER BY created_at ASC", (parent_id,)
    ).fetchall()
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


def find_task_by_mr(url: str) -> Optional[dict]:
    """Find the task whose thread opened a merge request at *url* — how an
    incoming forge webhook (CI, review, merge) maps back to a task."""
    if not url:
        return None
    conn = get_conn()
    row = conn.execute(
        "SELECT task_id FROM messages WHERE kind = 'mr' AND text LIKE ? "
        "ORDER BY seq DESC LIMIT 1",
        (f"%{url}%",),
    ).fetchone()
    conn.close()
    return get_task(row["task_id"]) if row else None


def task_tree(task_id: str) -> List[str]:
    """A task id plus all of its descendant task ids (fan-out children, and
    theirs). Used so deletion reclaims a whole subtree's disk/sessions, not just
    the root. Depth is capped at one level today, but this recurses regardless."""
    ids = [task_id]
    for child in list_children(task_id):
        ids.extend(task_tree(child["id"]))
    return ids


def delete_task(task_id: str) -> bool:
    """Delete a task, its whole thread, and any descendant tasks + threads
    (fan-out children cascade). Returns True if the root task existed. Disk
    (worktrees, run logs, attachments) is reclaimed per node by the caller via
    core.worker.cleanup_task — this only touches the database."""
    ids = task_tree(task_id)
    conn = get_conn()
    placeholders = ",".join("?" * len(ids))
    conn.execute(f"DELETE FROM messages WHERE task_id IN ({placeholders})", ids)
    cur = conn.execute(f"DELETE FROM tasks WHERE id IN ({placeholders})", ids)
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


def _keep_in_window(m: dict) -> bool:
    """Which older messages survive windowing: the load-bearing ones — every
    orchestrator decision/question, human reply, worker lifecycle event, and
    any worker outcome/artifact (result / commit / mr / attachment). Only a
    worker's routine progress notes are collapsible."""
    if m["author"] in ("orchestrator", "human", "system"):
        return True
    if m["kind"] in ("result", "commit", "mr"):
        return True
    return bool(m["attachment"])


def render_thread(task_id: str, for_prompt: bool = False,
                  max_messages: int = 40, tail: int = 20) -> str:
    """Compact transcript of a task.

    Default (for_prompt=False): the full thread — used for the UI/API and human
    reading. The board *is* the orchestrator's memory — this is how it re-reads it.

    for_prompt=True bounds the tokens on a long task: once past *max_messages*,
    the older head is windowed — every decision, question, human reply, and
    worker outcome kept verbatim; routine progress notes collapsed to a count —
    and the last *tail* messages are always kept verbatim. Deterministic, no
    extra model call."""
    task = get_task(task_id)
    if not task:
        return f"(unknown task {task_id})"

    def _line(m):
        att = f"  [attachment: {m['attachment']}]" if m["attachment"] else ""
        return f"{m['seq']}. [{m['author']}/{m['kind']}] {m['text']}{att}"

    # The FULL goal, never the display title: this header is what the
    # orchestrator (and any thread reader) sees as the task statement.
    header = [f"Task: {task['goal']}", f"Status: {task['status']}", "---"]
    msgs = get_thread(task_id)

    if not (for_prompt and len(msgs) > max_messages):
        lines = header + [_line(m) for m in msgs]
        if not msgs:
            lines.append("(no messages yet)")
        return "\n".join(lines)

    head, recent = msgs[:-tail], msgs[-tail:]
    out = list(header)
    omitted = 0
    for m in head:
        if _keep_in_window(m):
            if omitted:
                out.append(f"… ({omitted} routine progress notes omitted) …")
                omitted = 0
            out.append(_line(m))
        else:
            omitted += 1
    if omitted:
        out.append(f"… ({omitted} routine progress notes omitted) …")
    out.append("--- recent ---")
    out += [_line(m) for m in recent]
    return "\n".join(out)


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
