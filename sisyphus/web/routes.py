from pathlib import Path
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from core.db import init_db, get_conn
from core.state import list_cards
from core.resources import read_resource
from core.recovery import recover_orphaned_steps
from core.config import get_config
from core.scheduler import start_scheduler, get_scheduler
from agents import load_agent, list_agents

router = APIRouter()

def _tmpl():
    return Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

@router.on_event("startup")
async def startup():
    from core.runners import init_runners_table
    init_db()
    init_runners_table()  # daemon is self-sufficient: seed the runners table
    recover_orphaned_steps()
    start_scheduler()

@router.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse('<meta http-equiv="refresh" content="0;url=/kanban">')

@router.get("/kanban", response_class=HTMLResponse)
async def kanban(request: Request):
    return _tmpl().TemplateResponse("kanban.html", {"request": request, "active_page": "kanban"})

@router.get("/api/cards/new-form", response_class=HTMLResponse)
async def new_form(request: Request):
    import yaml
    pd = Path(__file__).parent.parent / "pipelines"
    pipe_names = [f.stem for f in sorted(pd.glob("*.yaml"))]
    from core.repos import list_repos
    return _tmpl().TemplateResponse("new_card.html", {"request": request, "pipelines": pipe_names, "repos": list_repos()})

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    cards = list_cards()
    stats = {"total": len(cards), "backlog": sum(1 for c in cards if c["status"]=="backlog"),
             "completed": sum(1 for c in cards if c["status"]=="completed"),
             "running": sum(1 for c in cards if c["status"]=="running"),
             "failed": sum(1 for c in cards if c["status"]=="failed")}
    return _tmpl().TemplateResponse("dashboard.html", {"request": request, "active_page": "dashboard", "stats": stats, "cards": cards[:10]})

@router.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request):
    ags = [load_agent(n) for n in list_agents()]
    return _tmpl().TemplateResponse("agents.html", {"request": request, "active_page": "agents", "agents": ags})

@router.get("/runtime", response_class=HTMLResponse)
async def runtime_page(request: Request):
    cfg = get_config()
    sched = get_scheduler()
    jobs = sched.get_jobs() if sched else []
    return _tmpl().TemplateResponse("runtime.html", {"request": request, "active_page": "runtime", "config": cfg, "jobs": jobs})

@router.get("/pipelines", response_class=HTMLResponse)
async def pipelines_page(request: Request):
    import yaml
    pd = Path(__file__).parent.parent / "pipelines"
    pipes = []
    for f in sorted(pd.glob("*.yaml")):
        d = yaml.safe_load(f.read_text())
        pipes.append({"name": d.get("name",""), "file": f.name, "steps": d.get("steps",[])})
    return _tmpl().TemplateResponse("pipelines.html", {"request": request, "active_page": "pipelines", "pipelines": pipes})

@router.get("/history", response_class=HTMLResponse)
async def history_page(request: Request, status: str = ""):
    conn = get_conn()
    if status:
        rows = conn.execute("""
            SELECT r.*, c.pipeline_name, c.description as card_description
            FROM runs r LEFT JOIN cards c ON r.card_id = c.id
            WHERE r.status=? ORDER BY r.started_at DESC LIMIT 50
        """, (status,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT r.*, c.pipeline_name, c.description as card_description
            FROM runs r LEFT JOIN cards c ON r.card_id = c.id
            ORDER BY r.started_at DESC LIMIT 50
        """).fetchall()
    runs = [{k: row[k] for k in row.keys()} for row in rows]
    conn.close()
    return _tmpl().TemplateResponse("history.html", {"request": request, "runs": runs, "active_page": "history"})

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

# ── Board ──────────────────────────────────────────────────────────────

@router.get("/api/cards/board", response_class=HTMLResponse)
async def board(request: Request):
    conn = get_conn()
    cols = {}
    for s in ["backlog", "created", "running", "completed", "failed"]:
        rows = conn.execute("""
            SELECT c.*, 
                   (SELECT s2.agent_name FROM steps s2 
                    JOIN runs r2 ON s2.run_id = r2.id 
                    WHERE r2.card_id = c.id AND s2.status IN ('running','pending')
                    ORDER BY r2.started_at DESC LIMIT 1) as current_agent,
                   r.status as run_status,
                   r.started_at as run_started_at,
                   r.finished_at as run_finished_at,
                   (SELECT COUNT(*) FROM steps s3 WHERE s3.run_id = r.id) as step_count,
                   (SELECT COUNT(*) FROM steps s3 WHERE s3.run_id = r.id AND s3.status = 'completed') as steps_done,
                   (SELECT s4.agent_name || ': ' || COALESCE(s4.error, '') FROM steps s4 
                    WHERE s4.run_id = r.id AND s4.status IN ('failed','error')
                    ORDER BY s4.step_order ASC LIMIT 1) as last_error,
                   (SELECT COUNT(*) FROM resources res WHERE res.run_id = r.id) as resource_count,
                   (SELECT GROUP_CONCAT(filename, ', ') FROM (
                       SELECT DISTINCT filename FROM resources WHERE run_id = r.id
                   )) as resource_list
            FROM cards c 
            LEFT JOIN runs r ON r.card_id = c.id AND r.started_at = (
                SELECT MAX(r2.started_at) FROM runs r2 WHERE r2.card_id = c.id
            )
            WHERE c.status=? ORDER BY c.created_at DESC
        """, (s,)).fetchall()
        cols[s] = [dict(r) for r in rows]
    conn.close()
    
    for col_cards in cols.values():
        for card in col_cards:
            card["repo_name"] = card.get("repo_name", "")
    return _tmpl().TemplateResponse("board_content.html", {"request": request, "columns": cols})

@router.get("/api/cards/{card_id}/detail", response_class=HTMLResponse)
async def card_detail(request: Request, card_id: str):
    from core.operations import get_card_detail
    try:
        detail = get_card_detail(card_id)
    except ValueError:
        return HTMLResponse('<div class="detail-panel"><p style="color:#f85149">Not found</p></div>')
    card = detail["card"]
    run = detail["run"]
    steps = detail["steps"]
    resources = detail["resources"]
    repo_name = detail["repo_name"]
    return _tmpl().TemplateResponse("card_detail.html", {"request": request, "card": card, "run": run, "steps": steps, "resources": resources, "activities": detail["activities"], "repo_name": repo_name})

@router.get("/api/resources/{run_id}/{filename}", response_class=HTMLResponse)
async def resource_view(request: Request, run_id: str, filename: str, step_id: str = ""):
    content = read_resource(run_id, filename, step_id or None)
    if content is None:
        return HTMLResponse('<div style="color:#f85149;padding:12px">Resource not found</div>')
    if filename.endswith('.json'):
        import json
        try:
            content = json.dumps(json.loads(content), indent=2)
        except Exception:
            pass
    return HTMLResponse(f'<pre style="font-size:13px;color:#c9d1d9;background:#0d1117;padding:16px;border-radius:6px;overflow:auto;max-height:80vh;white-space:pre-wrap;word-wrap:break-word;line-height:1.5">{content}</pre>')

@router.post("/api/cards")
async def create(request: Request, pipeline: str = Form(...), repo: str = Form(...), description: str = Form(...), jira_url: str = Form(""), repo_url: str = Form(""), run_now: str = Form("0"), name: str = Form("")):
    from core.operations import create_card_only, create_and_run_async
    if run_now == "1":
        await create_and_run_async(pipeline, repo, description, jira_url or None, repo_url or None, name=name)
    else:
        create_card_only(pipeline, repo, description, jira_url or None, name=name)
    from fastapi.responses import Response
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "cardCreated"
    return resp

@router.post("/api/cards/{card_id}/run")
async def run_card(card_id: str):
    from core.operations import run_card as ops_run_card
    await ops_run_card(card_id)
    from fastapi.responses import Response
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "cardUpdated"
    return resp

@router.post("/api/cards/{card_id}/cancel")
async def cancel(card_id: str):
    from core.operations import cancel_card
    await cancel_card(card_id)
    from fastapi.responses import Response
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "cardUpdated"
    return resp

@router.post("/api/cards/{card_id}/resume")
async def resume(card_id: str):
    from core.operations import resume_card
    await resume_card(card_id)
    from fastapi.responses import Response
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "cardUpdated"
    return resp

@router.post("/api/cards/{card_id}/comments")
async def add_comment(card_id: str, text: str = Form(...)):
    from core.state import add_activity
    from core.db import get_conn
    from fastapi.responses import Response

    text = text.strip()
    if not text:
        return Response(status_code=422)

    conn = get_conn()
    run = conn.execute(
        "SELECT id FROM runs WHERE card_id = ? ORDER BY started_at DESC LIMIT 1",
        (card_id,),
    ).fetchone()
    conn.close()

    add_activity(
        card_id, text, author="user", author_type="human",
        run_id=run["id"] if run else None,
    )
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "cardUpdated"
    return resp

@router.get("/api/cards")
async def cards_list():
    return list_cards()

