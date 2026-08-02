import asyncio
import logging
from pathlib import Path
from typing import List
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from core.db import init_db
from core.config import get_config
from core import mergerequests

router = APIRouter()
logger = logging.getLogger(__name__)

def _tmpl():
    return Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_started = False


@router.on_event("startup")
async def startup():
    global _started
    if _started:
        logger.warning("startup() fired again — skipping (avoids a duplicate dispatch loop)")
        return
    _started = True

    from core.runners import init_runners_table
    from core.agents import init_agents_table
    from core.usage import init_usage_table
    from core.mergerequests import init_mr_table
    init_db()
    init_runners_table()  # daemon is self-sufficient: seed the runners table
    init_agents_table()   # and the agents (personas + tool allowlists)
    init_usage_table()    # and the per-run token/cost ledger
    init_mr_table()       # and the local merge-request ledger

    import web.agent_mcp as agent_mcp
    from core import orchestrator
    agent_mcp.on_worker_finished = lambda task_id: orchestrator.emit(task_id, "worker_finished")
    reconciled = orchestrator.reconcile_on_boot()
    if reconciled:
        logger.info("orchestrator: resumed %d task(s) orphaned by restart", reconciled)

    from core import preflight
    pf = preflight.check()
    if pf["ok"]:
        logger.info("preflight: ready (runner=%s)", pf["runner"])
    else:
        logger.warning("preflight: NOT ready to run work — %s", pf["reason"])

    asyncio.create_task(orchestrator.dispatch_forever())

@router.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse('<meta http-equiv="refresh" content="0;url=/dashboard">')

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    return _tmpl().TemplateResponse("dashboard.html", {"request": request, "active_page": "dashboard"})

@router.get("/api/dashboard/view", response_class=HTMLResponse)
async def dashboard_view(request: Request):
    from core.stats import dashboard
    return _tmpl().TemplateResponse("dashboard_view.html", {"request": request, "d": dashboard()})

@router.get("/api/notifications/poll")
async def notifications_poll():
    """Recent task state changes worth a browser notification: tasks that just
    finished (done/failed) or now need input (waiting). Bounded to the last 15
    minutes so the payload stays small; the client dedupes by key."""
    from datetime import datetime, timedelta, timezone
    from core import store

    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    items = []
    for status in ("waiting", "done", "failed"):
        for t in store.list_tasks(status=status):
            if t.get("updated_at", "") >= cutoff:
                items.append({
                    "key": f"{t['id']}:{status}:{t.get('updated_at', '')}",
                    "task_id": t["id"],
                    "status": status,
                    "goal": t["goal"],
                })
    return {"items": items}

@router.get("/runtime", response_class=HTMLResponse)
async def runtime_page(request: Request):
    import os
    from core.runners import list_runners

    cfg = get_config()

    # Actual workspace root the daemon is running with (set via env in app.py),
    # not the bare config default which is misleading.
    workspace_root = os.environ.get("WORKSPACE_ROOT") or cfg.get("workspace_root") or "(unset)"

    # The default runner is what actually drives agents now (raw LLM HTTP was
    # removed), so surface it instead of the stale seed-only default_model.
    default_runner = None
    for r in list_runners():
        if r.get("is_default"):
            default_runner = r
            break

    # Only show integration base URLs that are actually configured — unresolved
    # ${ENV} placeholders are noise.
    def _clean(v):
        v = (v or "").strip()
        return v if v and not v.startswith("${") else None

    integrations = {}
    for key in ("jira", "gitlab", "github"):
        url = _clean((cfg.get(key) or {}).get("base_url"))
        if url:
            integrations[key] = url

    return _tmpl().TemplateResponse(
        "runtime.html",
        {
            "request": request,
            "active_page": "runtime",
            "config": cfg,
            "workspace_root": workspace_root,
            "default_runner": default_runner,
            "integrations": integrations,
        },
    )

@router.get("/repos", response_class=HTMLResponse)
async def repos_page(request: Request):
    return _tmpl().TemplateResponse("repos.html", {"request": request, "active_page": "repos"})

@router.get("/api/repos/list", response_class=HTMLResponse)
async def repos_list(request: Request):
    from core.repos import list_repos
    return _tmpl().TemplateResponse("repos_list.html", {"request": request, "repos": list_repos()})

@router.get("/api/repos/new-form", response_class=HTMLResponse)
async def repos_new_form(request: Request):
    return _tmpl().TemplateResponse("repos_new.html", {"request": request})

@router.post("/api/repos")
async def repos_add(name: str = Form(...), source: str = Form(...), platform: str = Form(""), branch: str = Form("main")):
    from core.repos import add_repo
    from fastapi.responses import Response
    try:
        add_repo(name, source, platform, branch)
        resp = Response(status_code=200)
        resp.headers["HX-Trigger"] = "reposUpdated"
        return resp
    except Exception as e:
        return HTMLResponse(f'<div class="error-banner">Error: {e}</div>')

@router.get("/api/repos/health")
async def repos_health():
    from core.repos import list_repos
    repos = list_repos()
    total = len(repos)
    cloned = sum(1 for r in repos if r.get("cloned"))
    return {"total": total, "cloned": cloned, "pending": total - cloned}



@router.get("/api/repos/{name}/edit-form", response_class=HTMLResponse)
async def repos_edit_form(request: Request, name: str):
    from core.repos import get_repo
    repo = get_repo(name)
    if not repo:
        return HTMLResponse("<p>Repo not found</p>")
    return _tmpl().TemplateResponse("repos_edit.html", {"request": request, "repo": repo})

@router.post("/api/repos/{name}/edit")
async def repos_edit(name: str, source: str = Form(""), platform: str = Form(""), branch: str = Form("")):
    from core.repos import update_repo_config
    from fastapi.responses import Response
    update_repo_config(name, source or None, platform or None, branch or None)
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "reposUpdated"
    return resp


@router.delete("/api/repos/{name}")
@router.post("/api/repos/{name}/delete")
async def repos_delete(name: str):
    from core.repos import delete_repo
    from fastapi.responses import Response
    delete_repo(name)
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "reposUpdated"
    return resp


@router.post("/api/repos/{name}/update")
async def repos_update(name: str):
    from core.repos import update_repo
    from fastapi.responses import Response
    update_repo(name)
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "reposUpdated"
    return resp

# ── Runner CRUD ────────────────────────────────────────────────────────

@router.get("/backups", response_class=HTMLResponse)
async def backups_page(request: Request):
    return _tmpl().TemplateResponse("backups.html", {"request": request, "active_page": "backups"})

@router.get("/api/backups/list", response_class=HTMLResponse)
async def backups_list_view(request: Request):
    from core import backup
    return _tmpl().TemplateResponse(
        "backups_list.html",
        {"request": request, "backups": backup.list_backups(), "retention": backup.RETENTION},
    )

def _backups_list_response(request, flash=None, error=None):
    from core import backup
    resp = _tmpl().TemplateResponse(
        "backups_list.html",
        {"request": request, "backups": backup.list_backups(),
         "retention": backup.RETENTION, "flash": flash, "error": error},
    )
    return resp

@router.post("/api/backups/create", response_class=HTMLResponse)
async def backups_create(request: Request):
    from core import backup
    try:
        info = backup.snapshot(label="manual")
        return _backups_list_response(request, flash=f"Backup created — {info['name']} ({info['size_h']})")
    except Exception as exc:  # noqa: BLE001
        return _backups_list_response(request, error=f"Backup failed: {exc}")

@router.post("/api/backups/{name}/restore", response_class=HTMLResponse)
async def backups_restore(request: Request, name: str):
    from core import backup
    try:
        res = backup.restore(name)
        return _backups_list_response(
            request,
            flash=f"Restored from {res['restored']}. A safety copy of the previous state was saved as {res['safety']}.",
        )
    except Exception as exc:  # noqa: BLE001
        return _backups_list_response(request, error=f"Restore failed: {exc}")

@router.post("/api/backups/{name}/delete", response_class=HTMLResponse)
async def backups_delete(request: Request, name: str):
    from core import backup
    ok = backup.delete_backup(name)
    return _backups_list_response(
        request,
        flash=f"Deleted {name}" if ok else None,
        error=None if ok else f"Could not delete {name}",
    )

@router.get("/runners", response_class=HTMLResponse)
async def runners_page(request: Request):
    return _tmpl().TemplateResponse("runners.html", {"request": request, "active_page": "runners"})

@router.get("/api/runners/list", response_class=HTMLResponse)
async def runners_list_view(request: Request):
    from core.runners import list_runners
    return _tmpl().TemplateResponse("runners_list.html", {"request": request, "runners": list_runners()})

@router.get("/api/runners/new-form", response_class=HTMLResponse)
async def runners_new_form(request: Request):
    return _tmpl().TemplateResponse("runners_new.html", {"request": request})

@router.get("/api/runners/detect", response_class=HTMLResponse)
async def runners_detect_view(request: Request):
    from core.runner_detect import detect, resolve
    from core.runners import list_runners
    configured = list_runners()
    configured_bins = {r["binary"] for r in configured}
    detected = detect()
    for d in detected:
        d["configured"] = d["binary"] in configured_bins
    conf = [{"name": r["name"], "binary": r["binary"], "resolved": resolve(r["binary"])}
            for r in configured]
    return _tmpl().TemplateResponse(
        "runners_detect.html",
        {"request": request, "detected": detected, "configured": conf},
    )

@router.post("/api/runners")
async def runners_add(name: str = Form(...), binary: str = Form(...), type: str = Form("opencode"),
                       model: str = Form(""), api_key_env: str = Form(""), base_url: str = Form("")):
    from core.runners import add_runner
    from fastapi.responses import Response
    add_runner(name, binary, type, model, api_key_env, base_url)
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "runnersUpdated"
    return resp

@router.get("/api/runners/{runner_id}/edit-form", response_class=HTMLResponse)
async def runners_edit_form(request: Request, runner_id: str):
    from core.runners import get_runner
    runner = get_runner(runner_id)
    if not runner:
        return HTMLResponse("<p>Runner not found</p>")
    return _tmpl().TemplateResponse("runners_edit.html", {"request": request, "runner": runner})

@router.post("/api/runners/{runner_id}/edit")
async def runners_edit(runner_id: str, name: str = Form(""), binary: str = Form(""),
                        type: str = Form(""), model: str = Form(""), api_key_env: str = Form(""),
                        base_url: str = Form("")):
    from core.runners import update_runner
    from fastapi.responses import Response
    fields = {}
    if name: fields["name"] = name
    if binary: fields["binary"] = binary
    if type: fields["type"] = type
    if model: fields["model"] = model
    if api_key_env: fields["api_key_env"] = api_key_env
    if base_url: fields["base_url"] = base_url
    update_runner(runner_id, **fields)
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "runnersUpdated"
    return resp

@router.post("/api/runners/{runner_id}/delete")
async def runners_delete(runner_id: str):
    from core.runners import delete_runner
    from fastapi.responses import Response
    delete_runner(runner_id)
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "runnersUpdated"
    return resp

@router.post("/api/runners/{runner_id}/set-default")
async def runners_set_default(runner_id: str):
    from core.runners import set_default_runner
    from fastapi.responses import Response
    set_default_runner(runner_id)
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "runnersUpdated"
    return resp

@router.post("/api/runners/{runner_id}/health")
async def runners_health(runner_id: str):
    from core.runners import health_check
    from fastapi.responses import Response
    await health_check(runner_id)
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "runnersUpdated"
    return resp

# ── Statistics ──────────────────────────────────────────────────────────

@router.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request):
    return _tmpl().TemplateResponse("stats.html", {"request": request, "active_page": "stats"})

@router.get("/api/stats/view", response_class=HTMLResponse)
async def stats_view(request: Request):
    from core.stats import overview
    return _tmpl().TemplateResponse("stats_view.html", {"request": request, "s": overview()})

# ── Agents ──────────────────────────────────────────────────────────────

@router.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request):
    return _tmpl().TemplateResponse("agents.html", {"request": request, "active_page": "agents"})

@router.get("/api/agents/list", response_class=HTMLResponse)
async def agents_list_view(request: Request):
    from core.agents import list_agents
    from core.runners import list_runners
    runners = {r["id"]: r["name"] for r in list_runners()}
    return _tmpl().TemplateResponse("agents_list.html",
                                    {"request": request, "agents": list_agents(), "runners": runners})

@router.get("/api/agents/new-form", response_class=HTMLResponse)
async def agents_new_form(request: Request):
    from web.agent_mcp import TOOLS
    from core.runners import list_runners
    return _tmpl().TemplateResponse("agents_new.html",
                                    {"request": request, "catalog": list(TOOLS), "runners": list_runners()})

@router.post("/api/agents")
async def agents_add(name: str = Form(...), kind: str = Form("worker"), persona: str = Form(""),
                     model: str = Form(""), runner_id: str = Form(""), tools: List[str] = Form([])):
    from core.agents import add_agent, get_agent
    from fastapi.responses import Response
    if not name.strip() or get_agent(name):
        return Response(status_code=409)
    add_agent(name, persona, tools, runner_id or None, model, kind)
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "agentsUpdated"
    return resp

@router.get("/api/agents/{name}/edit-form", response_class=HTMLResponse)
async def agents_edit_form(request: Request, name: str):
    from core.agents import get_agent
    from web.agent_mcp import TOOLS
    from core.runners import list_runners
    agent = get_agent(name)
    if not agent:
        return HTMLResponse("not found", status_code=404)
    return _tmpl().TemplateResponse("agents_edit.html",
                                    {"request": request, "agent": agent,
                                     "catalog": list(TOOLS), "runners": list_runners()})

@router.post("/api/agents/{name}/edit")
async def agents_edit(name: str, persona: str = Form(""), model: str = Form(""),
                      runner_id: str = Form(""), tools: List[str] = Form([])):
    from core.agents import update_agent
    from fastapi.responses import Response
    update_agent(name, persona=persona, model=model, runner_id=runner_id or None, tools=tools)
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "agentsUpdated"
    return resp

@router.post("/api/agents/{name}/delete")
async def agents_delete(name: str):
    from core.agents import delete_agent
    from fastapi.responses import Response
    try:
        delete_agent(name)
    except ValueError:
        return Response(status_code=422)   # built-in — protected
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "agentsUpdated"
    return resp

# ── Tasks (Task+Thread system) ──────────────────────────────────────────

@router.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request):
    return _tmpl().TemplateResponse("tasks.html", {"request": request, "active_page": "tasks"})

@router.get("/api/tasks/list", response_class=HTMLResponse)
async def tasks_list_view(request: Request):
    from core import store
    columns = {}
    for status in store.VALID_STATUSES:
        tasks = [t for t in store.list_tasks(status=status) if not t.get("parent_id")]
        for t in tasks:   # annotate fan-out parents with child progress
            kids = store.list_children(t["id"])
            if kids:
                t["child_total"] = len(kids)
                t["child_done"] = sum(1 for c in kids if c["status"] == "done")
        columns[status] = tasks
    return _tmpl().TemplateResponse("tasks_list.html", {"request": request, "columns": columns})

@router.get("/api/tasks/new-form", response_class=HTMLResponse)
async def tasks_new_form(request: Request):
    from core.repos import list_repos
    from core.agents import list_agents
    return _tmpl().TemplateResponse("tasks_new.html", {
        "request": request, "repos": list_repos(),
        "agents": list_agents(kind="worker"),
    })

@router.post("/api/tasks")
async def tasks_create(goal: str = Form(...), repo: str = Form(""), agent: str = Form("")):
    from core import store
    from core.agents import get_agent
    from core.orchestrator import emit
    from fastapi.responses import Response

    if not goal.strip():
        # htmx doesn't swap on non-2xx; this surfaces via the global error
        # toast (base.html's htmx:afterRequest handler).
        return Response(status_code=422)
    if agent.strip():
        a = get_agent(agent)
        if not a or a["kind"] != "worker":
            return Response(status_code=422)
    # Repo optional — repo-less tasks run in a scratch dir (see web/api.py).
    task = store.create_task(goal, repo, agent=agent or None)
    emit(task["id"], "created")
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "taskCreated"
    return resp

@router.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_page(request: Request, task_id: str):
    from core import store

    task = store.get_task(task_id)
    if not task:
        return HTMLResponse('<div class="detail-panel"><p style="color:#f85149">Task not found</p></div>', status_code=404)
    return _tmpl().TemplateResponse("task_detail.html", {
        "request": request, "active_page": "tasks",
        "task": task, "messages": store.get_thread(task_id),
        "result": store.result_view(task_id),
        "children": store.list_children(task_id),
        "parent": store.get_task(task["parent_id"]) if task.get("parent_id") else None,
        "mr": mergerequests.latest_for_task(task_id),
    })

def _classify_diff(text: str):
    """Split a unified diff into (class, line) pairs + add/del counts, for
    colourised rendering. Shared by the diff dialog and the MR view."""
    lines = []
    for ln in (text or "").splitlines():
        if ln.startswith("+") and not ln.startswith("+++"):
            cls = "add"
        elif ln.startswith("-") and not ln.startswith("---"):
            cls = "del"
        elif ln.startswith("@@"):
            cls = "hunk"
        elif ln.startswith(("diff --git", "index ", "+++ ", "--- ", "new file", "deleted file", "old mode", "new mode", "similarity", "rename ")):
            cls = "meta"
        else:
            cls = "ctx"
        lines.append((cls, ln))
    adds = sum(1 for c, _ in lines if c == "add")
    dels = sum(1 for c, _ in lines if c == "del")
    return lines, adds, dels


@router.get("/api/tasks/{task_id}/diff/{filename}", response_class=HTMLResponse)
async def task_diff_modal(request: Request, task_id: str, filename: str):
    """Render a task attachment (a diff patch / any text) in a dialog, with diff
    syntax colouring — so the change opens in-app, not a separate browser tab."""
    from core import attachments
    text = attachments.read(task_id, filename)
    if text is None:
        return HTMLResponse('<div class="modal-backdrop" onclick="this.remove()"><div class="modal" onclick="event.stopPropagation()"><div class="modal__body"><div class="empty">Attachment not found.</div></div></div></div>')
    lines, adds, dels = _classify_diff(text)
    return _tmpl().TemplateResponse("diff_modal.html", {
        "request": request, "filename": filename, "lines": lines,
        "adds": adds, "dels": dels,
    })


# ── Local Merge Requests ────────────────────────────────────────────────

def _mr_view_ctx(request, mr):
    """Build the MR-view context: branches, mergeability, and the colourised diff."""
    from core import attachments, mergerequests
    branches = mergerequests.list_branches(mr["source_path"])
    merge = mergerequests.check_mergeability(
        mr["bare_path"], mr["source_path"], mr["source_branch"], mr["target_branch"])
    patch = attachments.read(mr["task_id"], "change.patch") or ""
    lines, adds, dels = _classify_diff(patch)
    return {"request": request, "mr": mr, "branches": branches, "merge": merge,
            "lines": lines, "adds": adds, "dels": dels}

@router.get("/api/mr/{mr_id}/view", response_class=HTMLResponse)
async def mr_view(request: Request, mr_id: str):
    from core import mergerequests
    mr = mergerequests.get_mr(mr_id)
    if not mr:
        return HTMLResponse('<div class="modal-backdrop" onclick="this.remove()"><div class="modal"><div class="modal__body"><div class="empty">Merge request not found.</div></div></div></div>')
    return _tmpl().TemplateResponse("mr_modal.html", _mr_view_ctx(request, mr))

@router.post("/api/mr/{mr_id}/target", response_class=HTMLResponse)
async def mr_set_target(request: Request, mr_id: str, target: str = Form(...)):
    from core import mergerequests
    mr = mergerequests.set_target(mr_id, target)
    return _tmpl().TemplateResponse("mr_modal.html", _mr_view_ctx(request, mr))

@router.post("/api/mr/{mr_id}/land", response_class=HTMLResponse)
async def mr_land(request: Request, mr_id: str):
    from core import mergerequests, store
    from fastapi.responses import Response
    mr = mergerequests.get_mr(mr_id)
    if not mr:
        return Response(status_code=404)
    res = mergerequests.land_branch(mr)
    if res.get("ok"):
        store.post_message(mr["task_id"], "system", kind="note",
                           text=f"Landed branch {mr['source_branch']} into {mr['source_path']}")
        mr = mergerequests.get_mr(mr_id)
        ctx = _mr_view_ctx(request, mr)
        ctx["flash"] = f"Landed {mr['source_branch']} — review it in your repo (git checkout {mr['source_branch']})"
    else:
        ctx = _mr_view_ctx(request, mr)
        ctx["error"] = res.get("error", "land failed")
    return _tmpl().TemplateResponse("mr_modal.html", ctx)

@router.post("/api/mr/{mr_id}/merge", response_class=HTMLResponse)
async def mr_merge(request: Request, mr_id: str):
    from core import mergerequests, store
    from fastapi.responses import Response
    mr = mergerequests.get_mr(mr_id)
    if not mr:
        return Response(status_code=404)
    if mr["status"] != "open":
        ctx = _mr_view_ctx(request, mr); ctx["error"] = f"MR is already {mr['status']}"
        return _tmpl().TemplateResponse("mr_modal.html", ctx)
    res = mergerequests.merge_branch(mr, mr["target_branch"])
    if res.get("ok"):
        mergerequests.mark_merged(mr_id, res["sha"])
        store.post_message(mr["task_id"], "system", kind="note",
                           text=f"Merged {mr['source_branch']} into {res['target']} ({res['sha'][:8]}) in {mr['source_path']}")
        from core.worker import cleanup_task
        cleanup_task(mr["task_id"])   # MR resolved — reclaim the per-task bare clone
        mr = mergerequests.get_mr(mr_id)
        ctx = _mr_view_ctx(request, mr)
        ctx["flash"] = f"Merged into {res['target']} as {res['sha'][:8]} — reversible with git reset --hard @{{1}}"
    else:
        ctx = _mr_view_ctx(request, mr); ctx["error"] = res.get("error", "merge failed")
    return _tmpl().TemplateResponse("mr_modal.html", ctx)

@router.post("/api/mr/{mr_id}/close", response_class=HTMLResponse)
async def mr_close(request: Request, mr_id: str):
    from core import mergerequests
    from fastapi.responses import Response
    mr = mergerequests.get_mr(mr_id)
    if not mr:
        return Response(status_code=404)
    mergerequests.set_status(mr_id, "closed")
    from core.worker import cleanup_task
    cleanup_task(mr["task_id"])   # MR resolved — reclaim the per-task bare clone
    return _tmpl().TemplateResponse("mr_modal.html", _mr_view_ctx(request, mergerequests.get_mr(mr_id)))

@router.get("/api/tasks/{task_id}/detail", response_class=HTMLResponse)
async def task_detail_view(request: Request, task_id: str):
    from core import store

    task = store.get_task(task_id)
    if not task:
        return HTMLResponse('<div class="detail-panel"><p style="color:#f85149">Task not found</p></div>')
    return _tmpl().TemplateResponse("task_detail_content.html", {
        "request": request, "task": task, "messages": store.get_thread(task_id),
        "result": store.result_view(task_id),
        "children": store.list_children(task_id),
        "parent": store.get_task(task["parent_id"]) if task.get("parent_id") else None,
        "mr": mergerequests.latest_for_task(task_id),
    })

@router.post("/api/tasks/{task_id}/messages")
async def task_reply(task_id: str, text: str = Form(...)):
    from core import store
    from core.orchestrator import emit
    from fastapi.responses import Response

    text = text.strip()
    if not text:
        return Response(status_code=422)
    if not store.get_task(task_id):
        return Response(status_code=404)

    store.post_message(task_id, "human", kind="answer", text=text)
    emit(task_id, "human_replied")
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "taskUpdated"
    return resp

@router.post("/api/tasks/{task_id}/delete")
async def task_delete(task_id: str):
    from core import sessions, store
    from core.worker import cleanup_task
    from fastapi.responses import Response

    if not store.get_task(task_id):
        return Response(status_code=404)
    tree = store.task_tree(task_id)   # root + fan-out descendants
    for tid in tree:
        sessions.revoke_task(tid)
    store.delete_task(task_id)        # cascades the whole subtree in the DB
    for tid in tree:
        try:
            cleanup_task(tid, deep=True)
        except Exception:
            pass
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = "/tasks"   # navigate back to the board
    return resp
