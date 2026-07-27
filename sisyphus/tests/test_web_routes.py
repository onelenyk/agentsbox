"""
API endpoint tests for FastAPI web routes using TestClient.

All tests use the ``db_path`` fixture from conftest for DB setup.
"""

import os
import sys
from unittest.mock import patch

# Ensure sisyphus is importable (same as conftest)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient
from web.app import app

from core.runners import init_runners_table


# ── Fixture: TestClient ──────────────────────────────────────────────────


def _make_client():
    """Create a fresh TestClient — called inside a test so DB_PATH is set."""
    return TestClient(app)


# ── Tests: GET /api/repos/list ───────────────────────────────────────────


def test_repos_list(db_path):
    """Repos list returns 200 with HTML content."""
    client = _make_client()
    mock_repos = [
        {"name": "test-repo", "source": "https://example.com/repo.git", "branch": "main"},
    ]
    with patch("core.repos.list_repos", return_value=mock_repos):
        response = client.get("/api/repos/list")
    assert response.status_code == 200
    # HTML template rendered successfully
    assert response.text


# ── Tests: GET /api/repos/health ─────────────────────────────────────────


def test_repos_health(db_path):
    """Repos health returns 200 with JSON containing total/cloned/pending."""
    client = _make_client()
    mock_repos = [
        {"name": "r1", "cloned": True},
        {"name": "r2", "cloned": False},
        {"name": "r3", "cloned": False},
    ]
    with patch("core.repos.list_repos", return_value=mock_repos):
        response = client.get("/api/repos/health")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 3
    assert data["cloned"] == 1
    assert data["pending"] == 2


# ── Tests: GET /api/runners/list ─────────────────────────────────────────


def test_runners_list(db_path):
    """Runners list returns 200 with HTML content."""
    init_runners_table()
    client = _make_client()
    response = client.get("/api/runners/list")
    assert response.status_code == 200
    assert response.text


# ── Tests: GET / (root redirect) ─────────────────────────────────────────


def test_root(db_path):
    """Root URL returns 200 with redirect HTML to /tasks."""
    client = _make_client()
    response = client.get("/")
    assert response.status_code == 200
    assert "refresh" in response.text.lower()
    assert "/tasks" in response.text


# ── Tests: Tasks (Task+Thread system) ────────────────────────────────────


def _make_task(goal="do the thing", repo=""):
    from core import store
    return store.create_task(goal, repo)


def test_tasks_page(db_path):
    client = _make_client()
    response = client.get("/tasks")
    assert response.status_code == 200


def test_tasks_list_view(db_path):
    _make_task(goal="visible in list")
    client = _make_client()
    response = client.get("/api/tasks/list")
    assert response.status_code == 200
    assert "visible in list" in response.text


def test_tasks_create_via_form(db_path):
    client = _make_client()
    with patch("core.orchestrator.emit") as mock_emit:
        response = client.post("/api/tasks", data={"goal": "form-created task", "repo": "sisyphus"})
    assert response.status_code == 200
    assert "taskCreated" in response.headers.get("HX-Trigger", "")
    mock_emit.assert_called_once()

    listed = client.get("/api/tasks/list")
    assert "form-created task" in listed.text


def test_tasks_create_via_form_without_repo_allowed(db_path):
    """Repo-less tasks run in a scratch dir — allowed, same rule as the JSON API."""
    client = _make_client()
    with patch("core.orchestrator.emit") as mock_emit:
        response = client.post("/api/tasks", data={"goal": "no repo task", "repo": ""})
    assert response.status_code == 200
    mock_emit.assert_called_once()


def test_tasks_create_via_form_without_goal_rejected(db_path):
    client = _make_client()
    with patch("core.orchestrator.emit") as mock_emit:
        response = client.post("/api/tasks", data={"goal": "  ", "repo": "sisyphus"})
    assert response.status_code == 422
    mock_emit.assert_not_called()


def test_task_page_existing(db_path):
    task = _make_task(goal="page task")
    client = _make_client()
    response = client.get(f"/tasks/{task['id']}")
    assert response.status_code == 200
    assert "page task" in response.text


def test_task_page_nonexistent(db_path):
    client = _make_client()
    response = client.get("/tasks/nope")
    assert response.status_code == 404


def test_task_detail_view_shows_thread(db_path):
    from core import store

    task = _make_task(goal="thread task")
    store.post_message(task["id"], "orchestrator", kind="note", text="visible in thread")
    client = _make_client()
    response = client.get(f"/api/tasks/{task['id']}/detail")
    assert response.status_code == 200
    assert "thread task" in response.text
    assert "visible in thread" in response.text


def test_task_detail_view_nonexistent(db_path):
    client = _make_client()
    response = client.get("/api/tasks/nope/detail")
    assert response.status_code == 200
    assert "not found" in response.text.lower()


def test_task_reply_posts_message_and_triggers(db_path):
    from core import store

    task = _make_task()
    client = _make_client()
    with patch("core.orchestrator.emit") as mock_emit:
        response = client.post(f"/api/tasks/{task['id']}/messages", data={"text": "my reply"})
    assert response.status_code == 200
    assert "taskUpdated" in response.headers.get("HX-Trigger", "")
    mock_emit.assert_called_once_with(task["id"], "human_replied")

    thread = store.get_thread(task["id"])
    assert thread[-1]["text"] == "my reply"
    assert thread[-1]["author"] == "human"


def test_task_reply_empty_rejected(db_path):
    task = _make_task()
    client = _make_client()
    response = client.post(f"/api/tasks/{task['id']}/messages", data={"text": "   "})
    assert response.status_code == 422


def test_task_reply_unknown_task_404(db_path):
    client = _make_client()
    response = client.post("/api/tasks/nope/messages", data={"text": "x"})
    assert response.status_code == 404


def test_task_delete_removes_and_redirects(db_path):
    from core import store
    task = _make_task()
    client = _make_client()
    with patch("core.worker.cleanup_task") as mock_cleanup:
        response = client.post(f"/api/tasks/{task['id']}/delete")
    assert response.status_code == 200
    assert response.headers.get("HX-Redirect") == "/tasks"
    mock_cleanup.assert_called_once_with(task["id"], deep=True)
    assert store.get_task(task["id"]) is None


def test_task_delete_unknown_404(db_path):
    client = _make_client()
    assert client.post("/api/tasks/nope/delete").status_code == 404


def test_task_detail_shows_delete_button(db_path):
    task = _make_task(goal="deletable")
    client = _make_client()
    response = client.get(f"/api/tasks/{task['id']}/detail")
    assert f"/api/tasks/{task['id']}/delete" in response.text


def test_task_detail_shows_result_card_with_diff_link(db_path):
    from core import store
    task = _make_task(goal="ship")
    store.post_message(task["id"], "engineer", kind="commit", text="add x (abc12345)")
    store.post_message(task["id"], "engineer", kind="mr",
                       text="Add x\n\nBranch: sisyphus/x", attachment="change.patch")
    store.post_message(task["id"], "engineer", kind="result", text="done and dusted")
    client = _make_client()
    response = client.get(f"/api/tasks/{task['id']}/detail")
    assert "Result" in response.text
    assert "done and dusted" in response.text
    assert f"/api/v1/tasks/{task['id']}/attachments/change.patch" in response.text
    assert "abc12345" in response.text
