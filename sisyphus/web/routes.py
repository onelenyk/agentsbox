import asyncio
import logging
from pathlib import Path
from typing import List
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from core.db import init_db
from core.config import get_config

router = APIRouter()
logger = logging.getLogger(__name__)

def _tmpl():
    return Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

@router.on_event("startup")
async def startup():
    from core.runners import init_runners_table
    from core.agents import init_agents_table
    init_db()
    init_runners_table()  # daemon is self-sufficient: seed the runners table
    init_agents_table()   # and the agents (personas + tool allowlists)

    import web.agent_mcp as agent_mcp
    from core import orchestrator
    agent_mcp.on_worker_finished = lambda task_id: orchestrator.emit(task_id, "worker_finished")
    reconciled = orchestrator.reconcile_on_boot()
    if reconciled:
        logger.info("orchestrator: resumed %d task(s) orphaned by restart", reconciled)
    asyncio.create_task(orchestrator.dispatch_forever())

@router.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse('<meta http-equiv="refresh" content="0;url=/tasks">')

@router.get("/runtime", response_class=HTMLResponse)
async def runtime_page(request: Request):
    cfg = get_config()
    return _tmpl().TemplateResponse("runtime.html", {"request": request, "active_page": "runtime", "config": cfg})

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
    })

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
