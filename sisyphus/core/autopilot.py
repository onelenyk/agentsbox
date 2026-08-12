"""Autopilot — named autonomous automations that fire on a schedule (cron or
interval) or an event, create a task, and let the normal orchestrator loop run
it. Turns Sisyphus from reactive into proactive.

Firing = store.create_task(...) + orchestrator.emit(task_id, "created"): an
autopilot task is an ordinary task and inherits every guardrail (worker
semaphore, per-task budget, pass budget). The tick loop lives on the daemon's
event loop next to orchestrator.dispatch_forever.
"""
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from uuid import uuid4

from core.db import get_conn

logger = logging.getLogger(__name__)

_TERMINAL = ("done", "failed")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dict(row) -> Optional[dict]:
    return dict(row) if row is not None else None


def init_autopilot_table() -> None:
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS autopilots (
            id               TEXT PRIMARY KEY,
            name             TEXT NOT NULL,
            description      TEXT NOT NULL DEFAULT '',
            trigger_type     TEXT NOT NULL DEFAULT 'schedule',
            cron             TEXT NOT NULL DEFAULT '',
            interval_seconds INTEGER NOT NULL DEFAULT 0,
            webhook_platform TEXT NOT NULL DEFAULT '',
            webhook_event    TEXT NOT NULL DEFAULT '',
            goal_template    TEXT NOT NULL DEFAULT '',
            repo             TEXT NOT NULL DEFAULT '',
            agent            TEXT NOT NULL DEFAULT '',
            enabled          INTEGER NOT NULL DEFAULT 1,
            skip_if_running  INTEGER NOT NULL DEFAULT 1,
            last_run         TEXT,
            next_run         TEXT,
            last_task_id     TEXT,
            run_count        INTEGER NOT NULL DEFAULT 0,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


# ── schedule math ───────────────────────────────────────────────────────

def compute_next_run(ap: dict, after: Optional[datetime] = None) -> Optional[str]:
    """Next fire time (ISO UTC) for a schedule autopilot, or None if not a
    schedule / not computable. Cron via apscheduler; interval via now+seconds."""
    now = after or datetime.now(timezone.utc)
    if (ap.get("cron") or "").strip():
        try:
            from apscheduler.triggers.cron import CronTrigger
            trig = CronTrigger.from_crontab(ap["cron"].strip(), timezone=timezone.utc)
            nxt = trig.get_next_fire_time(None, now)
            return nxt.isoformat() if nxt else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("autopilot %s: bad cron %r (%s)", ap.get("id"), ap.get("cron"), exc)
            return None
    secs = int(ap.get("interval_seconds") or 0)
    if secs > 0:
        return (now + timedelta(seconds=secs)).isoformat()
    return None


def is_schedule(ap: dict) -> bool:
    return ap.get("trigger_type") == "schedule" and (
        bool((ap.get("cron") or "").strip()) or int(ap.get("interval_seconds") or 0) > 0
    )


# ── CRUD ────────────────────────────────────────────────────────────────

_FIELDS = ("name", "description", "trigger_type", "cron", "interval_seconds",
           "webhook_platform", "webhook_event", "goal_template", "repo", "agent",
           "enabled", "skip_if_running")


def create_autopilot(**fields) -> dict:
    ap = {k: fields.get(k) for k in _FIELDS}
    ap["id"] = f"ap-{uuid4().hex[:8]}"
    now = _now()
    ap["enabled"] = int(fields.get("enabled", 1))
    ap["skip_if_running"] = int(fields.get("skip_if_running", 1))
    ap["interval_seconds"] = int(fields.get("interval_seconds") or 0)
    for k in ("name", "description", "trigger_type", "cron", "webhook_platform",
              "webhook_event", "goal_template", "repo", "agent"):
        ap[k] = ap.get(k) or ("" if k != "trigger_type" else "schedule")
    ap["next_run"] = compute_next_run(ap, datetime.now(timezone.utc)) if is_schedule(ap) else None
    conn = get_conn()
    conn.execute(
        """INSERT INTO autopilots
           (id, name, description, trigger_type, cron, interval_seconds,
            webhook_platform, webhook_event, goal_template, repo, agent,
            enabled, skip_if_running, next_run, run_count, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)""",
        (ap["id"], ap["name"], ap["description"], ap["trigger_type"], ap["cron"],
         ap["interval_seconds"], ap["webhook_platform"], ap["webhook_event"],
         ap["goal_template"], ap["repo"], ap["agent"], ap["enabled"],
         ap["skip_if_running"], ap["next_run"], now, now),
    )
    conn.commit()
    conn.close()
    return get_autopilot(ap["id"])


def get_autopilot(ap_id: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM autopilots WHERE id = ?", (ap_id,)).fetchone()
    conn.close()
    return _dict(row)


def list_autopilots(enabled: Optional[bool] = None) -> List[dict]:
    conn = get_conn()
    if enabled is None:
        rows = conn.execute("SELECT * FROM autopilots ORDER BY created_at DESC").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM autopilots WHERE enabled = ? ORDER BY created_at DESC",
            (int(enabled),),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_autopilot(ap_id: str, **fields) -> Optional[dict]:
    updates = {k: v for k, v in fields.items() if k in _FIELDS and v is not None}
    if "interval_seconds" in updates:
        updates["interval_seconds"] = int(updates["interval_seconds"])
    if "enabled" in updates:
        updates["enabled"] = int(updates["enabled"])
    if "skip_if_running" in updates:
        updates["skip_if_running"] = int(updates["skip_if_running"])
    if updates:
        cols = ", ".join(f"{k} = ?" for k in updates)
        conn = get_conn()
        conn.execute(f"UPDATE autopilots SET {cols}, updated_at = ? WHERE id = ?",
                     (*updates.values(), _now(), ap_id))
        conn.commit()
        conn.close()
    # a schedule change moves the next fire time; a non-schedule clears it
    ap = get_autopilot(ap_id)
    if ap:
        nxt = compute_next_run(ap, datetime.now(timezone.utc)) if is_schedule(ap) else None
        _set_next_run(ap_id, nxt)
    return get_autopilot(ap_id)


def set_enabled(ap_id: str, enabled: bool) -> Optional[dict]:
    return update_autopilot(ap_id, enabled=int(bool(enabled)))


def delete_autopilot(ap_id: str) -> bool:
    conn = get_conn()
    cur = conn.execute("DELETE FROM autopilots WHERE id = ?", (ap_id,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def _set_next_run(ap_id: str, next_run: Optional[str]) -> None:
    conn = get_conn()
    conn.execute("UPDATE autopilots SET next_run = ?, updated_at = ? WHERE id = ?",
                 (next_run, _now(), ap_id))
    conn.commit()
    conn.close()


def record_fire(ap_id: str, task_id: str) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE autopilots SET last_run = ?, last_task_id = ?, "
        "run_count = run_count + 1, updated_at = ? WHERE id = ?",
        (_now(), task_id, _now(), ap_id),
    )
    conn.commit()
    conn.close()


# ── firing ──────────────────────────────────────────────────────────────

def render_template(tmpl: str, context: dict) -> str:
    """Substitute {{ a.b.c }} from a (possibly nested) context dict. Missing
    fields render empty. No code evaluation."""
    def repl(m):
        cur = context
        for part in m.group(1).strip().split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
            if cur is None:
                return ""
        return str(cur)
    return re.sub(r"\{\{\s*([^}]+?)\s*\}\}", repl, tmpl or "")


def _make_title(ap: dict, context: Optional[dict] = None) -> Optional[str]:
    """Build a human-readable display title for a task fired by this autopilot.

    Schedule: ``"name · 2026-08-09"``.
    Webhook:  ``"name: event"``, or just the name if no event key.
    Truncated to 60 chars."""
    name = (ap.get("name") or "").strip()
    if not name:
        return None
    tp = ap.get("trigger_type", "")
    if tp == "schedule":
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        title = f"{name} · {date_str}"
    elif tp == "webhook":
        if context:
            platform = ap.get("webhook_platform", "")
            key = event_key(platform, context)
        else:
            key = ""
        title = f"{name}: {key}" if key else name
    else:
        return None
    if len(title) > 60:
        title = title[:59] + "…"
    return title


def fire(ap: dict, context: Optional[dict] = None) -> Optional[str]:
    """Create a task from the autopilot and wake the orchestrator. Returns the
    task id, or None if skipped (previous run still in flight)."""
    context = context or {}
    from core import store
    from core.orchestrator import emit

    if ap.get("skip_if_running") and ap.get("last_task_id"):
        prev = store.get_task(ap["last_task_id"])
        if prev and prev["status"] not in _TERMINAL:
            logger.info("autopilot %s: skip — prev task %s still %s",
                        ap["name"], prev["id"], prev["status"])
            return None

    goal = render_template(ap.get("goal_template", ""), context).strip() or ap["name"]
    title = _make_title(ap, context)
    task = store.create_task(goal, ap.get("repo") or "", agent=ap.get("agent") or None,
                             title=title)
    emit(task["id"], "created")
    record_fire(ap["id"], task["id"])
    logger.info("autopilot %s fired -> task %s", ap["name"], task["id"])
    return task["id"]


# ── webhook triggers ────────────────────────────────────────────────────

def event_key(platform: str, payload: dict) -> str:
    """A short string identifying the event, for the optional webhook_event
    filter. github: action; gitlab: object_kind; jira: webhookEvent."""
    payload = payload or {}
    if platform == "github":
        return str(payload.get("action") or "")
    if platform == "gitlab":
        return str(payload.get("object_kind") or "")
    if platform == "jira":
        return str(payload.get("webhookEvent") or "")
    return ""


def fire_webhook_autopilots(platform: str, payload: dict) -> list:
    """Fire every enabled webhook autopilot matching this platform + event
    filter, rendering the goal template against the payload. Returns the fires."""
    key = event_key(platform, payload).lower()
    fired = []
    for ap in list_autopilots(enabled=True):
        if ap.get("trigger_type") != "webhook":
            continue
        if ap.get("webhook_platform") and ap["webhook_platform"] != platform:
            continue
        filt = (ap.get("webhook_event") or "").strip().lower()
        if filt and filt not in key:
            continue
        tid = fire(ap, context=payload or {})
        if tid:
            fired.append({"autopilot": ap["id"], "task_id": tid})
    return fired


# ── tick loop ───────────────────────────────────────────────────────────

def reconcile_next_runs() -> None:
    """On boot, recompute next_run for schedule autopilots from *now* — missed
    fires while the daemon was down are skipped, never replayed."""
    now = datetime.now(timezone.utc)
    for ap in list_autopilots(enabled=True):
        if is_schedule(ap):
            _set_next_run(ap["id"], compute_next_run(ap, now))


def tick_once() -> int:
    """Fire every due schedule autopilot once; advance its next_run regardless
    of fire/skip so a due one can't spam every tick. Returns how many fired."""
    now_iso = _now()
    fired = 0
    for ap in list_autopilots(enabled=True):
        if not is_schedule(ap):
            continue
        if ap.get("next_run") and ap["next_run"] <= now_iso:
            try:
                if fire(ap):
                    fired += 1
            except Exception:  # noqa: BLE001
                logger.exception("autopilot %s: fire failed", ap["id"])
            _set_next_run(ap["id"], compute_next_run(ap, datetime.now(timezone.utc)))
    return fired


async def run_forever() -> None:
    """Daemon background loop: reconcile on boot, then fire due autopilots every
    autopilot_tick_seconds."""
    import asyncio
    from core.config import get_config

    tick = int(get_config("autopilot_tick_seconds", 30) or 30)
    try:
        reconcile_next_runs()
    except Exception:  # noqa: BLE001
        logger.exception("autopilot: reconcile_on_boot failed")
    while True:
        try:
            tick_once()
        except Exception:  # noqa: BLE001
            logger.exception("autopilot: tick failed")
        await asyncio.sleep(tick)
