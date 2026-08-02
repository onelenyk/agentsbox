"""Persistent Board Copilot chat — sessions + messages in SQLite, so history
survives reloads and the dock reopens on the latest conversation.
"""
from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4

from core.db import get_conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dict(row) -> Optional[dict]:
    return dict(row) if row is not None else None


def init_copilot_tables() -> None:
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS copilot_sessions (
            id         TEXT PRIMARY KEY,
            title      TEXT NOT NULL DEFAULT 'New chat',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS copilot_messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role       TEXT NOT NULL,
            text       TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cpmsg_session ON copilot_messages(session_id)")
    conn.commit()
    conn.close()


def create_session(title: str = "New chat") -> dict:
    sid = f"cs-{uuid4().hex[:8]}"
    now = _now()
    conn = get_conn()
    conn.execute("INSERT INTO copilot_sessions (id, title, created_at, updated_at) VALUES (?,?,?,?)",
                 (sid, title, now, now))
    conn.commit()
    conn.close()
    return get_session(sid)


def get_session(sid: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM copilot_sessions WHERE id = ?", (sid,)).fetchone()
    conn.close()
    return _dict(row)


def latest_session() -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM copilot_sessions ORDER BY updated_at DESC LIMIT 1").fetchone()
    conn.close()
    return _dict(row)


def latest_or_create() -> dict:
    return latest_session() or create_session()


def list_sessions(limit: int = 30) -> List[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM copilot_sessions ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_messages(sid: str) -> List[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM copilot_messages WHERE session_id = ? ORDER BY id ASC", (sid,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_message(sid: str, role: str, text: str) -> dict:
    now = _now()
    conn = get_conn()
    cur = conn.execute("INSERT INTO copilot_messages (session_id, role, text, created_at) VALUES (?,?,?,?)",
                       (sid, role, text, now))
    conn.execute("UPDATE copilot_sessions SET updated_at = ? WHERE id = ?", (now, sid))
    # title the session from its first user message
    if role == "user":
        row = conn.execute("SELECT title FROM copilot_sessions WHERE id = ?", (sid,)).fetchone()
        if row and row["title"] in ("New chat", ""):
            conn.execute("UPDATE copilot_sessions SET title = ? WHERE id = ?",
                         (text.strip()[:40] or "New chat", sid))
    conn.commit()
    row = conn.execute("SELECT * FROM copilot_messages WHERE id = ?", (cur.lastrowid,)).fetchone()
    conn.close()
    return _dict(row)


def delete_session(sid: str) -> bool:
    conn = get_conn()
    conn.execute("DELETE FROM copilot_messages WHERE session_id = ?", (sid,))
    cur = conn.execute("DELETE FROM copilot_sessions WHERE id = ?", (sid,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0
