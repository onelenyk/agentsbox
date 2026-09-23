"""Statistics — activity analytics from tasks + messages, plus token/cost usage.

All derived from data the system already records; the only instrumented part is
token/cost (core.usage). No estimation.
"""
from datetime import datetime
from typing import Optional
import logging

logger = logging.getLogger(__name__)

from . import usage as _usage
from .db import get_conn


_LEADS = ("[done]", "[spawn_worker]", "APPROVED: APPROVE.", "APPROVED: APPROVE", "APPROVED:", "CHANGES REQUESTED:")


def _headline(text: str) -> str:
    """One line a person can read in a feed: the first line, without the
    machine prefixes ("[done]", "APPROVED: APPROVE.") the verdict parser needs."""
    line = next((l.strip() for l in (text or "").splitlines() if l.strip()), "")
    for lead in _LEADS:
        if line.startswith(lead):
            line = line[len(lead):].strip()
            break
    return line


def dashboard() -> dict:
    """Live operational snapshot for the Mission Control dashboard: the KPI/usage
    overview plus what's active right now, the queue, and recent activity."""
    ov = overview()
    conn = get_conn()

    active = []
    for t in conn.execute("SELECT * FROM tasks WHERE status = 'active' ORDER BY updated_at DESC LIMIT 8"):
        last = conn.execute(
            "SELECT author, kind FROM messages WHERE task_id = ? ORDER BY seq DESC LIMIT 1",
            (t["id"],)).fetchone()
        running = bool(last and last["kind"] == "worker_started")
        active.append({
            "id": t["id"], "goal": t["goal"], "repo": t["repo"], "agent": t["agent"],
            "running": running,
            "role": (last["author"] if running else None) or (t["agent"] or "orchestrator"),
            "state": "running" if running else "deciding",
        })

    waiting = [dict(r) for r in conn.execute(
        "SELECT id, goal, repo FROM tasks WHERE status = 'waiting' ORDER BY updated_at DESC LIMIT 6")]

    # The daemon's own bookkeeping ("reviewer finished") is noise in a feed a
    # person reads; what agents and people said is the news.
    recent = [dict(r) for r in conn.execute(
        "SELECT m.task_id, m.author, m.kind, m.text, m.created_at, t.key AS task_key "
        "FROM messages m LEFT JOIN tasks t ON t.id = m.task_id WHERE m.author != 'system' "
        "ORDER BY m.id DESC LIMIT 200")]
    # One row per task — its latest word. Three rows of one task's chatter
    # pushed everything else off the feed.
    seen, feed = set(), []
    for m in recent:
        if m["task_id"] in seen:
            continue
        seen.add(m["task_id"])
        m["text"] = _headline(m["text"])
        feed.append(m)
        if len(feed) == 10:
            break
    recent = feed

    conn.close()
    return {"ov": ov, "active": active, "waiting": waiting, "recent": recent}


def _seconds(a: str, b: str) -> Optional[float]:
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
    except Exception as e:
        logger.debug("_seconds: datetime parse failed: %s", e)
        return None


def overview() -> dict:
    conn = get_conn()
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]

    by_status = {r["status"]: r["n"]
                 for r in conn.execute("SELECT status, COUNT(*) n FROM tasks GROUP BY status")}
    total = sum(by_status.values())
    done = by_status.get("done", 0)
    failed = by_status.get("failed", 0)
    terminal = done + failed

    # avg wall-clock time to reach done (created_at → updated_at)
    done_rows = conn.execute(
        "SELECT created_at, updated_at FROM tasks WHERE status = 'done'").fetchall()
    durs = [s for r in done_rows if (s := _seconds(r["created_at"], r["updated_at"])) is not None]
    avg_done_s = round(sum(durs) / len(durs)) if durs else 0

    activity = {
        "worker_runs":         q("SELECT COUNT(*) FROM messages WHERE kind = 'worker_started'"),
        "orchestrator_passes": q("SELECT COUNT(*) FROM messages WHERE author = 'orchestrator'"),
        "commits":             q("SELECT COUNT(*) FROM messages WHERE kind = 'commit'"),
        "merge_requests":      q("SELECT COUNT(*) FROM messages WHERE kind = 'mr'"),
        "approvals":           q("SELECT COUNT(*) FROM messages WHERE kind = 'result' AND text LIKE 'APPROVED%'"),
        "changes_requested":   q("SELECT COUNT(*) FROM messages WHERE kind = 'result' AND text LIKE 'CHANGES REQUESTED%'"),
        "human_replies":       q("SELECT COUNT(*) FROM messages WHERE author = 'human'"),
    }

    per_repo = [dict(r) for r in conn.execute(
        "SELECT COALESCE(NULLIF(repo,''),'(none)') repo, COUNT(*) n "
        "FROM tasks WHERE parent_id IS NULL GROUP BY repo ORDER BY n DESC LIMIT 8")]

    created_by_day = [dict(r) for r in conn.execute(
        "SELECT substr(created_at,1,10) day, COUNT(*) n FROM tasks "
        "GROUP BY day ORDER BY day DESC LIMIT 14")][::-1]

    fanout = {
        "parents":  q("SELECT COUNT(DISTINCT parent_id) FROM tasks WHERE parent_id IS NOT NULL"),
        "children": q("SELECT COUNT(*) FROM tasks WHERE parent_id IS NOT NULL"),
    }
    conn.close()

    return {
        "tasks": {
            "total": total, "by_status": by_status,
            "done": done, "failed": failed,
            "completion_rate": round(100 * done / terminal) if terminal else None,
            "avg_seconds_to_done": avg_done_s,
        },
        "activity": activity,
        "per_repo": per_repo,
        "created_by_day": created_by_day,
        "fanout": fanout,
        "usage": {
            "totals": _usage.totals(),
            "by_agent": _usage.by_agent(),
            "by_day": _usage.by_day(),
        },
    }
