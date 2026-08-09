import asyncio
import logging
from pathlib import Path
from typing import List
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from core.db import init_db
from core.config import get_config
from core import mergerequests, sessions
from web.markdown import render_md

router = APIRouter()
logger = logging.getLogger(__name__)

def _tmpl():
    from core.store import task_display_title, message_lane
    tmpl = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    tmpl.env.globals["task_display_title"] = task_display_title
    tmpl.env.globals["message_lane"] = message_lane
    tmpl.env.filters["md"] = render_md
    return tmpl

_started = False


def _enrich_sessions(raw_sessions):
    from datetime import datetime, timezone
    from core import store

    now = datetime.now(timezone.utc)
    enriched = []
    for s in raw_sessions:
        s = dict(s)
        try:
            started = datetime.fromisoformat(s["created_at"])
            s["elapsed_mins"] = int((now - started).total_seconds() // 60)
        except Exception:
            s["elapsed_mins"] = 0
        task = store.get_task(s["task_id"])
        s["task_title"] = store.task_display_title(task) if task else (s["task_id"] or "")
        enriched.append(s)
    return enriched


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
    from core.autopilot import init_autopilot_table
    from core.copilot_store import init_copilot_tables
    from core.planner import init_planner_tables
    init_db()
    init_runners_table()  # daemon is self-sufficient: seed the runners table
    init_agents_table()   # and the agents (personas + tool allowlists)
    init_usage_table()    # and the per-run token/cost ledger
    init_mr_table()       # and the local merge-request ledger
    init_autopilot_table()  # and the autopilot automations
    init_copilot_tables()   # and the copilot chat sessions
    init_planner_tables()   # and the mission-planner plans/items

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

    from core import autopilot
    asyncio.create_task(autopilot.run_forever())

@router.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse('<meta http-equiv="refresh" content="0;url=/dashboard">')

@router.get("/system", response_class=HTMLResponse)
async def system_page():
    """The System door in the 3-item nav — lands on its first tab."""
    return HTMLResponse('<meta http-equiv="refresh" content="0;url=/agents">')

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    return _tmpl().TemplateResponse(request, "dashboard.html", {"active_page": "dashboard"})

@router.get("/api/dashboard/view", response_class=HTMLResponse)
async def dashboard_view(request: Request):
    from core.stats import dashboard
    return _tmpl().TemplateResponse(request, "dashboard_view.html", {
        "d": dashboard(),
        "live_sessions": _enrich_sessions(sessions.list_active()),
    })

@router.get("/api/inbox/view", response_class=HTMLResponse)
async def inbox_view(request: Request):
    """The Needs-you feed: waiting tasks (with the open question + inline reply)
    and done tasks whose merge request is still open."""
    from core import store

    waiting = []
    for t in store.list_tasks(status="waiting"):
        q = next((m["text"] for m in reversed(store.get_thread(t["id"]))
                  if m["kind"] == "question"), "")
        waiting.append({"task": t, "question": q[:120] + "…" if len(q) > 120 else q})

    open_mr = {}
    for mr in mergerequests.list_mrs(status="open"):
        open_mr.setdefault(mr["task_id"], mr)   # newest first — keep the latest
    to_merge = [{"task": t, "mr": open_mr[t["id"]]}
                for t in store.list_tasks(status="done") if t["id"] in open_mr]

    return _tmpl().TemplateResponse(request, "inbox_view.html",
                                    {"waiting": waiting, "to_merge": to_merge})

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
        request,
        "runtime.html",
        {
            "active_page": "runtime",
            "config": cfg,
            "workspace_root": workspace_root,
            "default_runner": default_runner,
            "integrations": integrations,
        },
    )

@router.get("/repos", response_class=HTMLResponse)
async def repos_page(request: Request):
    return _tmpl().TemplateResponse(request, "repos.html", {"active_page": "repos"})

@router.get("/api/repos/list", response_class=HTMLResponse)
async def repos_list(request: Request):
    from core.repos import list_repos
    from core import store
    repos = list_repos()
    tasks = [t for t in store.list_tasks() if not t.get("parent_id")]
    for r in repos:  # annotate each repo with its task progress (Projects-style)
        rt = [t for t in tasks if t.get("repo") == r["name"]]
        r["task_total"] = len(rt)
        r["task_done"] = sum(1 for t in rt if t["status"] == "done")
    return _tmpl().TemplateResponse(request, "repos_list.html", {"repos": repos})

@router.get("/repos/{name}", response_class=HTMLResponse)
async def repo_detail_page(request: Request, name: str):
    from core.repos import get_repo, REPOS_DIR
    from core import repo_context, store
    repo = get_repo(name)
    if not repo:
        return HTMLResponse('<div class="empty">Repository not found</div>', status_code=404)
    bare = REPOS_DIR / name
    repo["cloned"] = bare.exists() and (bare / "HEAD").exists()
    source = repo.get("source", "")
    tasks = [t for t in store.list_tasks() if t.get("repo") == name and not t.get("parent_id")]
    return _tmpl().TemplateResponse(request, "repos_detail.html", {
        "active_page": "repos", "repo": repo,
        "resources": repo_context.resources(source),
        "summary": repo_context.summary(source),
        "tasks": tasks,
        "done": sum(1 for t in tasks if t["status"] == "done"),
    })

@router.get("/api/repos/{name}/resource", response_class=HTMLResponse)
async def repo_resource_view(request: Request, name: str, path: str):
    from core.repos import get_repo
    from core import repo_context
    repo = get_repo(name)
    content = repo_context.read_resource(repo["source"], path) if repo else None
    return _tmpl().TemplateResponse(request, "repo_resource_modal.html",
        {"name": name, "path": path, "content": content})

@router.get("/api/repos/new-form", response_class=HTMLResponse)
async def repos_new_form(request: Request):
    return _tmpl().TemplateResponse(request, "repos_new.html", {})

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
    return _tmpl().TemplateResponse(request, "repos_edit.html", {"repo": repo})

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
    return _tmpl().TemplateResponse(request, "backups.html", {"active_page": "backups"})

@router.get("/api/backups/list", response_class=HTMLResponse)
async def backups_list_view(request: Request):
    from core import backup
    return _tmpl().TemplateResponse(
        request,
        "backups_list.html",
        {"backups": backup.list_backups(), "retention": backup.RETENTION},
    )

def _backups_list_response(request, flash=None, error=None):
    from core import backup
    resp = _tmpl().TemplateResponse(
        request,
        "backups_list.html",
        {"backups": backup.list_backups(),
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

@router.get("/autopilots", response_class=HTMLResponse)
async def autopilots_page(request: Request):
    return _tmpl().TemplateResponse(request, "autopilots.html", {"active_page": "autopilots"})

def _autopilots_list_response(request, flash=None, error=None):
    from core.autopilot import list_autopilots
    return _tmpl().TemplateResponse(
        request,
        "autopilots_list.html",
        {"autopilots": list_autopilots(), "flash": flash, "error": error},
    )

@router.get("/api/autopilots/list", response_class=HTMLResponse)
async def autopilots_list_view(request: Request):
    return _autopilots_list_response(request)

@router.get("/api/autopilots/new-form", response_class=HTMLResponse)
async def autopilots_new_form(request: Request):
    from core.repos import list_repos
    from core.agents import list_agents
    return _tmpl().TemplateResponse(request, "autopilots_form.html",
        {"ap": None, "repos": list_repos(), "agents": list_agents(kind="worker")})

@router.get("/api/autopilots/{ap_id}/edit-form", response_class=HTMLResponse)
async def autopilots_edit_form(request: Request, ap_id: str):
    from core.autopilot import get_autopilot
    from core.repos import list_repos
    from core.agents import list_agents
    return _tmpl().TemplateResponse(request, "autopilots_form.html",
        {"ap": get_autopilot(ap_id), "repos": list_repos(), "agents": list_agents(kind="worker")})

def _ap_form_fields(name, description, trigger_type, cron, interval_seconds,
                    webhook_platform, webhook_event, goal_template, repo, agent, skip_if_running):
    return dict(name=name, description=description, trigger_type=trigger_type, cron=cron,
                interval_seconds=int(interval_seconds or 0), webhook_platform=webhook_platform,
                webhook_event=webhook_event, goal_template=goal_template, repo=repo,
                agent=agent, skip_if_running=1 if skip_if_running else 0)

@router.post("/api/autopilots", response_class=HTMLResponse)
async def autopilots_create(request: Request, name: str = Form(...), description: str = Form(""),
        trigger_type: str = Form("schedule"), cron: str = Form(""), interval_seconds: str = Form("0"),
        webhook_platform: str = Form(""), webhook_event: str = Form(""), goal_template: str = Form(""),
        repo: str = Form(""), agent: str = Form(""), skip_if_running: str = Form("")):
    from core.autopilot import create_autopilot
    create_autopilot(**_ap_form_fields(name, description, trigger_type, cron, interval_seconds,
        webhook_platform, webhook_event, goal_template, repo, agent, skip_if_running))
    return _autopilots_list_response(request, flash=f"Created “{name}”")

@router.post("/api/autopilots/{ap_id}", response_class=HTMLResponse)
async def autopilots_update(request: Request, ap_id: str, name: str = Form(...), description: str = Form(""),
        trigger_type: str = Form("schedule"), cron: str = Form(""), interval_seconds: str = Form("0"),
        webhook_platform: str = Form(""), webhook_event: str = Form(""), goal_template: str = Form(""),
        repo: str = Form(""), agent: str = Form(""), skip_if_running: str = Form("")):
    from core.autopilot import update_autopilot
    update_autopilot(ap_id, **_ap_form_fields(name, description, trigger_type, cron, interval_seconds,
        webhook_platform, webhook_event, goal_template, repo, agent, skip_if_running))
    return _autopilots_list_response(request, flash=f"Saved “{name}”")

@router.post("/api/autopilots/{ap_id}/run", response_class=HTMLResponse)
async def autopilots_run(request: Request, ap_id: str):
    from core.autopilot import get_autopilot, fire
    ap = get_autopilot(ap_id)
    if not ap:
        return _autopilots_list_response(request, error="autopilot not found")
    tid = fire(ap)
    return _autopilots_list_response(request,
        flash=(f"Fired → task {tid}" if tid else "Skipped — previous run still running"))

@router.post("/api/autopilots/{ap_id}/toggle", response_class=HTMLResponse)
async def autopilots_toggle(request: Request, ap_id: str):
    from core.autopilot import get_autopilot, set_enabled
    ap = get_autopilot(ap_id)
    if ap:
        set_enabled(ap_id, not ap["enabled"])
    return _autopilots_list_response(request)

@router.post("/api/autopilots/{ap_id}/delete", response_class=HTMLResponse)
async def autopilots_delete(request: Request, ap_id: str):
    from core.autopilot import delete_autopilot
    delete_autopilot(ap_id)
    return _autopilots_list_response(request)

@router.get("/runners", response_class=HTMLResponse)
async def runners_page(request: Request):
    return _tmpl().TemplateResponse(request, "runners.html", {"active_page": "runners"})

@router.get("/api/runners/list", response_class=HTMLResponse)
async def runners_list_view(request: Request):
    from core.runners import list_runners
    return _tmpl().TemplateResponse(request, "runners_list.html", {"runners": list_runners()})

@router.get("/api/runners/new-form", response_class=HTMLResponse)
async def runners_new_form(request: Request):
    return _tmpl().TemplateResponse(request, "runners_new.html", {})

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
        request,
        "runners_detect.html",
        {"detected": detected, "configured": conf},
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
    return _tmpl().TemplateResponse(request, "runners_edit.html", {"runner": runner})

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
    return _tmpl().TemplateResponse(request, "stats.html", {"active_page": "stats"})

@router.get("/api/stats/view", response_class=HTMLResponse)
async def stats_view(request: Request):
    from core.stats import overview
    return _tmpl().TemplateResponse(request, "stats_view.html", {"s": overview()})

# ── Live Workers ─────────────────────────────────────────────────────────

@router.get("/api/workers/live", response_class=HTMLResponse)
async def workers_live(request: Request):
    return _tmpl().TemplateResponse(request, "workers_live.html",
                                    {"sessions": _enrich_sessions(sessions.list_active())})

# ── Agents ──────────────────────────────────────────────────────────────

@router.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request):
    return _tmpl().TemplateResponse(request, "agents.html", {"active_page": "agents"})

@router.get("/api/agents/list", response_class=HTMLResponse)
async def agents_list_view(request: Request):
    from core.agents import list_agents
    from core.runners import list_runners
    runners = {r["id"]: r["name"] for r in list_runners()}
    return _tmpl().TemplateResponse(request, "agents_list.html",
                                    {"agents": list_agents(), "runners": runners})

@router.get("/api/agents/new-form", response_class=HTMLResponse)
async def agents_new_form(request: Request):
    from web.agent_mcp import TOOLS
    from core.runners import list_runners
    return _tmpl().TemplateResponse(request, "agents_new.html",
                                    {"catalog": list(TOOLS), "runners": list_runners()})

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
    return _tmpl().TemplateResponse(request, "agents_edit.html",
                                    {"agent": agent,
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

def _enrich_now_card(t, mr, status):
    """Rich-card data for the NOW zone: pipeline stage dots, the last dialogue
    event, cost, and the pending question. One thread read per live card —
    the NOW zone holds a handful of tasks, not the whole history."""
    from core import store, usage

    th = store.get_thread(t["id"])
    has_commit = any(m["kind"] == "commit" for m in th)
    reviewed = any(m["kind"] == "result" and m["author"] not in ("engineer", "human")
                   and (m["text"].startswith("APPROVED") or m["text"].startswith("CHANGES REQUESTED"))
                   for m in th)
    merged = bool(mr and mr.get("status") == "merged")
    t["stages"] = [
        ("explore", bool(th)),
        ("code", has_commit),
        ("review", reviewed),
        ("merge", merged),
    ]
    t["stage_now"] = next((name for name, done in t["stages"] if not done), "merge")

    for m in reversed(th):
        if store.message_lane(m) == "dialogue" and m["text"]:
            txt = m["text"].split("\n", 1)[0]
            t["last_event"] = (txt[:70] + "…") if len(txt) > 70 else txt
            t["last_author"] = m["author"]
            break

    if status == "waiting":
        qs = [m for m in th if m["kind"] == "question"]
        if qs:
            q = qs[-1]["text"]
            t["question"] = (q[:90] + "…") if len(q) > 90 else q

    cost = usage.task_usage(t["id"])["cost"]
    t["cost"] = f"${cost:.2f}" if cost else None


def board_column(task, mr):
    """Derived board column from task status + latest merge-request state."""
    status = task.get("status", "")
    if status == "active":
        return "in-progress"
    if status == "waiting":
        return "needs-you"
    if status == "done":
        if mr is not None:
            if mr.get("status") == "merged":
                return "shipped"
            if mr.get("status") == "open":
                return "to-merge"
        return "done"
    if status == "failed":
        return "failed"
    return "done"


@router.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request):
    return _tmpl().TemplateResponse(request, "tasks.html", {"active_page": "tasks"})

@router.get("/api/tasks/list", response_class=HTMLResponse)
async def tasks_list_view(request: Request):
    from core import store

    all_mrs = mergerequests.list_mrs()
    task_latest_mr = {}
    for mr in all_mrs:
        tid = mr["task_id"]
        if tid not in task_latest_mr or mr["created_at"] > task_latest_mr[tid]["created_at"]:
            task_latest_mr[tid] = mr

    # Board cards are read at a glance — enrich them with what a glance needs:
    # who is working RIGHT NOW (live session), what a waiting task actually
    # asks, and a snippet of the goal beyond the title line.
    live_by_task = {s["task_id"]: s for s in _enrich_sessions(sessions.list_active())}

    cols = ("in-progress", "needs-you", "to-merge", "shipped", "done", "failed")
    columns = {c: [] for c in cols}
    for status in store.VALID_STATUSES:
        tasks = [t for t in store.list_tasks(status=status) if not t.get("parent_id")]
        for t in tasks:
            kids = store.list_children(t["id"])
            if kids:
                t["child_total"] = len(kids)
                t["child_done"] = sum(1 for c in kids if c["status"] == "done")
            mr = task_latest_mr.get(t["id"])
            t["review_status"] = mr.get("review_status") if mr and mr.get("review_status") else None

            goal = t.get("goal", "")
            rest = goal.split("\n", 1)[1].strip() if "\n" in goal else ""
            t["snippet"] = (rest[:110] + "…") if len(rest) > 110 else rest

            live = live_by_task.get(t["id"])
            if live and status == "active":
                t["live_role"] = live["role"]
                t["live_mins"] = live["elapsed_mins"]

            col = board_column(t, mr)
            if col in ("in-progress", "needs-you", "to-merge"):
                _enrich_now_card(t, mr, status)
            columns[col].append(t)
    return _tmpl().TemplateResponse(request, "tasks_list.html", {"columns": columns})

@router.get("/api/tasks/new-form", response_class=HTMLResponse)
async def tasks_new_form(request: Request):
    from core.repos import list_repos
    from core.agents import list_agents
    return _tmpl().TemplateResponse(request, "tasks_new.html", {
        "repos": list_repos(),
        "agents": list_agents(kind="worker"),
    })

@router.post("/api/tasks")
async def tasks_create(goal: str = Form(...), repo: str = Form(""), agent: str = Form(""),
                       title: str = Form("")):
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
    task = store.create_task(goal, repo, agent=agent or None, title=title or None)
    emit(task["id"], "created")
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "taskCreated"
    return resp

# ── Command Bar ─────────────────────────────────────────────────────────

@router.post("/api/command/classify", response_class=HTMLResponse)
async def command_classify(request: Request, text: str = Form(...)):
    from core import ai_assist
    from core.repos import list_repos

    if not text.strip():
        return HTMLResponse('<div class="panel" style="margin-top:12px"><div class="empty">Type something first.</div></div>')
    repos = list_repos()
    repo_names = [r["name"] for r in repos]
    try:
        result = await asyncio.to_thread(ai_assist.classify_command, text.strip(), repo_names)
    except Exception:
        result = {"kind": "task", "title": text.strip()[:80], "goal": text.strip(), "repo": "", "cron": ""}
    return _tmpl().TemplateResponse(request, "command_result.html", {"result": result, "repos": repos})


@router.post("/api/command/create-task")
async def command_create_task(goal: str = Form(...), repo: str = Form(""), title: str = Form("")):
    from core import store
    from core.orchestrator import emit
    from fastapi.responses import Response

    if not goal.strip():
        return Response(status_code=422)
    task = store.create_task(goal.strip(), repo, title=title or None)
    emit(task["id"], "created")
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = f"/tasks/{task['id']}"
    return resp


@router.post("/api/command/create-plan")
async def command_create_plan(goal: str = Form(...), repo: str = Form(""), title: str = Form("")):
    from core import planner
    from fastapi.responses import Response

    if not goal.strip():
        return Response(status_code=422)
    pid = planner.create_plan(title=title or goal.strip()[:48], repo=repo)["id"]
    planner.start_plan_turn(pid, goal.strip())
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = "/plans"
    return resp


@router.post("/api/command/create-automation")
async def command_create_automation(goal: str = Form(...), repo: str = Form(""),
                                     title: str = Form(""), cron: str = Form("")):
    from core.autopilot import create_autopilot
    from fastapi.responses import Response

    if not goal.strip():
        return Response(status_code=422)
    create_autopilot(
        name=title or goal.strip()[:48],
        description=goal.strip(),
        trigger_type="schedule",
        cron=cron or "",
        goal_template=goal.strip(),
        repo=repo,
        enabled=1,
    )
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = "/autopilots"
    return resp

@router.post("/api/tasks/ai-parse")
async def tasks_ai_parse(text: str = Form(...), template: str = Form("")):
    """Turn rough words (+ template) into {goal, repo, agent} via a quick local
    AI run. Returns JSON for the New Task form to fill in."""
    from fastapi.responses import JSONResponse
    from core import ai_assist
    from core.repos import list_repos
    from core.agents import list_agents

    if not text.strip():
        return JSONResponse({"error": "empty"}, status_code=422)
    try:
        return await asyncio.to_thread(ai_assist.parse_task, text, template, list_repos(), list_agents(kind="worker"))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=503)

def _copilot_panel(request, session):
    from core.copilot_store import get_messages
    return _tmpl().TemplateResponse(request, "copilot_panel.html",
        {"session": session, "messages": get_messages(session["id"])})

@router.get("/api/board/copilot", response_class=HTMLResponse)
async def copilot_load(request: Request):
    """The dock loads this on open — the latest session's messages (persisted)."""
    from core.copilot_store import latest_or_create
    return _copilot_panel(request, latest_or_create())

@router.get("/api/board/copilot/sessions", response_class=HTMLResponse)
async def copilot_sessions(request: Request):
    from core.copilot_store import list_sessions
    return _tmpl().TemplateResponse(request, "copilot_sessions.html",
        {"sessions": list_sessions()})

@router.post("/api/board/copilot/new", response_class=HTMLResponse)
async def copilot_new(request: Request):
    from core.copilot_store import create_session
    return _copilot_panel(request, create_session())

@router.get("/api/board/copilot/{sid}", response_class=HTMLResponse)
async def copilot_open_session(request: Request, sid: str):
    from core.copilot_store import get_session, latest_or_create
    return _copilot_panel(request, get_session(sid) or latest_or_create())

@router.post("/api/board/copilot/{sid}/delete", response_class=HTMLResponse)
async def copilot_delete_session(request: Request, sid: str):
    from core.copilot_store import delete_session, latest_or_create
    delete_session(sid)
    return _copilot_panel(request, latest_or_create())

@router.post("/api/board/chat")
async def board_chat(request: Request):
    """One copilot turn, persisted to a session. Returns {reply, action?, session_id};
    a proposed action is validated but executed only on confirm via /api/board/act."""
    from fastapi.responses import JSONResponse
    from core import copilot, copilot_store
    body = await request.json()
    message = (body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "empty"}, status_code=422)
    sid = body.get("session_id")
    if not sid or not copilot_store.get_session(sid):
        sid = copilot_store.latest_or_create()["id"]
    history = [{"role": m["role"], "text": m["text"]} for m in copilot_store.get_messages(sid)]
    copilot_store.add_message(sid, "user", message)
    try:
        result = await asyncio.to_thread(copilot.chat, message, history)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc), "session_id": sid}, status_code=503)
    copilot_store.add_message(sid, "assistant", result.get("reply", ""))
    result["session_id"] = sid
    return result

@router.post("/api/board/act")
async def board_act(request: Request):
    """Execute a user-confirmed copilot action; record the result in the session."""
    from fastapi.responses import JSONResponse
    from core import copilot, copilot_store
    body = await request.json()
    try:
        res = copilot.execute_action(body.get("action") or {})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=422)
    sid = body.get("session_id")
    if sid and copilot_store.get_session(sid):
        copilot_store.add_message(sid, "system", res)
    return {"result": res}

# ── Mission Planner ─────────────────────────────────────────────────────

@router.get("/plans", response_class=HTMLResponse)
async def plans_page(request: Request):
    return _tmpl().TemplateResponse(request, "plans.html", {"active_page": "plans"})

def _plan_ctx(request, plan, flash=None, error=None):
    from core import planner
    from core.repos import list_repos
    items = planner.list_items(plan["id"])
    by = {x["id"]: x for x in items}
    for it in items:
        it["state"] = planner.item_state(it, by)
    idx = {it["id"]: i + 1 for i, it in enumerate(items)}
    for it in items:
        it["after_nums"] = [idx[a] for a in it["after"] if a in idx]
    brief = plan.get("brief_obj")
    return {
        "request": request, "plan": plan, "brief": brief, "items": items,
        "messages": planner.list_messages(plan["id"]),
        "dag": planner.steps_dag_mermaid(brief["steps"]) if brief and brief.get("steps") else "",
        "repos": list_repos(), "plans": planner.list_plans(),
        "has_launched": any(it["status"] == "launched" for it in items),
        "generating": planner.is_generating(plan["id"]),
        "flash": flash, "error": error or planner.pop_error(plan["id"]),
    }

def _plan_panel(request, plan, **kw):
    return _tmpl().TemplateResponse(request, "plan_panel.html", _plan_ctx(request, plan, **kw))

@router.get("/api/plans/panel", response_class=HTMLResponse)
async def plans_panel_latest(request: Request):
    from core import planner
    return _plan_panel(request, planner.latest_plan() or planner.create_plan())

@router.get("/api/plans/{pid}", response_class=HTMLResponse)
async def plans_panel(request: Request, pid: str):
    from core import planner
    return _plan_panel(request, planner.get_plan(pid) or planner.latest_plan() or planner.create_plan())

@router.get("/api/plans/{pid}/items", response_class=HTMLResponse)
async def plans_items(request: Request, pid: str):
    from core import planner
    return _tmpl().TemplateResponse(request, "plan_items.html", _plan_ctx(request, planner.get_plan(pid) or planner.create_plan()))

@router.post("/api/plans/new", response_class=HTMLResponse)
async def plans_new(request: Request):
    from core import planner
    return _plan_panel(request, planner.create_plan())

@router.post("/api/plans/{pid}/repo", response_class=HTMLResponse)
async def plans_set_repo(request: Request, pid: str, repo: str = Form("")):
    from core import planner
    planner.update_plan(pid, repo=repo, brief="")  # a new project → drop the old brief
    return _plan_panel(request, planner.get_plan(pid))

@router.post("/api/plans/{pid}/toggle/{flag}", response_class=HTMLResponse)
async def plans_toggle(request: Request, pid: str, flag: str):
    from core import planner
    if flag not in ("auto_release", "auto_merge"):
        return _plan_panel(request, planner.get_plan(pid), error="unknown setting")
    plan = planner.get_plan(pid)
    new = 0 if (plan or {}).get(flag) else 1
    planner.update_plan(pid, **{flag: new})
    label = "Auto-release" if flag == "auto_release" else "Auto-merge"
    return _plan_panel(request, planner.get_plan(pid),
                       flash=f"{label} {'on' if new else 'off'}.")

@router.post("/api/plans/{pid}/plan", response_class=HTMLResponse)
async def plans_plan_turn(request: Request, pid: str, message: str = Form(...)):
    from core import planner
    if not message.strip():
        return _plan_panel(request, planner.get_plan(pid), error="Describe the task first.")
    planner.start_plan_turn(pid, message.strip())   # background; the panel polls
    return _plan_panel(request, planner.get_plan(pid))

@router.post("/api/plans/{pid}/convert", response_class=HTMLResponse)
async def plans_convert(request: Request, pid: str):
    from core import planner
    try:
        planner.convert_to_drafts(pid)
        return _plan_panel(request, planner.get_plan(pid), flash="Converted to draft tasks.")
    except Exception as exc:  # noqa: BLE001
        return _plan_panel(request, planner.get_plan(pid), error=str(exc))

@router.post("/api/plans/{pid}/launch/{iid}", response_class=HTMLResponse)
async def plans_launch(request: Request, pid: str, iid: str):
    from core import planner
    try:
        planner.launch_item(iid)
        return _plan_panel(request, planner.get_plan(pid), flash="Launched.")
    except Exception as exc:  # noqa: BLE001
        return _plan_panel(request, planner.get_plan(pid), error=str(exc))

@router.post("/api/plans/{pid}/item/{iid}/delete", response_class=HTMLResponse)
async def plans_item_delete(request: Request, pid: str, iid: str):
    from core import planner
    planner.delete_item(iid)
    return _plan_panel(request, planner.get_plan(pid))

@router.post("/api/plans/{pid}/delete", response_class=HTMLResponse)
async def plans_delete(request: Request, pid: str):
    from core import planner
    planner.delete_plan(pid)
    return _plan_panel(request, planner.latest_plan() or planner.create_plan())

@router.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_page(request: Request, task_id: str):
    from core import store

    task = store.get_task(task_id)
    if not task:
        return HTMLResponse('<div class="detail-panel"><p style="color:#f85149">Task not found</p></div>', status_code=404)
    from core import usage
    return _tmpl().TemplateResponse(request, "task_detail.html", {
        "active_page": "tasks",
        "task": task, "messages": store.get_thread(task_id),
        "result": store.result_view(task_id),
        "children": store.list_children(task_id),
        "parent": store.get_task(task["parent_id"]) if task.get("parent_id") else None,
        "mr": mergerequests.latest_for_task(task_id),
        "usage": usage.task_usage(task_id),
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
    return _tmpl().TemplateResponse(request, "diff_modal.html", {
        "filename": filename, "lines": lines,
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
    return _tmpl().TemplateResponse(request, "mr_modal.html", _mr_view_ctx(request, mr))

@router.post("/api/mr/{mr_id}/target", response_class=HTMLResponse)
async def mr_set_target(request: Request, mr_id: str, target: str = Form(...)):
    from core import mergerequests
    mr = mergerequests.set_target(mr_id, target)
    return _tmpl().TemplateResponse(request, "mr_modal.html", _mr_view_ctx(request, mr))

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
    return _tmpl().TemplateResponse(request, "mr_modal.html", ctx)

@router.post("/api/mr/{mr_id}/merge", response_class=HTMLResponse)
async def mr_merge(request: Request, mr_id: str):
    from core import mergerequests, store
    from fastapi.responses import Response
    mr = mergerequests.get_mr(mr_id)
    if not mr:
        return Response(status_code=404)
    if mr["status"] != "open":
        ctx = _mr_view_ctx(request, mr); ctx["error"] = f"MR is already {mr['status']}"
        return _tmpl().TemplateResponse(request, "mr_modal.html", ctx)
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
    return _tmpl().TemplateResponse(request, "mr_modal.html", ctx)

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
    return _tmpl().TemplateResponse(request, "mr_modal.html", _mr_view_ctx(request, mergerequests.get_mr(mr_id)))

@router.get("/api/tasks/{task_id}/detail", response_class=HTMLResponse)
async def task_detail_view(request: Request, task_id: str):
    from core import store

    task = store.get_task(task_id)
    if not task:
        return HTMLResponse('<div class="detail-panel"><p style="color:#f85149">Task not found</p></div>')
    from core import usage
    messages = store.get_thread(task_id)

    # Live "what is happening right now" for the header: a worker running or
    # queued, with elapsed minutes — the thread is silent while an agent works,
    # which reads as "stuck" without this.
    from datetime import datetime, timezone

    working = None
    if task["status"] == "active" and messages:
        last = messages[-1]
        if last["kind"] in ("worker_started", "worker_queued"):
            try:
                started = datetime.fromisoformat(last["created_at"])
                mins = int((datetime.now(timezone.utc) - started).total_seconds() // 60)
            except Exception:  # noqa: BLE001
                mins = 0
            role = last["text"].split(" ", 2)[1].rstrip(":") if last["kind"] == "worker_started" \
                else last["text"].split(" ", 1)[0]
            label = "working" if last["kind"] == "worker_started" else "queued for a slot"
            working = {"role": role, "label": label, "mins": mins}

    # Nothing running? Show what JUST ended for a few minutes — otherwise a
    # worker finishing makes the indicator vanish mid-glance and a refresh
    # reads as "did anything happen at all?".
    finished = None
    if working is None:
        for m in reversed(messages):
            if m["kind"] == "worker_finished":
                try:
                    ended = datetime.fromisoformat(m["created_at"])
                    mins = int((datetime.now(timezone.utc) - ended).total_seconds() // 60)
                except Exception:  # noqa: BLE001
                    break
                if mins <= 5:
                    finished = {"text": m["text"], "mins": mins}
                break

    live_sessions = sessions.list_for_task(task_id)

    return _tmpl().TemplateResponse(request, "task_detail_content.html", {
        "task": task, "messages": messages, "working": working, "finished": finished,
        "live_sessions": _enrich_sessions(live_sessions),
        "result": store.result_view(task_id),
        "children": store.list_children(task_id),
        "parent": store.get_task(task["parent_id"]) if task.get("parent_id") else None,
        "mr": mergerequests.latest_for_task(task_id),
        "usage": usage.task_usage(task_id),
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

@router.post("/api/tasks/{task_id}/reopen")
async def task_reopen(task_id: str):
    """Reopen a terminal task as *waiting*: the follow-up work continues on the
    same thread (and branch) once the human replies with what to do next —
    waiting is the right state because only a human reply should resume it."""
    from core import store
    from fastapi.responses import Response

    task = store.get_task(task_id)
    if not task:
        return Response(status_code=404)
    if task["status"] not in ("done", "failed"):
        return Response(status_code=409)

    store.set_status(task_id, "waiting")
    store.post_message(task_id, "system", kind="note",
                       text="reopened by the human — reply below to tell the orchestrator what to do next")
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
        except Exception as e:
            logger.warning("cleanup failed for task %s during delete: %s", tid, e)
            pass
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = "/tasks"   # navigate back to the board
    return resp
