"""JSON API (v1) — the daemon's programmatic surface.

Thin wrappers over ``core/`` that return JSON, consumed by the MCP server (and
any HTTP client). The browser's htmx routes live in ``web/routes.py``; this
router is JSON-only. Runs are dispatched as background tasks on the daemon's
event loop, so a run started by a client keeps executing here even if that
client disconnects.
"""
import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

api = APIRouter(prefix="/api/v1")

# Keep references to in-flight background run tasks so they are not GC'd.
_bg_tasks: set = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


@api.get("/health")
async def health():
    return {"ok": True, "version": "2.0"}


# ── Cards ──────────────────────────────────────────────────────────────


class CreateCard(BaseModel):
    repo: str
    pipeline: str = "investigate"
    description: str = ""
    jira_url: str = ""
    name: str = ""
    run: bool = False


class Comment(BaseModel):
    text: str


@api.post("/cards")
async def create_card(body: CreateCard):
    from core.operations import create_card_only, create_and_run_async

    desc = body.description or "No description"
    if body.run:
        card_id = await create_and_run_async(
            body.pipeline, body.repo, desc, body.jira_url or None, name=body.name,
        )
        return {"card_id": card_id, "status": "running"}
    card = create_card_only(body.pipeline, body.repo, desc, body.jira_url or None, name=body.name)
    return {
        "card_id": card["id"],
        "status": card["status"],
        "pipeline": card["pipeline_name"],
        "name": card.get("name", ""),
        "repo_name": card.get("repo_name", ""),
    }


@api.get("/cards")
async def list_cards(repo: str = "", status: str = "", pipeline: str = ""):
    from core.operations import list_cards_filtered

    cards = list_cards_filtered(
        pipeline=pipeline or None, status=status or None, repo=repo or None,
    )
    return {"cards": cards, "count": len(cards)}


@api.get("/cards/{card_id}")
async def get_card(card_id: str):
    from core.operations import get_card_detail

    try:
        return get_card_detail(card_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Card {card_id} not found")


@api.post("/cards/{card_id}/run")
async def run_card(card_id: str):
    from core.operations import run_card as ops_run_card
    from core.state import get_card as _get_card

    if not _get_card(card_id):
        raise HTTPException(status_code=404, detail=f"Card {card_id} not found")
    _spawn(ops_run_card(card_id))  # non-blocking: executes in the daemon
    return {"card_id": card_id, "status": "running"}


@api.post("/cards/{card_id}/cancel")
async def cancel_card(card_id: str):
    from core.operations import cancel_card as ops_cancel

    await ops_cancel(card_id)
    return {"success": True, "card_id": card_id}


@api.post("/cards/{card_id}/resume")
async def resume_card(card_id: str):
    from core.operations import resume_card as ops_resume

    await ops_resume(card_id)  # fires its own background task, returns quickly
    return {"success": True, "card_id": card_id}


@api.post("/cards/{card_id}/comments")
async def add_comment(card_id: str, body: Comment):
    from core.db import get_conn
    from core.state import add_activity, get_card as _get_card

    if not _get_card(card_id):
        raise HTTPException(status_code=404, detail=f"Card {card_id} not found")
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="text must not be empty")

    conn = get_conn()
    run = conn.execute(
        "SELECT id FROM runs WHERE card_id = ? ORDER BY started_at DESC LIMIT 1",
        (card_id,),
    ).fetchone()
    conn.close()

    activity = add_activity(
        card_id, text, author="user", author_type="human",
        run_id=run["id"] if run else None,
    )
    return {"id": activity["id"], "card_id": card_id}


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
