"""Tests for web/agent_mcp.py — the agent-facing MCP tool surface.

Drives the HTTP MCP endpoint like a worker would: mint a session, then POST
JSON-RPC with the Bearer token. Verifies role-scoped tools, thread writes, and
preconditions.
"""
import json
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
        ("explorer", {"get_context", "comment", "attach", "finish"}),
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


def test_attach_records_filename(db_path):
    task_id, token = _session(db_path)
    _call(_client(), token, "attach", {"filename": "analysis.md", "description": "the plan"})
    assert store.get_thread(task_id)[-1]["attachment"] == "analysis.md"


def test_attach_rejects_path(db_path):
    task_id, token = _session(db_path)
    out = _call(_client(), token, "attach", {"filename": "../etc/passwd"})
    assert out["result"]["isError"] is True


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

    with patch("core.git_ops.commit_all", return_value="a" * 40):
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
    with patch("core.git_ops.commit_all", return_value="deadbeef" + "0" * 32):
        out = _call(_client(), token, "commit", {"message": "add thing"})
    assert out["result"]["isError"] is False
    assert json.loads(out["result"]["content"][0]["text"])["sha"].startswith("deadbeef")
    commits = [m for m in store.get_thread(task_id) if m["kind"] == "commit"]
    assert commits and "deadbeef" in commits[0]["text"]


def test_commit_with_no_changes_errors(db_path):
    task_id, token = _session(db_path, "engineer")
    with patch("core.git_ops.commit_all", return_value=None):
        out = _call(_client(), token, "commit", {"message": "nothing"})
    assert out["result"]["isError"] is True
    assert "nothing to commit" in json.loads(out["result"]["content"][0]["text"])["error"]
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
