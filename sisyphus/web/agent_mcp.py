"""Agent-facing MCP server (HTTP) — the worker's tool surface.

A spawned worker (opencode) connects here over HTTP with a Bearer token that
scopes it to one (task, role). ``tools/list`` returns exactly that role's tools;
``tools/call`` writes to the task thread. "When to use a tool" is enforced by
preconditions *inside the tool*, not by the prompt. This replaces the old
file-based agent contract (events.jsonl / result.json / dir-scan).

Speaks MCP JSON-RPC 2.0 over HTTP: one POST endpoint, one JSON response per
request (notifications get 202). Mounted in the daemon where the DB is writable.
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from core import sessions, store
from core.roles import role_tools

agent_mcp = APIRouter()

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
    return {"ok": True, "branch": result.get("branch")}


def _approve(session, summary="", **_):
    store.post_message(session["task_id"], session["role"], kind="result",
                       text=f"APPROVED: {summary}".strip())
    _end_worker(session)
    return {"ok": True, "verdict": "approved"}


def _request_changes(session, reason="", **_):
    if not reason.strip():
        return {"error": "reason required for request_changes"}
    store.post_message(session["task_id"], session["role"], kind="result",
                       text=f"CHANGES REQUESTED: {reason}")
    _end_worker(session)
    return {"ok": True, "verdict": "changes_requested"}


# name -> (description, inputSchema, handler)
TOOLS = {
    "get_context": ("Read the task goal and the full thread so far.",
                    {"type": "object", "properties": {}}, _get_context),
    "comment": ("Post a short progress note to the task.",
                {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}, _comment),
    "attach": ("Attach a file you saved in your working dir to the task.",
               {"type": "object", "properties": {"filename": {"type": "string"}, "description": {"type": "string"}}, "required": ["filename"]}, _attach),
    "finish": ("Report your final result. Mandatory last action.",
               {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}, _finish),
    "commit": ("Commit your changes in the working tree.",
               {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}, _commit),
    "open_mr": ("Open a merge request. Requires a prior commit.",
                {"type": "object", "properties": {"title": {"type": "string"}, "description": {"type": "string"}}, "required": ["title"]}, _open_mr),
    "approve": ("Approve the reviewed work (a reviewer's way to finish).",
                {"type": "object", "properties": {"summary": {"type": "string"}}}, _approve),
    "request_changes": ("Request changes with a reason (a reviewer's way to finish).",
                        {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}, _request_changes),
}


def _tools_for_role(role: str) -> list:
    return [
        {"name": name, "description": TOOLS[name][0], "inputSchema": TOOLS[name][1]}
        for name in role_tools(role)
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
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": _tools_for_role(session["role"])}}

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments", {})
        allowed = role_tools(session["role"])
        if name not in allowed:
            raw = {"error": f"tool {name!r} not permitted for role {session['role']!r}"}
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
