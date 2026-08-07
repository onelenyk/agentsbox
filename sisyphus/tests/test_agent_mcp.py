"""Tests for web/agent_mcp.py — the agent-facing MCP tool surface.

Drives the HTTP MCP endpoint like a worker would: mint a session, then POST
JSON-RPC with the Bearer token. Verifies role-scoped tools, thread writes, and
preconditions.
"""
import json
import os
import subprocess
from unittest.mock import patch

from fastapi.testclient import TestClient
from web.app import app

from core import sessions, store


def _client():
    return TestClient(app)


def _rpc(client, token, method, params=None, req_id=1):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    body = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp/agent", json=body, headers=headers)


def _call(client, token, name, args=None):
    return _rpc(client, token, "tools/call", {"name": name, "arguments": args or {}}).json()


def _session(db_path, role="explorer"):
    sessions.clear()
    task = store.create_task("do the thing", repo="sisyphus")
    token = sessions.mint(task["id"], role)
    return task["id"], token


def _grant(role, *tools):
    from core import agents
    current = agents.agent_tools(role)
    new = list(set(current) | set(tools))
    agents.update_agent(role, tools=new)


# ── protocol ────────────────────────────────────────────────────────────


def test_initialize(db_path):
    task_id, token = _session(db_path)
    r = _rpc(_client(), token, "initialize").json()
    assert r["result"]["protocolVersion"] == "2024-11-05"
    assert r["result"]["serverInfo"]["name"] == "sisyphus-agent"


def test_notification_gets_202(db_path):
    _, token = _session(db_path)
    r = _rpc(_client(), token, "notifications/initialized")
    assert r.status_code == 202


def test_missing_token_rejected(db_path):
    _session(db_path)
    r = _rpc(_client(), None, "tools/list").json()
    assert "error" in r
    assert "session" in r["error"]["message"]


# ── role-scoped tools ───────────────────────────────────────────────────


def test_tools_list_matches_role(db_path):
    c = _client()
    for role, expected in [
        ("explorer", {"get_context", "comment", "attach", "propose_subtasks", "finish"}),
        ("engineer", {"get_context", "comment", "attach", "commit", "open_mr", "finish"}),
        ("reviewer", {"get_context", "comment", "attach", "approve", "request_changes"}),
    ]:
        _, token = _session(db_path, role)
        tools = _rpc(c, token, "tools/list").json()["result"]["tools"]
        assert {t["name"] for t in tools} == expected


def test_tool_outside_role_rejected(db_path):
    task_id, token = _session(db_path, "explorer")
    out = _call(_client(), token, "commit", {"message": "x"})
    assert out["result"]["isError"] is True
    assert "not permitted" in json.loads(out["result"]["content"][0]["text"])["error"]


# ── thread writes ───────────────────────────────────────────────────────


def test_comment_writes_thread(db_path):
    task_id, token = _session(db_path)
    _call(_client(), token, "comment", {"text": "found the bug"})
    thread = store.get_thread(task_id)
    assert thread[-1]["author"] == "explorer"
    assert thread[-1]["text"] == "found the bug"


def test_attach_persists_file_and_records_filename(db_path, tmp_path):
    sessions.clear()
    task = store.create_task("do the thing", repo="sisyphus")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "analysis.md").write_text("the findings")
    token = sessions.mint(task["id"], "explorer", worktree=str(wt))

    out = _call(_client(), token, "attach", {"filename": "analysis.md", "description": "the plan"})
    assert out["result"]["isError"] is False
    assert store.get_thread(task["id"])[-1]["attachment"] == "analysis.md"
    # persisted into the durable store so it survives worktree cleanup
    from core import attachments
    assert attachments.read(task["id"], "analysis.md") == "the findings"


def test_attach_missing_file_errors(db_path, tmp_path):
    sessions.clear()
    task = store.create_task("x", repo="sisyphus")
    wt = tmp_path / "wt"; wt.mkdir()
    token = sessions.mint(task["id"], "explorer", worktree=str(wt))
    out = _call(_client(), token, "attach", {"filename": "nope.md"})
    assert out["result"]["isError"] is True


def test_attach_rejects_path(db_path):
    task_id, token = _session(db_path)
    out = _call(_client(), token, "attach", {"filename": "../etc/passwd"})
    assert out["result"]["isError"] is True


def test_attachment_served_over_api(db_path):
    from core import attachments
    task = store.create_task("x", repo="sisyphus")
    attachments.save_text(task["id"], "change.patch", "diff --git a/x b/x\n")
    r = _client().get(f"/api/v1/tasks/{task['id']}/attachments/change.patch")
    assert r.status_code == 200
    assert "diff --git" in r.text
    assert _client().get(f"/api/v1/tasks/{task['id']}/attachments/missing.txt").status_code == 404


def test_finish_posts_result_and_revokes(db_path):
    task_id, token = _session(db_path)
    out = _call(_client(), token, "finish", {"summary": "all done"})
    assert out["result"]["isError"] is False
    kinds = [m["kind"] for m in store.get_thread(task_id)]
    assert "result" in kinds and "worker_finished" in kinds
    # session revoked → further calls fail
    assert "error" in _rpc(_client(), token, "tools/list").json()


def test_finish_requires_summary(db_path):
    _, token = _session(db_path)
    out = _call(_client(), token, "finish", {"summary": "  "})
    assert out["result"]["isError"] is True


def test_finish_hook_fires(db_path):
    import web.agent_mcp as amcp
    fired = []
    amcp.on_worker_finished = lambda tid: fired.append(tid)
    try:
        task_id, token = _session(db_path)
        _call(_client(), token, "finish", {"summary": "done"})
        assert fired == [task_id]
    finally:
        amcp.on_worker_finished = None


# ── preconditions (the "when" enforced by the tool) ─────────────────────


def test_open_mr_requires_prior_commit(db_path):
    # commit / open_mr do real git + publishing; mock those layers so this test
    # covers the MCP wiring (precondition + thread writes), not git mechanics.
    task_id, token = _session(db_path, "engineer")
    c = _client()
    early = _call(c, token, "open_mr", {"title": "Fix", "description": "..."})
    assert early["result"]["isError"] is True  # no commit yet

    with patch("core.git_ops.is_git_repo", return_value=True), \
         patch("core.git_ops.commit_all", return_value="a" * 40):
        _call(c, token, "commit", {"message": "fix: retry"})
    with patch("core.providers.publish_change", return_value={
        "ok": True, "platform": "local", "branch": "sisyphus/x",
        "stat": " feature.py | 2 +-", "patch_file": "change.patch",
    }):
        ok = _call(c, token, "open_mr", {"title": "Fix", "description": "..."})
    assert ok["result"]["isError"] is False
    mr = [m for m in store.get_thread(task_id) if m["kind"] == "mr"]
    assert mr and mr[0]["attachment"] == "change.patch"
    assert "sisyphus/x" in mr[0]["text"]


def test_commit_reports_sha_and_records_it(db_path):
    task_id, token = _session(db_path, "engineer")
    with patch("core.git_ops.is_git_repo", return_value=True), \
         patch("core.git_ops.commit_all", return_value="deadbeef" + "0" * 32):
        out = _call(_client(), token, "commit", {"message": "add thing"})
    assert out["result"]["isError"] is False
    assert json.loads(out["result"]["content"][0]["text"])["sha"].startswith("deadbeef")
    commits = [m for m in store.get_thread(task_id) if m["kind"] == "commit"]
    assert commits and "deadbeef" in commits[0]["text"]


def test_commit_with_no_changes_errors(db_path):
    task_id, token = _session(db_path, "engineer")
    with patch("core.git_ops.is_git_repo", return_value=True), \
         patch("core.git_ops.commit_all", return_value=None):
        out = _call(_client(), token, "commit", {"message": "nothing"})
    assert out["result"]["isError"] is True
    assert "nothing to commit" in json.loads(out["result"]["content"][0]["text"])["error"]
    assert not any(m["kind"] == "commit" for m in store.get_thread(task_id))


def test_commit_without_repo_errors_clearly(db_path):
    """A repo-less task's scratch dir isn't a git repo — commit tells the worker
    to report via finish() instead of failing with a raw git error."""
    task_id, token = _session(db_path, "engineer")
    with patch("core.git_ops.is_git_repo", return_value=False):
        out = _call(_client(), token, "commit", {"message": "add thing"})
    assert out["result"]["isError"] is True
    assert "no repo" in json.loads(out["result"]["content"][0]["text"])["error"]
    assert not any(m["kind"] == "commit" for m in store.get_thread(task_id))


def test_request_changes_requires_reason(db_path):
    _, token = _session(db_path, "reviewer")
    out = _call(_client(), token, "request_changes", {"reason": ""})
    assert out["result"]["isError"] is True


def test_reviewer_approve_finishes(db_path):
    task_id, token = _session(db_path, "reviewer")
    out = _call(_client(), token, "approve", {"summary": "looks good"})
    assert json.loads(out["result"]["content"][0]["text"])["verdict"] == "approved"
    assert "worker_finished" in [m["kind"] for m in store.get_thread(task_id)]


def test_propose_subtasks_posts_plan(db_path):
    task_id, token = _session(db_path, "explorer")
    out = _call(_client(), token, "propose_subtasks", {"items": [
        {"repo": "ios-app", "goal": "iOS impl"},
        {"repo": "android-app", "goal": "Android impl"},
    ]})
    assert out["result"]["isError"] is False
    plan = [m for m in store.get_thread(task_id) if m["kind"] == "plan"]
    assert plan and "[ios-app] iOS impl" in plan[0]["text"]
    assert "[android-app] Android impl" in plan[0]["text"]


def test_propose_subtasks_requires_items(db_path):
    _, token = _session(db_path, "explorer")
    out = _call(_client(), token, "propose_subtasks", {"items": []})
    assert out["result"]["isError"] is True


def test_propose_subtasks_denied_for_engineer(db_path):
    _, token = _session(db_path, "engineer")
    out = _call(_client(), token, "propose_subtasks", {"items": [{"goal": "x"}]})
    assert out["result"]["isError"] is True  # not in the engineer's tool allowlist


# ── orchestrator decision tools ─────────────────────────────────────────


def test_decide_tool_records_decision_on_session(db_path):
    task_id, token = _session(db_path, "orchestrator")
    out = _call(_client(), token, "decide_spawn_worker",
                {"agent": "engineer", "instruction": "fix it", "why": "start"})
    assert out["result"]["isError"] is False
    dec = sessions.resolve(token)["decision"]
    assert dec["action"] == "spawn_worker"
    assert dec["role"] == "engineer"        # agent name lands in the Decision.role field
    assert dec["instruction"] == "fix it"


def test_decide_complete_records_done(db_path):
    task_id, token = _session(db_path, "orchestrator")
    _call(_client(), token, "decide_complete", {"why": "all children shipped"})
    assert sessions.resolve(token)["decision"]["action"] == "done"


def test_decide_spawn_worker_requires_agent(db_path):
    _, token = _session(db_path, "orchestrator")
    out = _call(_client(), token, "decide_spawn_worker", {"agent": ""})
    assert out["result"]["isError"] is True


def test_worker_cannot_call_decision_tools(db_path):
    _, token = _session(db_path, "engineer")
    out = _call(_client(), token, "decide_complete", {"why": "x"})
    assert out["result"]["isError"] is True   # not in the engineer's allowlist
    assert "not permitted" in json.loads(out["result"]["content"][0]["text"])["error"]


def test_decide_spawn_worker_rejects_unknown_agent(db_path):
    """A hallucinated agent name is rejected in-tool so the orchestrator can
    re-pick (regression: v4-flash inventing 'code')."""
    task_id, token = _session(db_path, "orchestrator")
    out = _call(_client(), token, "decide_spawn_worker", {"agent": "code", "instruction": "x"})
    assert out["result"]["isError"] is True
    err = json.loads(out["result"]["content"][0]["text"])["error"]
    assert "unknown agent 'code'" in err and "engineer" in err
    assert sessions.resolve(token).get("decision") is None   # not recorded


# ── merge request tools ──────────────────────────────────────────────────


def test_list_mrs_returns_open(db_path):
    from core import mergerequests

    _grant("explorer", "list_mrs")
    task_id, token = _session(db_path, "explorer")
    mr = mergerequests.create_mr(task_id, "test-repo", "branch", "main", "Test MR")
    out = _call(_client(), token, "list_mrs")
    result = json.loads(out["result"]["content"][0]["text"])
    assert isinstance(result, list)
    assert any(r["id"] == mr["id"] for r in result)


def test_list_mrs_repo_filter(db_path):
    from core import mergerequests

    _grant("explorer", "list_mrs")
    task_id, token = _session(db_path, "explorer")
    mr_a = mergerequests.create_mr(task_id, "repo-a", "branch-a", "main", "MR A")
    mr_b = mergerequests.create_mr(task_id, "repo-b", "branch-b", "main", "MR B")

    all_out = _call(_client(), token, "list_mrs")
    all_result = json.loads(all_out["result"]["content"][0]["text"])
    assert len(all_result) >= 2

    filt_out = _call(_client(), token, "list_mrs", {"repo": "repo-a"})
    filt_result = json.loads(filt_out["result"]["content"][0]["text"])
    assert any(r["id"] == mr_a["id"] for r in filt_result)
    assert not any(r["id"] == mr_b["id"] for r in filt_result)


def test_get_mr_diff_returns_patch(db_path):
    from core import attachments, mergerequests

    _grant("explorer", "get_mr_diff")
    task_id, token = _session(db_path)
    mr = mergerequests.create_mr(task_id, "test-repo", "branch", "main", "Test")
    attachments.save_text(task_id, "change.patch", "diff --git a/x b/x\n")
    out = _call(_client(), token, "get_mr_diff", {"mr_id": mr["id"]})
    result = json.loads(out["result"]["content"][0]["text"])
    assert "diff" in result
    assert "diff --git" in result["diff"]
    assert result["source"] == "snapshot"


def test_get_mr_diff_fresh_from_clone(db_path, tmp_path):
    from core import mergerequests

    _grant("explorer", "get_mr_diff")
    task_id, token = _session(db_path)

    repo = tmp_path / "clone"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init"], capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=test@test",
                    "-c", "user.name=test", "commit", "--allow-empty", "-m", "base"],
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-b", "sisyphus-task"],
                   capture_output=True)
    (repo / "file.txt").write_text("changed")
    subprocess.run(["git", "-C", str(repo), "add", "file.txt"], capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=test@test",
                    "-c", "user.name=test", "commit", "-m", "change"],
                   capture_output=True)

    mr = mergerequests.create_mr(
        task_id, "test-repo", "sisyphus-task", "main", "Test",
        bare_path=str(repo),
    )
    out = _call(_client(), token, "get_mr_diff", {"mr_id": mr["id"]})
    result = json.loads(out["result"]["content"][0]["text"])
    assert "diff" in result
    assert result["source"] == "fresh"
    assert "file.txt" in result["diff"]


def test_get_mr_diff_fresh_fails_falls_back(db_path):
    from core import attachments, mergerequests

    _grant("explorer", "get_mr_diff")
    task_id, token = _session(db_path)
    mr = mergerequests.create_mr(task_id, "test-repo", "branch", "main", "Test",
                                 bare_path="/nonexistent/path")
    attachments.save_text(task_id, "change.patch", "snapshot diff\n")
    out = _call(_client(), token, "get_mr_diff", {"mr_id": mr["id"]})
    result = json.loads(out["result"]["content"][0]["text"])
    assert result["source"] == "snapshot"
    assert result["diff"] == "snapshot diff\n"


def test_get_mr_diff_missing_errors(db_path):
    _grant("explorer", "get_mr_diff")
    _, token = _session(db_path)
    out = _call(_client(), token, "get_mr_diff", {"mr_id": "mr-nonexistent"})
    assert out["result"]["isError"] is True


def test_list_mrs_requires_session(db_path):
    _grant("explorer", "list_mrs")
    out = _call(_client(), None, "list_mrs")
    assert out["id"] == 1
    assert "error" in out
