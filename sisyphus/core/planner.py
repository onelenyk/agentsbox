"""Mission Planner — plan a big goal into a project-aware Plan Brief (approach +
diagrams + risks + steps), convert the steps into a DAG of draft tasks, release
them deliberately, and track progress.

Nothing costs anything until a draft is launched. Launched drafts are ordinary
tasks (they inherit the worker semaphore, budget caps, thread/MR). Planning is a
one-shot local AI call grounded in the repo's own context.
"""
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4

from core.db import get_conn

logger = logging.getLogger(__name__)

_AGENTS = ("engineer", "reviewer", "explorer", "planner")
_KEY_FILES = ("AGENTS.md", "CLAUDE.md", "README.md", ".cursorrules", "CONVENTIONS.md")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dict(row) -> Optional[dict]:
    return dict(row) if row is not None else None


def init_planner_tables() -> None:
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS plans (
            id         TEXT PRIMARY KEY,
            title      TEXT NOT NULL DEFAULT 'New plan',
            repo       TEXT NOT NULL DEFAULT '',
            status     TEXT NOT NULL DEFAULT 'planning',
            brief      TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS plan_items (
            id         TEXT PRIMARY KEY,
            plan_id    TEXT NOT NULL,
            goal       TEXT NOT NULL DEFAULT '',
            repo       TEXT NOT NULL DEFAULT '',
            agent      TEXT NOT NULL DEFAULT '',
            after      TEXT NOT NULL DEFAULT '[]',
            position   INTEGER NOT NULL DEFAULT 0,
            status     TEXT NOT NULL DEFAULT 'draft',
            task_id    TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_planitem_plan ON plan_items(plan_id)")
    conn.commit()
    conn.close()


# ── plans CRUD ──────────────────────────────────────────────────────────

def create_plan(title: str = "New plan", repo: str = "") -> dict:
    pid = f"pl-{uuid4().hex[:8]}"
    now = _now()
    conn = get_conn()
    conn.execute("INSERT INTO plans (id, title, repo, created_at, updated_at) VALUES (?,?,?,?,?)",
                 (pid, title or "New plan", repo or "", now, now))
    conn.commit()
    conn.close()
    return get_plan(pid)


def get_plan(pid: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM plans WHERE id = ?", (pid,)).fetchone()
    conn.close()
    p = _dict(row)
    if p:
        p["brief_obj"] = json.loads(p["brief"]) if p["brief"] else None
    return p


def latest_plan() -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT id FROM plans ORDER BY updated_at DESC LIMIT 1").fetchone()
    conn.close()
    return get_plan(row["id"]) if row else None


def list_plans(limit: int = 30) -> List[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM plans ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_plan(pid: str, **fields) -> Optional[dict]:
    allowed = {k: v for k, v in fields.items() if k in ("title", "repo", "status", "brief") and v is not None}
    if allowed:
        cols = ", ".join(f"{k} = ?" for k in allowed)
        conn = get_conn()
        conn.execute(f"UPDATE plans SET {cols}, updated_at = ? WHERE id = ?",
                     (*allowed.values(), _now(), pid))
        conn.commit()
        conn.close()
    return get_plan(pid)


def delete_plan(pid: str) -> bool:
    conn = get_conn()
    conn.execute("DELETE FROM plan_items WHERE plan_id = ?", (pid,))
    cur = conn.execute("DELETE FROM plans WHERE id = ?", (pid,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


# ── plan_items CRUD + DAG ───────────────────────────────────────────────

def add_item(plan_id: str, goal: str, repo: str = "", agent: str = "",
             after: Optional[list] = None, position: int = 0) -> dict:
    iid = f"pi-{uuid4().hex[:8]}"
    now = _now()
    conn = get_conn()
    conn.execute("""INSERT INTO plan_items (id, plan_id, goal, repo, agent, after, position, created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                 (iid, plan_id, goal, repo, agent, json.dumps(after or []), position, now, now))
    conn.commit()
    conn.close()
    return get_item(iid)


def get_item(iid: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM plan_items WHERE id = ?", (iid,)).fetchone()
    conn.close()
    it = _dict(row)
    if it:
        it["after"] = json.loads(it["after"] or "[]")
    return it


def list_items(plan_id: str) -> List[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM plan_items WHERE plan_id = ? ORDER BY position ASC, created_at ASC",
                        (plan_id,)).fetchall()
    conn.close()
    items = [dict(r) for r in rows]
    for it in items:
        it["after"] = json.loads(it["after"] or "[]")
    return items


def update_item(iid: str, **fields) -> Optional[dict]:
    allowed = {k: v for k, v in fields.items()
               if k in ("goal", "repo", "agent", "after", "position", "status", "task_id") and v is not None}
    if "after" in allowed:
        allowed["after"] = json.dumps(allowed["after"])
    if allowed:
        cols = ", ".join(f"{k} = ?" for k in allowed)
        conn = get_conn()
        conn.execute(f"UPDATE plan_items SET {cols}, updated_at = ? WHERE id = ?",
                     (*allowed.values(), _now(), iid))
        conn.commit()
        conn.close()
    return get_item(iid)


def delete_item(iid: str) -> bool:
    conn = get_conn()
    cur = conn.execute("DELETE FROM plan_items WHERE id = ?", (iid,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def has_cycle(items: List[dict]) -> bool:
    """True if the plan_items `after` edges contain a cycle."""
    graph = {it["id"]: list(it.get("after") or []) for it in items}
    WHITE, GREY, BLACK = 0, 1, 2
    color = {n: WHITE for n in graph}

    def visit(n):
        color[n] = GREY
        for m in graph.get(n, []):
            if m not in color:
                continue
            if color[m] == GREY or (color[m] == WHITE and visit(m)):
                return True
        color[n] = BLACK
        return False

    return any(color[n] == WHITE and visit(n) for n in graph)


def item_state(item: dict, by_id: dict) -> str:
    """Derived UI state: draft items are 'ready' when all deps are done, else
    'blocked'. Launched/done/failed/skipped pass through."""
    if item["status"] != "draft":
        return item["status"]
    deps = item.get("after") or []
    if all(by_id.get(a, {}).get("status") == "done" for a in deps):
        return "ready"
    return "blocked"


def releasable(item: dict, by_id: dict) -> bool:
    return item["status"] == "draft" and all(
        by_id.get(a, {}).get("status") == "done" for a in (item.get("after") or []))


# ── project-aware planning ──────────────────────────────────────────────

def project_context(repo_name: str) -> str:
    """A compact grounding blob: the repo's summary, top-level tree, and the
    content of its key instruction files — what the agent should plan against."""
    from core.repos import get_repo
    from core import repo_context
    repo = get_repo(repo_name)
    if not repo:
        return ""
    src = repo.get("source", "")
    parts = [f"PROJECT: {repo_name} (branch {repo.get('branch', 'main')})"]
    summ = repo_context.summary(src)
    if summ.get("exists"):
        langs = ", ".join(f"{e}:{n}" for e, n in summ.get("langs", []))
        parts.append(f"~{summ['files']} files. Top extensions: {langs or 'n/a'}.")
    try:
        entries = sorted(e for e in os.listdir(src) if not e.startswith(".git"))[:40]
        parts.append("Top-level: " + ", ".join(entries))
    except Exception:
        pass
    for res in repo_context.resources(src):
        if res["kind"] == "file" and res["path"] in _KEY_FILES:
            content = repo_context.read_resource(src, res["path"])
            if content:
                parts.append(f"\n--- {res['path']} ---\n{content[:1500]}")
    return "\n".join(parts)


def _plan_prompt(message: str, repo: str, ctx: str, brief: Optional[dict]) -> str:
    cur = ""
    if brief:
        cur = ("\nCURRENT PLAN (refine it):\n" + json.dumps(
            {"approach": brief.get("approach", ""),
             "steps": [s.get("goal", "") for s in brief.get("steps", [])]})[:1200])
    return (
        "You are a senior engineer planning a software task on a SPECIFIC project. "
        "Ground the plan in this project — reference real files/modules and follow "
        "its conventions.\n\n"
        + (ctx or "(no project context)") + cur +
        "\n\nUser request: " + message +
        "\n\nReturn ONLY one JSON object (no prose, no code fences):\n"
        "{\n"
        '  "approach": "<1-3 short paragraphs on the approach>",\n'
        '  "diagram": "<mermaid \'graph TD\' source for the interaction/architecture, or empty>",\n'
        '  "steps": [{"goal": "<clear, self-contained task goal>", "repo": "' + repo + '", "agent": "<engineer|reviewer|explorer or empty>", "after": [<0-based indices of prerequisite steps>]}],\n'
        '  "risks": ["<a concrete potential problem>"],\n'
        '  "questions": ["<an open question to clarify>"],\n'
        '  "affected": ["<file or module>"]\n'
        "}\n"
        "Use \"after\" to express dependencies (a step index that must finish first). "
        "Available agents: engineer, reviewer, explorer."
    )


# Planning runs in a background thread (opencode is a blocking subprocess and a
# big-repo plan can take a minute) — the UI shows a progress bar and polls.
_generating = set()
_errors = {}
_lock = threading.Lock()


def is_generating(plan_id: str) -> bool:
    return plan_id in _generating


def pop_error(plan_id: str):
    return _errors.pop(plan_id, None)


def start_plan_turn(plan_id: str, message: str) -> None:
    """Kick off planning in the background. Safe to call once; ignored if a plan
    is already generating."""
    with _lock:
        if plan_id in _generating:
            return
        _generating.add(plan_id)
    _errors.pop(plan_id, None)

    def _run():
        try:
            plan_turn(plan_id, message, timeout=240)
        except Exception as exc:  # noqa: BLE001
            logger.exception("plan_turn failed for %s", plan_id)
            _errors[plan_id] = str(exc)[:200]
        finally:
            _generating.discard(plan_id)

    threading.Thread(target=_run, daemon=True).start()


def plan_turn(plan_id: str, message: str, timeout: int = 90) -> dict:
    """Generate/refine the Plan Brief for a plan, grounded in its repo. Saves the
    brief on the plan and returns it."""
    from core import ai_assist
    plan = get_plan(plan_id)
    if not plan:
        raise ValueError("plan not found")
    ctx = project_context(plan["repo"]) if plan["repo"] else "(no repo selected — plan generically)"
    out = ai_assist.run_prompt(_plan_prompt(message, plan["repo"], ctx, plan.get("brief_obj")), timeout=timeout)
    brief = _clean_brief(_find_json(out), plan["repo"])
    update_plan(plan_id, brief=json.dumps(brief))
    return brief


def _clean_brief(obj, repo: str) -> dict:
    obj = obj if isinstance(obj, dict) else {}
    steps = []
    for s in (obj.get("steps") or []):
        if not isinstance(s, dict) or not str(s.get("goal", "")).strip():
            continue
        after = [i for i in (s.get("after") or []) if isinstance(i, int)]
        agent = str(s.get("agent", "") or "").strip()
        steps.append({
            "goal": str(s["goal"]).strip(),
            "repo": str(s.get("repo", "") or repo).strip(),
            "agent": agent if agent in _AGENTS else "",
            "after": after,
        })
    def _strs(key):
        return [str(x).strip() for x in (obj.get(key) or []) if str(x).strip()]
    return {
        "approach": str(obj.get("approach", "") or "").strip(),
        "diagram": str(obj.get("diagram", "") or "").strip(),
        "steps": steps,
        "risks": _strs("risks"),
        "questions": _strs("questions"),
        "affected": _strs("affected"),
    }


# ── convert → drafts, launch, advance ───────────────────────────────────

def convert_to_drafts(plan_id: str) -> List[dict]:
    """Turn the brief's steps into plan_items (a DAG), mapping `after` indices to
    the created item ids. Replaces any existing draft items for this plan."""
    plan = get_plan(plan_id)
    brief = (plan or {}).get("brief_obj") or {}
    steps = brief.get("steps") or []
    # clear existing draft (unlaunched) items so re-convert is idempotent
    for it in list_items(plan_id):
        if it["status"] == "draft":
            delete_item(it["id"])
    ids = []
    for i, s in enumerate(steps):
        it = add_item(plan_id, s["goal"], s.get("repo", ""), s.get("agent", ""), after=[], position=i)
        ids.append(it["id"])
    for i, s in enumerate(steps):
        after_ids = [ids[j] for j in s.get("after", []) if 0 <= j < len(ids) and j != i]
        if after_ids:
            update_item(ids[i], after=after_ids)
    return list_items(plan_id)


def launch_item(iid: str) -> dict:
    """Launch a draft item as a real task (only if releasable). Returns the item."""
    it = get_item(iid)
    if not it:
        raise ValueError("item not found")
    if it["status"] != "draft":
        raise ValueError(f"item already {it['status']}")
    by_id = {x["id"]: x for x in list_items(it["plan_id"])}
    if not releasable(it, by_id):
        raise ValueError("blocked — a prerequisite isn't done yet")
    from core import store
    from core.orchestrator import emit
    task = store.create_task(it["goal"], it.get("repo") or "", agent=it.get("agent") or None)
    emit(task["id"], "created")
    update_item(iid, status="launched", task_id=task["id"])
    return get_item(iid)


def on_task_terminal(task_id: str, status: str) -> None:
    """Advance the plan when a launched task terminates — mark its item done/failed.
    (Auto-release of newly-ready items is P2; here we just record progress.)"""
    conn = get_conn()
    row = conn.execute("SELECT id FROM plan_items WHERE task_id = ? AND status = 'launched'",
                       (task_id,)).fetchone()
    conn.close()
    if row:
        update_item(row["id"], status=("done" if status == "done" else "failed"))


def steps_dag_mermaid(steps: list) -> str:
    """A `graph TD` mermaid picture of the plan steps + their `after` deps."""
    if not steps:
        return ""
    lines = ["graph TD"]
    for i, s in enumerate(steps):
        label = (s.get("goal", "")[:38]).replace('"', "'").replace("\n", " ")
        lines.append(f'  S{i}["{i + 1}. {label}"]')
    for i, s in enumerate(steps):
        for j in (s.get("after") or []):
            if isinstance(j, int) and 0 <= j < len(steps) and j != i:
                lines.append(f"  S{j} --> S{i}")
    return "\n".join(lines)


def _find_json(out: str):
    s = re.sub(r"```(?:json)?", "", out).strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    for m in reversed(re.findall(r"\{.*\}", out, re.DOTALL)):
        try:
            return json.loads(m)
        except Exception:
            continue
    return None
