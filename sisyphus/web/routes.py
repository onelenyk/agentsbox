import asyncio
import logging
from pathlib import Path
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
    init_db()
    init_runners_table()  # daemon is self-sufficient: seed the runners table

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

# ── Tasks (Task+Thread system) ──────────────────────────────────────────

@router.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request):
    return _tmpl().TemplateResponse("tasks.html", {"request": request, "active_page": "tasks"})

@router.get("/api/tasks/list", response_class=HTMLResponse)
async def tasks_list_view(request: Request):
    from core import store
    columns = {status: store.list_tasks(status=status) for status in store.VALID_STATUSES}
    return _tmpl().TemplateResponse("tasks_list.html", {"request": request, "columns": columns})

@router.get("/api/tasks/new-form", response_class=HTMLResponse)
async def tasks_new_form(request: Request):
    from core.repos import list_repos
    return _tmpl().TemplateResponse("tasks_new.html", {"request": request, "repos": list_repos()})

@router.post("/api/tasks")
async def tasks_create(goal: str = Form(...), repo: str = Form("")):
    from core import store
    from core.orchestrator import emit
    from fastapi.responses import Response

    if not repo.strip():
        # See web/api.py's create_task: a repo-less worker never gets its
        # agent-MCP config written and can never succeed. htmx doesn't swap
        # on non-2xx by default, so this surfaces via the global error toast
        # (base.html's htmx:afterRequest handler) — same pattern as the
        # empty-comment 422 below.
        return Response(status_code=422)
    task = store.create_task(goal, repo)
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
    })

@router.get("/api/tasks/{task_id}/detail", response_class=HTMLResponse)
async def task_detail_view(request: Request, task_id: str):
    from core import store

    task = store.get_task(task_id)
    if not task:
        return HTMLResponse('<div class="detail-panel"><p style="color:#f85149">Task not found</p></div>')
    return _tmpl().TemplateResponse("task_detail_content.html", {
        "request": request, "task": task, "messages": store.get_thread(task_id),
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
    sessions.revoke_task(task_id)
    store.delete_task(task_id)
    try:
        cleanup_task(task_id, deep=True)
    except Exception:
        pass
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = "/tasks"   # navigate back to the board
    return resp
