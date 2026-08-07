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
            id           TEXT PRIMARY KEY,
            title        TEXT NOT NULL DEFAULT 'New plan',
            repo         TEXT NOT NULL DEFAULT '',
            status       TEXT NOT NULL DEFAULT 'planning',
            brief        TEXT NOT NULL DEFAULT '',
            auto_release INTEGER NOT NULL DEFAULT 0,
            auto_merge   INTEGER NOT NULL DEFAULT 0,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        )
    """)
    # migrate existing DBs: add the autonomy flags if they predate this column
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(plans)").fetchall()}
    if "auto_release" not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN auto_release INTEGER NOT NULL DEFAULT 0")
    if "auto_merge" not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN auto_merge INTEGER NOT NULL DEFAULT 0")
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS plan_messages (
            id         TEXT PRIMARY KEY,
            plan_id    TEXT NOT NULL,
            role       TEXT NOT NULL,
            text       TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_planmsg_plan ON plan_messages(plan_id)")
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
    allowed = {k: v for k, v in fields.items()
               if k in ("title", "repo", "status", "brief", "auto_release", "auto_merge") and v is not None}
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
    conn.execute("DELETE FROM plan_messages WHERE plan_id = ?", (pid,))
    cur = conn.execute("DELETE FROM plans WHERE id = ?", (pid,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


# ── co-plan conversation ────────────────────────────────────────────────

def add_message(plan_id: str, role: str, text: str) -> str:
    mid = f"pm-{uuid4().hex[:8]}"
    conn = get_conn()
    conn.execute("INSERT INTO plan_messages (id, plan_id, role, text, created_at) VALUES (?,?,?,?,?)",
                 (mid, plan_id, role, text, _now()))
    conn.commit()
    conn.close()
    return mid


def list_messages(plan_id: str, limit: int = 60) -> List[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM plan_messages WHERE plan_id = ? ORDER BY created_at ASC LIMIT ?",
                        (plan_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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
        # Guard: if modifying dependencies, check that it doesn't introduce a cycle
        it = get_item(iid)
        if it:
            plan_items = list_items(it["plan_id"])
            # Create a hypothetical copy with the updated `after` field for cycle check
            test_items = []
            for pi in plan_items:
                if pi["id"] == iid:
                    test_items.append({**pi, "after": fields.get("after") or []})
                else:
                    test_items.append(pi)
            if has_cycle(test_items):
                raise ValueError("plan steps contain a dependency cycle")
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
    except Exception as e:
        logger.debug("project_context: os.listdir(%s) failed: %s", src, e)
        pass
    for res in repo_context.resources(src):
        if res["kind"] == "file" and res["path"] in _KEY_FILES:
            content = repo_context.read_resource(src, res["path"])
            if content:
                parts.append(f"\n--- {res['path']} ---\n{content[:1500]}")
    return "\n".join(parts)


def _plan_prompt(message: str, repo: str, ctx: str, brief: Optional[dict],
                 history: Optional[list] = None) -> str:
    cur = ""
    if brief:
        cur = ("\nCURRENT PLAN (evolve it — don't restart from scratch):\n" + json.dumps(
            {"approach": brief.get("approach", ""),
             "steps": [s.get("goal", "") for s in brief.get("steps", [])]})[:1200])
    convo = ""
    if history:
        lines = []
        for m in history[-8:]:
            who = "User" if m["role"] == "user" else "You"
            lines.append(f"{who}: {(m['text'] or '')[:400]}")
        if lines:
            convo = "\nCONVERSATION SO FAR:\n" + "\n".join(lines)
    return (
        "You are a senior engineer CO-PLANNING a software task WITH the user on a "
        "SPECIFIC project — a natural back-and-forth. Ground the plan in this project "
        "(real files/modules, its conventions) and evolve it as the conversation "
        "progresses. Talk to the user in \"reply\"; keep the structured plan up to date.\n\n"
        + (ctx or "(no project context)") + cur + convo +
        "\n\nLatest user message: " + message +
        "\n\nReturn ONLY one JSON object (no prose, no code fences):\n"
        "{\n"
        '  "reply": "<a short, warm conversational reply (1-3 sentences): what you changed this turn, or a question back to the user>",\n'
        '  "approach": "<1-3 short paragraphs on the approach>",\n'
        '  "diagram": "<mermaid \'graph TD\' source; wrap EVERY node label in double quotes so it parses (e.g. A[\\"Click Save\\"] --> B), or empty>",\n'
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

    # name the plan from the opening message, and record the user turn so the
    # conversation shows it immediately while the reply generates in the bg.
    plan = get_plan(plan_id)
    if plan and (plan.get("title") or "").strip() in ("", "New plan") and not list_messages(plan_id):
        update_plan(plan_id, title=message.strip()[:48])
    add_message(plan_id, "user", message)

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
    history = list_messages(plan_id)
    if history and history[-1]["role"] == "user":
        history = history[:-1]   # the current turn is passed as `message`
    out = ai_assist.run_prompt(
        _plan_prompt(message, plan["repo"], ctx, plan.get("brief_obj"), history), timeout=timeout)
    parsed = _find_json(out)
    if not isinstance(parsed, dict):
        # The model returned prose or errored — don't clobber the canvas with an
        # empty brief; surface it so the UI can show a retryable error.
        raise ValueError("The planner didn't return a usable plan this time — try rephrasing or send again.")
    brief = _clean_brief(parsed, plan["repo"])
    # keep the previous brief's artifacts if the model somehow returned an empty shell
    if not (brief["approach"] or brief["diagram"] or brief["steps"] or brief["risks"] or brief["affected"]):
        prev = plan.get("brief_obj") or {}
        for k in ("approach", "diagram", "steps", "risks", "affected"):
            if not brief[k] and prev.get(k):
                brief[k] = prev[k]
    update_plan(plan_id, brief=json.dumps(brief))
    add_message(plan_id, "assistant", brief.get("reply") or "Updated the plan on the canvas.")
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
        "reply": str(obj.get("reply", "") or "").strip(),
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

    # Guard: check the dependency graph for cycles BEFORE updating items
    # Build a hypothetical items list with updated dependencies
    items = list_items(plan_id)
    test_items = []
    for i, s in enumerate(steps):
        after_ids = [ids[j] for j in s.get("after", []) if 0 <= j < len(ids) and j != i]
        for it in items:
            if it["id"] == ids[i]:
                test_it = dict(it)
                test_it["after"] = after_ids
                test_items.append(test_it)
                break
    if has_cycle(test_items):
        # rollback: delete the created items since they form a cycle
        for it in items:
            if it["status"] == "draft":
                delete_item(it["id"])
        raise ValueError("plan steps contain a dependency cycle")

    # Now safe to update items with dependencies
    for i, s in enumerate(steps):
        after_ids = [ids[j] for j in s.get("after", []) if 0 <= j < len(ids) and j != i]
        if after_ids:
            update_item(ids[i], after=after_ids)
    return list_items(plan_id)


def plan_branch(plan_id: str) -> str:
    """The integration branch a plan's steps stack on: each step is cut from it
    and merged back into it, so dependent steps inherit each other's code. Lives
    in the source repo; never touches the repo's default branch."""
    return f"plan/{plan_id}"


def _ensure_plan_branch(plan_id: str, repo_name: str) -> Optional[str]:
    """Stand up the plan's integration branch in the source repo, off the repo's
    default. Returns the branch name, or None if the repo can't be resolved (in
    which case the step just falls back to the repo default base)."""
    try:
        from core import repos, git_ops
        r = repos.get_repo(repo_name)
        if not r or not r.get("source"):
            return None
        br = plan_branch(plan_id)
        git_ops.ensure_branch(r["source"], br, r.get("branch") or "main")
        return br
    except Exception:  # noqa: BLE001 — never let integration setup block a launch
        logger.exception("could not ensure plan branch for %s", plan_id)
        return None


def _ai_conflict_resolver(path: str, text: str) -> Optional[str]:
    """Merge one conflicted file with the local model, preserving both sides.
    Returns the resolved content, or None if it couldn't (markers remain / empty)
    so the caller aborts and leaves the conflict for a human."""
    from core import ai_assist
    prompt = (
        "You are resolving ONE git merge conflict. The file below contains conflict "
        "markers (<<<<<<<, =======, >>>>>>>). Produce the correct merged file that "
        "PRESERVES the intent of BOTH sides — never drop either side's changes — and "
        "removes every conflict marker. Respond with ONLY a JSON object: "
        '{"resolved": "<the complete merged file content>"}.\n\n'
        f"File: {path}\n\n{text}")
    try:
        obj = _find_json(ai_assist.run_prompt(prompt, timeout=150))
    except Exception as e:  # noqa: BLE001
        logger.debug("ai_conflict_resolver: AI resolution failed for %s: %s", path, e)
        return None
    if not isinstance(obj, dict) or "resolved" not in obj:
        return None
    out = str(obj["resolved"])
    if "<<<<<<<" in out or ">>>>>>>" in out or not out.strip():
        return None
    return out if out.endswith("\n") else out + "\n"


def _integrate_into_plan_branch(task_id: str, plan_id: str) -> None:
    """Merge a finished step's task branch into the plan's integration branch, so
    steps launched after it are cut from a base that already contains its code.
    A clean merge just commits; a conflict (genuinely parallel steps on the same
    lines) is auto-resolved by the model, preserving both sides — and if it can't,
    it's left untouched for a human (the step's own MR still stands). The plan
    branch never reaches main without a human reviewing the final plan MR, so an
    imperfect auto-resolution can't silently land."""
    try:
        from core import mergerequests
        m = mergerequests.latest_for_task(task_id)
        if not m:
            return   # reviewer / no-change step — nothing to integrate
        res = mergerequests.merge_resolving(m, plan_branch(plan_id), _ai_conflict_resolver)
        if res.get("ok") and res.get("resolved"):
            logger.info("integrated %s into %s — AI-resolved conflicts in %s",
                        m["source_branch"], plan_branch(plan_id), res["resolved"])
        elif res.get("ok"):
            logger.info("integrated %s into %s (%s)", m["source_branch"],
                        plan_branch(plan_id), res["sha"][:8])
        else:
            logger.warning("could not integrate %s into %s: %s — left for manual merge",
                           m["source_branch"], plan_branch(plan_id), res.get("error"))
    except Exception:  # noqa: BLE001
        logger.exception("integrate-into-plan-branch errored for task %s", task_id)


def _open_plan_mr(plan_id: str) -> None:
    """Once every step is done, open ONE merge request from the plan's integration
    branch to the repo default — the whole feature, assembled by the steps, as a
    single reviewable change to land. Idempotent; only opens if the plan branch
    actually diverged from the default."""
    try:
        from core import mergerequests, repos, git_ops
        plan = get_plan(plan_id)
        if not plan or not plan.get("repo"):
            return
        r = repos.get_repo(plan["repo"])
        if not r or not r.get("source"):
            return
        src, default = r["source"], (r.get("branch") or "main")
        br = plan_branch(plan_id)
        ahead = git_ops._run(["git", "-C", src, "rev-list", "--count",
                              f"{default}..{br}"]).stdout.strip()
        if not ahead or ahead == "0":
            return   # nothing landed on the plan branch — no feature to open
        if any(m.get("source_branch") == br for m in mergerequests.list_mrs()):
            return   # already opened
        mergerequests.create_mr(
            task_id=f"plan:{plan_id}", repo=plan["repo"], source_branch=br,
            target_branch=default, title=f"{plan.get('title') or 'Plan'} — full feature",
            bare_path=src, base_branch=default, source_path=src)
        logger.info("opened plan MR %s -> %s for %s", br, default, plan_id)
    except Exception:  # noqa: BLE001
        logger.exception("could not open plan MR for %s", plan_id)


def launch_item(iid: str) -> dict:
    """Launch a draft item as a real task (only if releasable). Returns the item.
    The task is cut from the plan's integration branch so it inherits whatever
    earlier steps merged there."""
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
    base_br = _ensure_plan_branch(it["plan_id"], it["repo"]) if it.get("repo") else None
    task = store.create_task(it["goal"], it.get("repo") or "",
                             agent=it.get("agent") or None, base_branch=base_br)
    emit(task["id"], "created")
    update_item(iid, status="launched", task_id=task["id"])
    return get_item(iid)


def on_task_terminal(task_id: str, status: str) -> None:
    """Advance the plan when a launched task terminates. Marks the item done/failed,
    then — if the plan opts in — auto-merges the item's MR and auto-releases any
    now-ready dependents, so the whole DAG can run hands-off. Runs inside the
    daemon that executes tasks, so launching more work here is safe."""
    conn = get_conn()
    row = conn.execute("SELECT id, plan_id FROM plan_items WHERE task_id = ? AND status = 'launched'",
                       (task_id,)).fetchone()
    conn.close()
    if not row:
        return
    update_item(row["id"], status=("done" if status == "done" else "failed"))
    if status != "done":
        return   # a failed step blocks its dependents — don't cascade past it

    plan = get_plan(row["plan_id"])
    if not plan:
        return
    # 1) integrate this step into the plan branch — always, so any step launched
    #    after it (a dependent, or a later auto-released one) is cut from a base
    #    that already contains its code. This is what lets dependent steps build
    #    on each other instead of each re-cloning a pristine default branch.
    _integrate_into_plan_branch(task_id, plan["id"])
    # 2) auto-merge this step's MR to its real target (opt-in; writes to main)
    if plan.get("auto_merge"):
        _auto_merge_task(task_id)
    # 3) auto-release: launch every draft item whose prerequisites are now done
    if plan.get("auto_release"):
        items = list_items(plan["id"])
        by = {x["id"]: x for x in items}
        for it in items:
            if it["status"] == "draft" and releasable(it, by):
                try:
                    launch_item(it["id"])
                except Exception:  # noqa: BLE001
                    logger.exception("auto-release failed for item %s", it["id"])
    # 4) plan complete → open the single land-the-feature MR (plan branch → default)
    items = list_items(plan["id"])
    if items and all(it["status"] in ("done", "skipped") for it in items):
        _open_plan_mr(plan["id"])


def _auto_merge_task(task_id: str) -> None:
    """Merge a task's open MR into its target if it merges cleanly. Best-effort:
    a conflict or a missing/absent MR is left for the user to resolve by hand."""
    try:
        from core import mergerequests, store
        mr = mergerequests.latest_for_task(task_id)
        if not mr or mr["status"] != "open":
            return
        check = mergerequests.check_mergeability(
            mr["bare_path"], mr["source_path"], mr["source_branch"], mr["target_branch"])
        if not (check.get("ok") and check.get("mergeable")):
            logger.info("auto-merge skipped for %s — not cleanly mergeable", task_id)
            return
        res = mergerequests.merge_branch(mr, mr["target_branch"])
        if res.get("ok"):
            mergerequests.mark_merged(mr["id"], res["sha"])
            store.post_message(task_id, "system", kind="note",
                               text=f"Auto-merged {mr['source_branch']} into {res['target']} ({res['sha'][:8]})")
            from core.worker import cleanup_task
            cleanup_task(task_id)
        else:
            logger.info("auto-merge failed for %s — %s", task_id, res.get("error"))
    except Exception:  # noqa: BLE001
        logger.exception("auto-merge errored for task %s", task_id)


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
    except Exception as e:
        logger.debug("_find_json: JSON parse failed: %s", e)
        pass
    for m in reversed(re.findall(r"\{.*\}", out, re.DOTALL)):
        try:
            return json.loads(m)
        except Exception as e:
            logger.debug("_find_json: regex extraction failed: %s", e)
            continue
    return None
