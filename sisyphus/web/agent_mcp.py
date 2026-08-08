"""Agent-facing MCP server (HTTP) — the worker's tool surface.

A spawned worker (opencode) connects here over HTTP with a Bearer token that
scopes it to one (task, role). ``tools/list`` returns exactly that role's tools;
``tools/call`` writes to the task thread. "When to use a tool" is enforced by
preconditions *inside the tool*, not by the prompt. This replaces the old
file-based agent contract (events.jsonl / result.json / dir-scan).

Speaks MCP JSON-RPC 2.0 over HTTP: one POST endpoint, one JSON response per
request (notifications get 202). Mounted in the daemon where the DB is writable.
"""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from core import agents, attachments, mergerequests, reviews, sessions, store

agent_mcp = APIRouter()
logger = logging.getLogger(__name__)

# Phase 3 sets this to enqueue an orchestrator event when a worker finishes.
# None = no-op (Phase 2 has no event bus yet).
on_worker_finished = None


def _end_worker(session: dict) -> None:
    store.post_message(
        session["task_id"], author="system", kind="worker_finished",
        text=f"{session['role']} finished",
    )
    sessions.revoke(session["token"])
    if on_worker_finished is not None:
        on_worker_finished(session["task_id"])


# ── Tool handlers (each writes to the task thread) ──────────────────────


def _get_context(session, **_):
    task = store.get_task(session["task_id"])
    return {"goal": task["goal"] if task else "", "thread": store.render_thread(session["task_id"], for_prompt=True)}


def _comment(session, text="", **_):
    if not text.strip():
        return {"error": "text required"}
    store.post_message(session["task_id"], session["role"], kind="note", text=text)
    return {"ok": True}


def _attach(session, filename="", description="", **_):
    if not filename or "/" in filename or "\\" in filename:
        return {"error": "filename must be a bare name"}
    from core import attachments

    if not attachments.save_from_worktree(session["task_id"], session.get("worktree", ""), filename):
        return {"error": f"no file named {filename!r} in your working dir to attach"}
    store.post_message(
        session["task_id"], session["role"], kind="note",
        text=description or f"attached {filename}", attachment=filename,
    )
    return {"ok": True}


def _propose_subtasks(session, items=None, **_):
    """A planner's structured fan-out proposal: one {repo, goal} per repo. Posts
    a 'plan' message the orchestrator turns into child tasks (spawn_task)."""
    items = items or []
    if not items:
        return {"error": "items required: a list of {repo, goal} objects"}
    lines = []
    for it in items:
        goal = (it.get("goal") or "").strip()
        if not goal:
            return {"error": "each item needs a goal"}
        repo = (it.get("repo") or "").strip()
        lines.append(f"- [{repo or 'no repo'}] {goal}")
    store.post_message(session["task_id"], session["role"], kind="plan",
                       text="Proposed subtasks:\n" + "\n".join(lines))
    return {"ok": True, "count": len(items)}


def _finish(session, summary="", **_):
    if not summary.strip():
        return {"error": "summary required"}
    store.post_message(session["task_id"], session["role"], kind="result", text=summary)
    _end_worker(session)
    return {"ok": True}


def _commit(session, message="", **_):
    if not message.strip():
        return {"error": "commit message required"}
    from core import git_ops

    if not git_ops.is_git_repo(session.get("worktree", "")):
        return {"error": "this task has no repo — commit needs one; report your work with finish() instead"}
    try:
        sha = git_ops.commit_all(session["worktree"], message)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"commit failed: {exc}"}
    if sha is None:
        return {"error": "nothing to commit — no changes in the working tree"}
    store.post_message(session["task_id"], session["role"], kind="commit",
                       text=f"{message} ({sha[:8]})")
    return {"ok": True, "sha": sha}


def _open_mr(session, title="", description="", **_):
    if not any(m["kind"] == "commit" for m in store.get_thread(session["task_id"])):
        return {"error": "cannot open a merge request before a commit"}
    from core.providers import publish_change

    result = publish_change(session, title, description)
    if "error" in result:
        return result

    # Compose the thread record. For a local repo the change IS the diff — show
    # the branch + files-changed summary and point at the saved patch (viewable
    # once attachment serving lands, roadmap #3). For a forge it'll be the URL.
    if result.get("platform") == "local":
        text = (f"{title}\n\nBranch: {result['branch']}\n\n{result['stat']}").strip()
        store.post_message(session["task_id"], session["role"], kind="mr",
                           text=text, attachment=result["patch_file"])
    else:
        text = f"{title}: {result.get('url', '')}".strip(": ")
        store.post_message(session["task_id"], session["role"], kind="mr", text=text)

    try:
        mr = mergerequests.latest_for_task(session["task_id"])
        if mr:
            task = reviews.ensure_review_task(mr)
            if task:
                mergerequests.set_review_status(mr["id"], "pending")
    except Exception:
        logger.warning("review hook failed for open_mr on task %s", session["task_id"], exc_info=True)

    return {"ok": True, "branch": result.get("branch")}


def _approve(session, summary="", **_):
    store.post_message(session["task_id"], session["role"], kind="result",
                       text=f"APPROVED: {summary}".strip())
    try:
        task = store.get_task(session["task_id"])
        if task:
            marker = reviews.parse_review_marker(task["goal"])
            if marker:
                mergerequests.set_review_status(marker["mr_id"], "approved")
                if marker["origin_task"]:
                    store.post_message(
                        marker["origin_task"], author="system", kind="note",
                        text=f"review verdict for {marker['mr_id']}: APPROVED — {summary}",
                    )
    except Exception:
        logger.warning("review bookkeeping failed for approve on task %s", session["task_id"], exc_info=True)
    _end_worker(session)
    return {"ok": True, "verdict": "approved"}


# ── Orchestrator decision tools ─────────────────────────────────────────
# The orchestrator agent calls exactly one of these per pass. They do NOT
# execute the action — they record the chosen Decision on the session, which
# core.orchestrator._decide reads back and hands to _execute (so all the
# spawn/worktree/disk logic stays in one place and the guards still apply).


def _record(session, decision: dict):
    session["decision"] = decision
    return {"ok": True}


def _decide_spawn_worker(session, agent="", instruction="", why="", **_):
    if not agent.strip():
        return {"error": "agent required (the worker agent to run)"}
    # Validate against the real roster so a hallucinated name (e.g. a weak model
    # inventing 'code') is rejected here — the orchestrator agent sees the error
    # and re-picks a valid agent in the same session, instead of it becoming a
    # failed spawn later.
    from core import agents
    a = agents.get_agent(agent)
    if not a or a["kind"] != "worker":
        names = ", ".join(x["name"] for x in agents.list_agents(kind="worker"))
        return {"error": f"unknown agent '{agent}'. Call decide_spawn_worker again with exactly one of: {names}"}
    return _record(session, {"action": "spawn_worker", "role": agent,
                             "instruction": instruction, "why": why})


def _decide_spawn_task(session, subtasks=None, why="", **_):
    if not subtasks:
        return {"error": "subtasks required: a list of {repo, goal}"}
    return _record(session, {"action": "spawn_task", "subtasks": subtasks, "why": why})


def _decide_ask_human(session, question="", why="", **_):
    if not question.strip():
        return {"error": "question required"}
    return _record(session, {"action": "ask_human", "question": question, "why": why})


def _decide_complete(session, why="", **_):
    return _record(session, {"action": "done", "why": why})


def _decide_fail(session, reason="", why="", **_):
    return _record(session, {"action": "fail", "reason": reason, "why": why or reason})


def _decide_note(session, text="", why="", **_):
    return _record(session, {"action": "note", "why": why or text})


def _request_changes(session, reason="", **_):
    if not reason.strip():
        return {"error": "reason required for request_changes"}
    store.post_message(session["task_id"], session["role"], kind="result",
                       text=f"CHANGES REQUESTED: {reason}")
    try:
        task = store.get_task(session["task_id"])
        if task:
            marker = reviews.parse_review_marker(task["goal"])
            if marker:
                mergerequests.set_review_status(marker["mr_id"], "changes_requested")
                if marker["origin_task"]:
                    store.post_message(
                        marker["origin_task"], author="system", kind="note",
                        text=f"review verdict for {marker['mr_id']}: CHANGES REQUESTED — {reason}",
                    )
    except Exception:
        logger.warning("review bookkeeping failed for request_changes on task %s", session["task_id"], exc_info=True)
    _end_worker(session)
    return {"ok": True, "verdict": "changes_requested"}


def _list_mrs(session, repo="", **_):
    mrs = mergerequests.list_mrs(status="open")
    return [{"id": m["id"], "task_id": m["task_id"], "title": m["title"],
             "branch": m["source_branch"], "target_branch": m["target_branch"],
             "status": m["status"], "created_at": m["created_at"]} for m in mrs
            if not repo or m["repo"] == repo]


def _get_mr_diff(session, mr_id="", **_):
    mr = mergerequests.get_mr(mr_id)
    if not mr:
        return {"error": f"merge request {mr_id!r} not found"}

    bare_path = mr.get("bare_path", "")
    if bare_path:
        try:
            import subprocess
            r = subprocess.run(
                ["git", "-C", bare_path, "diff", f"{mr['target_branch']}...{mr['source_branch']}"],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0:
                return {"mr_id": mr_id, "source": "fresh", "diff": r.stdout}
        except Exception as e:
            logger.debug("get_mr_diff: fresh diff failed for %s, falling back to snapshot: %s", mr_id, e)

    diff = attachments.read(mr["task_id"], "change.patch")
    if diff is not None:
        return {"mr_id": mr_id, "source": "snapshot", "diff": diff}
    return {"error": f"no diff found for MR {mr_id!r}"}


# name -> (description, inputSchema, handler)
TOOLS = {
    "get_context": ("Read the task goal and the full thread so far.",
                    {"type": "object", "properties": {}}, _get_context),
    "comment": ("Post a short progress note to the task.",
                {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}, _comment),
    "attach": ("Attach a file you saved in your working dir to the task.",
               {"type": "object", "properties": {"filename": {"type": "string"}, "description": {"type": "string"}}, "required": ["filename"]}, _attach),
    "propose_subtasks": ("Propose a multi-repo fan-out: one {repo, goal} per repo.",
                         {"type": "object", "properties": {"items": {"type": "array", "items": {
                             "type": "object",
                             "properties": {"repo": {"type": "string"}, "goal": {"type": "string"}},
                             "required": ["goal"]}}}, "required": ["items"]}, _propose_subtasks),
    "finish": ("Report your final result. Mandatory last action.",
               {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}, _finish),
    "commit": ("Commit your changes in the working tree.",
               {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}, _commit),
    "open_mr": ("Open a merge request. Requires a prior commit.",
                 {"type": "object", "properties": {"title": {"type": "string"}, "description": {"type": "string"}}, "required": ["title"]}, _open_mr),
    "list_mrs": ("List open merge requests and their statuses.",
                 {"type": "object", "properties": {"repo": {"type": "string"}}}, _list_mrs),
    "get_mr_diff": ("Get the diff/patch of a merge request.",
                    {"type": "object", "properties": {"mr_id": {"type": "string"}}, "required": ["mr_id"]}, _get_mr_diff),
    "approve": ("Approve the reviewed work (a reviewer's way to finish).",
                {"type": "object", "properties": {"summary": {"type": "string"}}}, _approve),
    "request_changes": ("Request changes with a reason (a reviewer's way to finish).",
                        {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}, _request_changes),
    # Orchestrator decision tools (kind=orchestrator agents).
    "decide_spawn_worker": ("Decide: run a worker agent on the task.",
                            {"type": "object", "properties": {"agent": {"type": "string"}, "instruction": {"type": "string"}, "why": {"type": "string"}}, "required": ["agent"]}, _decide_spawn_worker),
    "decide_spawn_task": ("Decide: fan out across repos (one child task per {repo, goal}).",
                          {"type": "object", "properties": {"subtasks": {"type": "array", "items": {"type": "object", "properties": {"repo": {"type": "string"}, "goal": {"type": "string"}}, "required": ["goal"]}}, "why": {"type": "string"}}, "required": ["subtasks"]}, _decide_spawn_task),
    "decide_ask_human": ("Decide: pause and ask the human a question.",
                         {"type": "object", "properties": {"question": {"type": "string"}, "why": {"type": "string"}}, "required": ["question"]}, _decide_ask_human),
    "decide_complete": ("Decide: the task is done (needs reviewer approval or all children done).",
                        {"type": "object", "properties": {"why": {"type": "string"}}}, _decide_complete),
    "decide_fail": ("Decide: the task cannot be completed.",
                    {"type": "object", "properties": {"reason": {"type": "string"}, "why": {"type": "string"}}, "required": ["reason"]}, _decide_fail),
    "decide_note": ("Decide: record a thought and take another turn.",
                    {"type": "object", "properties": {"text": {"type": "string"}, "why": {"type": "string"}}}, _decide_note),
}


def _tools_for_agent(agent_name: str) -> list:
    return [
        {"name": name, "description": TOOLS[name][0], "inputSchema": TOOLS[name][1]}
        for name in agents.agent_tools(agent_name) if name in TOOLS
    ]


def _bearer(request: Request):
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else None


def _handle(req: dict, session):
    """Dispatch one JSON-RPC request. Returns a response dict, or None for a
    notification (no id)."""
    req_id = req.get("id")
    method = req.get("method", "")
    params = req.get("params", {})

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "sisyphus-agent", "version": "1.0"},
        }}

    if method.startswith("notifications/"):
        return None

    if session is None:
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32001, "message": "invalid or missing session token"}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": _tools_for_agent(session["role"])}}

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments", {})
        allowed = agents.agent_tools(session["role"])
        if name not in allowed:
            raw = {"error": f"tool {name!r} not permitted for agent {session['role']!r}"}
        else:
            try:
                raw = TOOLS[name][2](session, **args)
            except TypeError as exc:
                raw = {"error": f"invalid arguments: {exc}"}
            except Exception as exc:  # noqa: BLE001
                raw = {"error": str(exc)}
        import json
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "content": [{"type": "text", "text": json.dumps(raw)}],
            "isError": "error" in raw,
        }}

    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


@agent_mcp.post("/mcp/agent")
async def agent_endpoint(request: Request):
    token = _bearer(request)
    session = sessions.resolve(token) if token else None
    body = await request.json()
    resp = _handle(body, session)
    if resp is None:
        return Response(status_code=202)
    return JSONResponse(resp)
