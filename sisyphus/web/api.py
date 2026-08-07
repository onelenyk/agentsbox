"""JSON API (v1) — the daemon's programmatic surface.

Thin wrappers over ``core/`` that return JSON, consumed by the MCP server (and
any HTTP client). The browser's htmx routes live in ``web/routes.py``; this
router is JSON-only.
"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import logging

logger = logging.getLogger(__name__)

api = APIRouter(prefix="/api/v1")


@api.get("/health")
async def health():
    """Liveness (ok) plus readiness: db reachable and the default runner's binary
    resolves. degraded=True means the process is up but can't run work."""
    from core import preflight

    pf = preflight.check()
    return {
        "ok": True,
        "version": "2.0",
        "ready": pf["ok"],
        "degraded": not pf["ok"],
        "db": pf["db"],
        "runner": pf["runner"],
        "reason": pf["reason"] or None,
    }


# ── Repos ──────────────────────────────────────────────────────────────


class AddRepo(BaseModel):
    name: str
    source: str
    branch: str = "main"
    platform: str = ""


@api.get("/repos")
async def list_repos():
    from core.repos import list_repos as _list_repos

    return {"repos": _list_repos()}


@api.post("/repos")
async def add_repo(body: AddRepo):
    from core.repos import add_repo as _add_repo

    path = _add_repo(body.name, body.source, body.platform, body.branch)
    return {"name": body.name, "source": body.source, "branch": body.branch, "bare_path": path}


@api.delete("/repos/{name}")
async def delete_repo(name: str):
    from core.repos import delete_repo as _delete_repo

    _delete_repo(name)
    return {"success": True, "name": name}


@api.post("/repos/{name}/update")
async def update_repo(name: str):
    from core.repos import update_repo as _update_repo

    return {"success": _update_repo(name), "name": name}


# ── Runners ────────────────────────────────────────────────────────────


class AddRunner(BaseModel):
    name: str
    binary: str
    type: str = "opencode"
    model: str = ""
    api_key_env: str = ""
    base_url: str = ""
    is_default: bool = False


class UpdateRunner(BaseModel):
    binary: str = ""
    model: str = ""
    api_key_env: str = ""
    base_url: str = ""


@api.get("/runners")
async def list_runners():
    from core.runners import list_runners as _list_runners

    return {"runners": _list_runners()}


@api.post("/runners")
async def add_runner(body: AddRunner):
    from core.runners import add_runner as _add_runner

    r = _add_runner(
        body.name, body.binary, body.type, body.model,
        body.api_key_env, body.base_url, [], body.is_default,
    )
    return {"id": r["id"], "name": r["name"], "binary": r["binary"], "type": r["type"]}


def _require_runner(name: str):
    from core.runners import get_runner_by_name

    r = get_runner_by_name(name)
    if not r:
        raise HTTPException(status_code=404, detail=f"Runner not found: {name}")
    return r


@api.patch("/runners/{name}")
async def update_runner(name: str, body: UpdateRunner):
    from core.runners import update_runner as _update_runner

    r = _require_runner(name)
    fields = {k: v for k, v in body.model_dump().items() if v}
    _update_runner(r["id"], **fields)
    return {"success": True, "name": name}


@api.delete("/runners/{name}")
async def delete_runner(name: str):
    from core.runners import delete_runner as _delete_runner

    r = _require_runner(name)
    _delete_runner(r["id"])
    return {"success": True, "name": name}


@api.post("/runners/{name}/health")
async def runner_health(name: str):
    from core.runners import health_check

    r = _require_runner(name)
    return await health_check(r["id"])


@api.post("/runners/{name}/set-default")
async def runner_set_default(name: str):
    from core.runners import set_default_runner

    r = _require_runner(name)
    set_default_runner(r["id"])
    return {"success": True, "name": name}


# ── Statistics ─────────────────────────────────────────────────────────


@api.get("/stats")
async def get_stats():
    from core.stats import overview
    return overview()


# ── Agents ─────────────────────────────────────────────────────────────


class AddAgent(BaseModel):
    name: str
    persona: str = ""
    tools: list = []
    runner_id: str = ""
    model: str = ""
    kind: str = "worker"


class UpdateAgent(BaseModel):
    persona: str = None
    tools: list = None
    runner_id: str = None
    model: str = None
    enabled: int = None


@api.get("/agents")
async def list_agents_ep(kind: str = ""):
    from core.agents import list_agents
    return {"agents": list_agents(kind or None)}


@api.get("/agents/tool-catalog")
async def agent_tool_catalog():
    """The full set of agent-MCP tools an agent can be granted, so a UI can
    render the allowlist as checkboxes."""
    from web.agent_mcp import TOOLS
    return {"tools": [{"name": n, "description": TOOLS[n][0]} for n in TOOLS]}


@api.post("/agents")
async def add_agent_ep(body: AddAgent):
    from core.agents import add_agent, get_agent
    if get_agent(body.name):
        raise HTTPException(status_code=409, detail=f"agent {body.name!r} already exists")
    try:
        a = add_agent(body.name, body.persona, body.tools,
                      body.runner_id or None, body.model, body.kind)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"success": True, "agent": a}


@api.patch("/agents/{name}")
async def update_agent_ep(name: str, body: UpdateAgent):
    from core.agents import get_agent, update_agent
    if not get_agent(name):
        raise HTTPException(status_code=404, detail=f"agent {name!r} not found")
    a = update_agent(name, **body.model_dump(exclude_none=True))
    return {"success": True, "agent": a}


@api.delete("/agents/{name}")
async def delete_agent_ep(name: str):
    from core.agents import delete_agent
    try:
        ok = delete_agent(name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail=f"agent {name!r} not found")
    return {"success": True, "name": name}


# ── Tasks ──────────────────────────────────────────────────────────────


class CreateTask(BaseModel):
    goal: str
    repo: str = ""
    agent: str = ""   # pin a handling agent; empty = orchestrator dispatches


class TaskMessage(BaseModel):
    text: str


@api.post("/tasks")
async def create_task(body: CreateTask):
    from core import store
    from core.orchestrator import emit

    if not body.goal.strip():
        raise HTTPException(status_code=422, detail="goal is required")
    # A repo is optional: repo-less tasks (research / planning / Q&A) run in a
    # private scratch dir (core.worker.spawn_worker). Without a repo the
    # engineer's commit/open_mr tools have nothing to act on and return a clear
    # "needs a repo" error — so such tasks are naturally read-only.
    # An agent is optional: pinning one runs it solo (single-agent mode); empty
    # lets the orchestrator dispatch. A pinned agent must be a real worker agent.
    if body.agent.strip():
        from core.agents import get_agent
        a = get_agent(body.agent)
        if not a or a["kind"] != "worker":
            raise HTTPException(status_code=422, detail="agent must be an existing worker agent")
    task = store.create_task(body.goal, body.repo, agent=body.agent or None)
    emit(task["id"], "created")
    return {"task_id": task["id"], "status": task["status"]}


@api.get("/tasks")
async def list_tasks(status: str = ""):
    from core import store

    tasks = store.list_tasks(status or None)
    return {"tasks": tasks, "count": len(tasks)}


@api.get("/tasks/{task_id}")
async def get_task(task_id: str):
    from core import store

    task = store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return {
        "task": task,
        "messages": store.get_thread(task_id),
        "rendered": store.render_thread(task_id),
    }


@api.post("/tasks/{task_id}/messages")
async def post_task_message(task_id: str, body: TaskMessage):
    from core import store
    from core.orchestrator import emit

    if not store.get_task(task_id):
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="text must not be empty")

    message = store.post_message(task_id, "human", kind="answer", text=text)
    emit(task_id, "human_replied")
    return {"id": message["id"], "task_id": task_id}


@api.get("/tasks/{task_id}/attachments/{filename}")
async def get_attachment(task_id: str, filename: str):
    from fastapi.responses import PlainTextResponse
    from core import attachments, store

    if not store.get_task(task_id):
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    content = attachments.read(task_id, filename)
    if content is None:
        raise HTTPException(status_code=404, detail=f"Attachment {filename} not found")
    return PlainTextResponse(content)


@api.delete("/tasks/{task_id}")
async def delete_task(task_id: str):
    from core import sessions, store
    from core.worker import cleanup_task

    if not store.get_task(task_id):
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    tree = store.task_tree(task_id)   # root + fan-out descendants
    for tid in tree:
        sessions.revoke_task(tid)     # stop any still-running worker from writing back
    store.delete_task(task_id)        # cascades the whole subtree in the DB
    for tid in tree:
        try:
            cleanup_task(tid, deep=True)   # also drop each node's attachments store
        except Exception as e:
            logger.warning("cleanup failed for task %s during delete: %s", tid, e)
            pass
    return {"success": True, "task_id": task_id}


# ── Forge webhooks (MR lifecycle) ───────────────────────────────────────


@api.post("/webhooks/{platform}")
async def receive_webhook(platform: str, request: Request):
    """Ingest a forge event. Two independent reactions, both best-effort:
    (1) MR lifecycle — map the payload's MR/PR URL to an existing task and
    re-open it; (2) autopilot triggers — fire webhook autopilots that match this
    platform + event, creating new tasks."""
    import json
    from core import autopilot, store, webhooks
    from core.orchestrator import emit
    from web.auth import _is_local

    # Webhook authentication: verify signature if secret is configured,
    # or require loopback if not.
    secret = webhooks.get_webhook_secret()
    body = await request.body()

    if secret:
        # Secret is configured — verify the request signature
        if platform == "github":
            sig = request.headers.get("X-Hub-Signature-256", "")
            if not webhooks.verify_github_signature(body, sig):
                return JSONResponse({"detail": "invalid webhook signature"}, status_code=401)
        elif platform == "gitlab":
            token = request.headers.get("X-Gitlab-Token", "")
            if not webhooks.verify_gitlab_token(token):
                return JSONResponse({"detail": "invalid webhook token"}, status_code=401)
        else:
            # generic/jira platform
            token = request.headers.get("X-Sisyphus-Token", "")
            if not webhooks.verify_sisyphus_token(token):
                return JSONResponse({"detail": "invalid webhook token"}, status_code=401)
    else:
        # No secret configured — only accept from loopback
        if not _is_local(request):
            return JSONResponse(
                {"detail": "webhook secret not configured; set SISYPHUS_WEBHOOK_SECRET or webhooks.secret"},
                status_code=403
            )

    try:
        payload = json.loads(body) if body else {}
    except Exception as e:
        logger.warning("Failed to parse webhook body for %s: %s", platform, e)
        payload = {}

    result = {"ok": True}

    # (1) MR lifecycle → wake the owning task
    parsed = webhooks.parse(platform, payload)
    if parsed:
        mr_url, summary = parsed
        task = store.find_task_by_mr(mr_url)
        if task:
            store.post_message(task["id"], "system", kind="external", text=summary)
            if task["status"] in ("done", "failed", "waiting"):
                store.set_status(task["id"], "active")
            emit(task["id"], "external")
            result["task_id"] = task["id"]
        else:
            result["matched"] = False
    else:
        result["ignored"] = True

    # (2) Autopilot webhook triggers → create new tasks
    fired = autopilot.fire_webhook_autopilots(platform, payload)
    if fired:
        result["autopilots_fired"] = fired

    return result


# ── Autopilots ──────────────────────────────────────────────────────────


class CreateAutopilot(BaseModel):
    name: str
    description: str = ""
    trigger_type: str = "schedule"   # schedule | webhook | manual
    cron: str = ""
    interval_seconds: int = 0
    webhook_platform: str = ""
    webhook_event: str = ""
    goal_template: str = ""
    repo: str = ""
    agent: str = ""
    enabled: int = 1
    skip_if_running: int = 1


class UpdateAutopilot(BaseModel):
    name: str = None
    description: str = None
    trigger_type: str = None
    cron: str = None
    interval_seconds: int = None
    webhook_platform: str = None
    webhook_event: str = None
    goal_template: str = None
    repo: str = None
    agent: str = None
    enabled: int = None
    skip_if_running: int = None


@api.get("/autopilots")
async def list_autopilots_ep():
    from core.autopilot import list_autopilots
    return {"autopilots": list_autopilots()}


@api.post("/autopilots")
async def create_autopilot_ep(body: CreateAutopilot):
    from core.autopilot import create_autopilot
    if not body.name.strip():
        raise HTTPException(status_code=422, detail="name is required")
    return {"autopilot": create_autopilot(**body.model_dump())}


def _require_autopilot(ap_id: str):
    from core.autopilot import get_autopilot
    ap = get_autopilot(ap_id)
    if not ap:
        raise HTTPException(status_code=404, detail=f"autopilot not found: {ap_id}")
    return ap


@api.get("/autopilots/{ap_id}")
async def get_autopilot_ep(ap_id: str):
    return {"autopilot": _require_autopilot(ap_id)}


@api.patch("/autopilots/{ap_id}")
async def update_autopilot_ep(ap_id: str, body: UpdateAutopilot):
    from core.autopilot import update_autopilot
    _require_autopilot(ap_id)
    return {"autopilot": update_autopilot(ap_id, **body.model_dump(exclude_none=True))}


@api.delete("/autopilots/{ap_id}")
async def delete_autopilot_ep(ap_id: str):
    from core.autopilot import delete_autopilot
    _require_autopilot(ap_id)
    return {"success": delete_autopilot(ap_id), "id": ap_id}


@api.post("/autopilots/{ap_id}/run")
async def run_autopilot_ep(ap_id: str):
    from core.autopilot import fire
    ap = _require_autopilot(ap_id)
    task_id = fire(ap)
    return {"fired": task_id is not None, "task_id": task_id}


@api.post("/autopilots/{ap_id}/pause")
async def pause_autopilot_ep(ap_id: str):
    from core.autopilot import set_enabled
    _require_autopilot(ap_id)
    return {"autopilot": set_enabled(ap_id, False)}


@api.post("/autopilots/{ap_id}/resume")
async def resume_autopilot_ep(ap_id: str):
    from core.autopilot import set_enabled
    _require_autopilot(ap_id)
    return {"autopilot": set_enabled(ap_id, True)}


# ── Orchestrator control ────────────────────────────────────────────────


@api.post("/orchestrator/emit")
async def orchestrator_emit(task_id: str = "", event_type: str = "worker_finished"):
    """Emit an event into the orchestrator's dispatch queue."""
    from core.orchestrator import emit
    from core.store import get_task

    if not task_id.strip():
        raise HTTPException(status_code=422, detail="task_id required")
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    emit(task_id, event_type)
    return {"ok": True, "task_id": task_id, "event": event_type}
