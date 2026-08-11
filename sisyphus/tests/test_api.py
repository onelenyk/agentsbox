"""Tests for web/api.py — the daemon's JSON API (/api/v1).

This is the surface the MCP thin client (and any HTTP client) forwards to.
"""
from unittest.mock import patch

from fastapi.testclient import TestClient
from web.app import app

from core.runners import init_runners_table


def _client():
    return TestClient(app)


# ── Health ──────────────────────────────────────────────────────────────


def test_health(db_path):
    r = _client().get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_health_git_commit_is_populated():
    """The ghost-daemon check is 'curl /health and compare the SHA' — an empty
    git_commit silently defeats it. Must not depend on the process CWD: the
    daemon is started from anywhere (launchd has no shell).
    """
    import os
    from web.api import _read_git_commit

    cwd = os.getcwd()
    try:
        os.chdir("/")
        sha = _read_git_commit()
    finally:
        os.chdir(cwd)
    assert sha, "git_commit is empty — /health cannot identify the running code"
    assert len(sha) >= 7


# ── Repos ───────────────────────────────────────────────────────────────


def test_list_repos(db_path):
    r = _client().get("/api/v1/repos")
    assert r.status_code == 200
    assert "repos" in r.json()


# ── Runners ─────────────────────────────────────────────────────────────


def test_runner_crud(db_path):
    init_runners_table()
    c = _client()
    assert c.get("/api/v1/runners").status_code == 200

    added = c.post("/api/v1/runners", json={"name": "t", "binary": "echo"}).json()
    assert added["name"] == "t"

    assert c.post("/api/v1/runners/t/set-default").json()["success"] is True
    assert c.patch("/api/v1/runners/t", json={"binary": "cat"}).json()["success"] is True
    assert c.request("DELETE", "/api/v1/runners/t").json()["success"] is True


# ── Tasks (Task+Thread system) ──────────────────────────────────────────


def test_create_task_emits_created_event(db_path):
    c = _client()
    with patch("core.orchestrator.emit") as mock_emit:
        r = c.post("/api/v1/tasks", json={"goal": "do the thing", "repo": "sisyphus"})
    assert r.status_code == 200
    body = r.json()
    assert body["task_id"].startswith("task-")
    assert body["status"] == "active"
    mock_emit.assert_called_once_with(body["task_id"], "created")


def test_create_task_without_repo_allowed(db_path):
    """Repo-less tasks (research / planning / Q&A) run in a private scratch dir
    — allowed, and they emit the created event like any other task."""
    c = _client()
    with patch("core.orchestrator.emit") as mock_emit:
        r = c.post("/api/v1/tasks", json={"goal": "research the options"})
    assert r.status_code == 200
    mock_emit.assert_called_once_with(r.json()["task_id"], "created")


def test_create_task_without_goal_rejected(db_path):
    c = _client()
    with patch("core.orchestrator.emit") as mock_emit:
        r = c.post("/api/v1/tasks", json={"goal": "   ", "repo": "sisyphus"})
    assert r.status_code == 422
    mock_emit.assert_not_called()


def test_list_tasks(db_path):
    c = _client()
    with patch("core.orchestrator.emit"):
        tid = c.post("/api/v1/tasks", json={"goal": "g", "repo": "sisyphus"}).json()["task_id"]
    listed = c.get("/api/v1/tasks").json()
    assert listed["count"] >= 1
    assert any(t["id"] == tid for t in listed["tasks"])


def test_get_task_detail(db_path):
    c = _client()
    with patch("core.orchestrator.emit"):
        tid = c.post("/api/v1/tasks", json={"goal": "g", "repo": "sisyphus"}).json()["task_id"]
    detail = c.get(f"/api/v1/tasks/{tid}").json()
    assert detail["task"]["id"] == tid
    assert detail["messages"] == []
    assert "Status: active" in detail["rendered"]


def test_get_unknown_task_404(db_path):
    assert _client().get("/api/v1/tasks/nope").status_code == 404


def test_post_task_message_emits_human_replied(db_path):
    from core import store

    c = _client()
    with patch("core.orchestrator.emit"):
        tid = c.post("/api/v1/tasks", json={"goal": "g", "repo": "sisyphus"}).json()["task_id"]
    with patch("core.orchestrator.emit") as mock_emit:
        r = c.post(f"/api/v1/tasks/{tid}/messages", json={"text": "here's the answer"})
    assert r.status_code == 200
    mock_emit.assert_called_once_with(tid, "human_replied")
    thread = store.get_thread(tid)
    assert thread[-1]["author"] == "human"
    assert thread[-1]["kind"] == "answer"
    assert thread[-1]["text"] == "here's the answer"


def test_post_empty_task_message_rejected(db_path):
    c = _client()
    with patch("core.orchestrator.emit"):
        tid = c.post("/api/v1/tasks", json={"goal": "g", "repo": "sisyphus"}).json()["task_id"]
    assert c.post(f"/api/v1/tasks/{tid}/messages", json={"text": "   "}).status_code == 422


def test_post_message_to_unknown_task_404(db_path):
    assert _client().post("/api/v1/tasks/nope/messages", json={"text": "x"}).status_code == 404


def test_delete_task(db_path):
    c = _client()
    with patch("core.orchestrator.emit"):
        tid = c.post("/api/v1/tasks", json={"goal": "g", "repo": "sisyphus"}).json()["task_id"]
    with patch("core.worker.cleanup_task") as mock_cleanup:
        r = c.request("DELETE", f"/api/v1/tasks/{tid}")
    assert r.status_code == 200
    assert r.json()["success"] is True
    mock_cleanup.assert_called_once_with(tid, deep=True)
    assert c.get(f"/api/v1/tasks/{tid}").status_code == 404


def test_delete_unknown_task_404(db_path):
    assert _client().request("DELETE", "/api/v1/tasks/nope").status_code == 404


def test_runner_not_found_404(db_path):
    init_runners_table()
    assert _client().post("/api/v1/runners/nope/set-default").status_code == 404


def test_create_task_with_valid_agent(db_path):
    c = _client()
    with patch("core.orchestrator.emit"):
        r = c.post("/api/v1/tasks", json={"goal": "solo", "repo": "sisyphus", "agent": "engineer"})
    assert r.status_code == 200


def test_create_task_with_unknown_agent_rejected(db_path):
    c = _client()
    with patch("core.orchestrator.emit") as mock_emit:
        r = c.post("/api/v1/tasks", json={"goal": "solo", "repo": "sisyphus", "agent": "nope"})
    assert r.status_code == 422
    mock_emit.assert_not_called()


def test_create_task_with_orchestrator_as_agent_rejected(db_path):
    """The orchestrator isn't a worker — can't pin it as a solo handler."""
    c = _client()
    with patch("core.orchestrator.emit"):
        r = c.post("/api/v1/tasks", json={"goal": "solo", "repo": "sisyphus", "agent": "orchestrator"})
    assert r.status_code == 422
