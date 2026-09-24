"""The journal of everything Sisyphus does against an outside system.

Every call to a tracker or a forge — Jira today, GitLab next — goes through a
client in this package, and every call leaves one row here: who acted (an
agent on a task, the daemon's own loop, or a human in the UI), what was done,
against what, whether it worked. The Integrations page is a view of this
table and nothing else, so what it shows is what happened, and a provider
added later needs no new page — only rows with its name.

Actors are strings in a fixed shape, so the page can group and link them:

  agent:<role>@<task_id>   a worker's tool call (engineer@task-1a2b3c4d)
  daemon:<loop>            a background loop (daemon:jira-poll)
  human:ui                 a button in the UI
  human:cli                sisyphus.py
"""
import json
from datetime import datetime, timezone
from typing import List, Optional

from .db import get_conn

PROVIDERS = ("jira", "gitlab", "github")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_integrations_table() -> None:
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS integration_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            provider    TEXT NOT NULL,             -- one of PROVIDERS
            action      TEXT NOT NULL,             -- search | get | create_tasks | transition | comment | myself | …
            actor       TEXT NOT NULL,             -- see module docstring
            subject     TEXT NOT NULL DEFAULT '',  -- the issue key / JQL / PR the action was about
            detail      TEXT NOT NULL DEFAULT '',  -- one human sentence: "fetched 50 issues"
            count       INTEGER NOT NULL DEFAULT 0,-- how many things: issues fetched, tasks created
            ok          INTEGER NOT NULL DEFAULT 1,
            error       TEXT,                      -- the provider's own words when ok = 0
            task_id     TEXT,                      -- the Sisyphus task involved, if any
            meta        TEXT NOT NULL DEFAULT '{}',-- json: anything a view may want later
            created_at  TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_integration_events_provider "
                 "ON integration_events(provider, created_at)")
    conn.commit()
    conn.close()


def record(provider: str, action: str, actor: str, *, subject: str = "", detail: str = "",
           count: int = 0, ok: bool = True, error: Optional[str] = None,
           task_id: Optional[str] = None, meta: Optional[dict] = None) -> int:
    """Write one event and return its id. Never raises for a bad row shape —
    a journal entry must not fail the action it describes."""
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO integration_events (provider, action, actor, subject, detail, count,
           ok, error, task_id, meta, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (provider, action, actor, subject or "", detail or "", int(count or 0), 1 if ok else 0,
         (error or None), task_id, json.dumps(meta or {}), _now()))
    conn.commit()
    event_id = cur.lastrowid
    conn.close()
    return event_id


def list_events(provider: Optional[str] = None, limit: int = 100,
                only_errors: bool = False) -> List[dict]:
    """Newest first."""
    where, args = [], []
    if provider:
        where.append("provider = ?"); args.append(provider)
    if only_errors:
        where.append("ok = 0")
    sql = "SELECT * FROM integration_events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    conn = get_conn()
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["meta"] = json.loads(d.get("meta") or "{}")
        except ValueError:
            d["meta"] = {}
        out.append(d)
    return out


def summary(provider: str, days: int = 1) -> dict:
    """Counts for the page's header: calls, failures, and the sum of `count`
    per action over the last *days*, plus the last event of all."""
    from datetime import timedelta
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = get_conn()
    rows = conn.execute(
        """SELECT action, COUNT(*) calls, SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) failed,
                  COALESCE(SUM(count), 0) things
           FROM integration_events WHERE provider = ? AND created_at >= ?
           GROUP BY action ORDER BY calls DESC""", (provider, since)).fetchall()
    last = conn.execute("SELECT * FROM integration_events WHERE provider = ? ORDER BY id DESC LIMIT 1",
                        (provider,)).fetchone()
    conn.close()
    return {"provider": provider, "days": days,
            "actions": [dict(r) for r in rows],
            "calls": sum(r["calls"] for r in rows),
            "failed": sum(r["failed"] for r in rows),
            "last": dict(last) if last else None}
