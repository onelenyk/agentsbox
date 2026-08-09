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
from core import mergerequests


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
    assert "/dashboard" in response.text


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


def test_task_detail_two_lane_thread(db_path):
    """Machinery collapses into a <details> run, dialogue stays a bubble —
    but the machinery text must remain in the DOM (untruncated)."""
    from core import store

    task = _make_task(goal="two lanes")
    store.post_message(task["id"], "system", kind="worker_started",
                       text="spawning engineer: do the thing")
    store.post_message(task["id"], "engineer", kind="note",
                       text="running the test suite now")
    store.post_message(task["id"], "engineer", kind="result",
                       text="APPROVED — all checks pass")
    client = _make_client()
    html = client.get(f"/api/tasks/{task['id']}/detail").text
    # dialogue lane: the result renders (markdown pipeline) with its badge
    assert "all checks pass" in html
    assert "badge--ok" in html
    # machinery lane: collapsed but full text still present in the DOM
    assert "<details" in html
    assert "spawning engineer: do the thing" in html
    assert "running the test suite now" in html


def test_new_task_form_allows_no_repo(db_path):
    """The New Task form offers a repo-less option (backend #3 support)."""
    client = _make_client()
    response = client.get("/api/tasks/new-form")
    assert response.status_code == 200
    # a non-required select with an explicit None option
    assert "research / planning" in response.text.lower()
    assert 'name="repo" required' not in response.text


def test_parent_detail_lists_subtasks(db_path):
    from core import store
    parent = store.create_task("ticket PROJ-123")
    ios = store.create_task("iOS impl", repo="ios-app", parent_id=parent["id"])
    store.set_status(ios["id"], "done")
    store.create_task("Android impl", repo="android-app", parent_id=parent["id"])

    r = _make_client().get(f"/api/tasks/{parent['id']}/detail")
    assert "Subtasks" in r.text
    assert "iOS impl" in r.text and "android-app" in r.text
    assert "1/2 done" in r.text


def test_child_detail_shows_parent_breadcrumb(db_path):
    from core import store
    parent = store.create_task("ticket PROJ-123")
    ios = store.create_task("iOS impl", repo="ios-app", parent_id=parent["id"])

    r = _make_client().get(f"/api/tasks/{ios['id']}/detail")
    assert "Part of" in r.text
    assert f"/tasks/{parent['id']}" in r.text


def test_board_column_unit_mappings(db_path):
    from web.routes import board_column

    assert board_column({"status": "active"}, None) == "in-progress"
    assert board_column({"status": "waiting"}, None) == "needs-you"
    assert board_column({"status": "done"}, None) == "done"
    assert board_column({"status": "done"}, {"status": "open"}) == "to-merge"
    assert board_column({"status": "done"}, {"status": "merged"}) == "shipped"
    assert board_column({"status": "failed"}, None) == "failed"


def test_board_lists_done_task_with_open_mr_under_to_merge(db_path):
    from core import store
    task = store.create_task("a task", repo="test-repo")
    store.set_status(task["id"], "done")
    mergerequests.create_mr(task["id"], "test-repo", "feature/x", "main")

    r = _make_client().get("/api/tasks/list")
    assert r.status_code == 200
    assert "board-col--to-merge" in r.text
    assert task["id"] in r.text


def test_board_hides_children_and_shows_parent_chip(db_path):
    from core import store
    with patch("core.orchestrator.emit"):
        parent = store.create_task("ticket")
        child = store.create_task("iOS", repo="ios-app", parent_id=parent["id"])

    r = _make_client().get("/api/tasks/list")
    assert parent["id"] in r.text        # parent shown
    assert child["id"] not in r.text     # child hidden from top-level board
    assert "0/1" in r.text               # parent's subtask progress chip


# ── Inbox (needs-you feed) ───────────────────────────────────────────────


def test_inbox_shows_waiting_task_with_reply_form(db_path):
    from core import store
    task = store.create_task("needs an answer", repo="test-repo")
    store.set_status(task["id"], "waiting")
    store.post_message(task["id"], "orchestrator", kind="question", text="Which colour?")

    r = _make_client().get("/api/inbox/view")
    assert r.status_code == 200
    assert "Needs you (1)" in r.text
    assert "needs an answer" in r.text
    assert "Which colour?" in r.text
    # inline reply form posts to the existing messages endpoint
    assert f"/api/tasks/{task['id']}/messages" in r.text


def test_inbox_shows_done_task_with_open_mr_under_to_merge(db_path):
    from core import store
    task = store.create_task("mergeable work", repo="test-repo")
    store.set_status(task["id"], "done")
    mr = mergerequests.create_mr(task["id"], "test-repo", "feature/inbox", "main")

    r = _make_client().get("/api/inbox/view")
    assert r.status_code == 200
    assert "To merge (1)" in r.text
    assert "mergeable work" in r.text
    assert "feature/inbox" in r.text
    assert f"/api/mr/{mr['id']}/merge" in r.text


def test_inbox_empty_state(db_path):
    r = _make_client().get("/api/inbox/view")
    assert r.status_code == 200
    assert "Inbox zero" in r.text


def test_external_event_renders_distinctly(db_path):
    """A forge webhook's [external] message stands out in the thread."""
    from core import store

    task = _make_task(goal="ship it")
    store.post_message(task["id"], "system", kind="external",
                       text="[external] GitHub CI failed: build")
    client = _make_client()
    response = client.get(f"/api/tasks/{task['id']}/detail")
    assert "[external] GitHub CI failed" in response.text
    assert "🔔" in response.text  # distinct forge-event marker


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
    # the diff opens in an in-app dialog (htmx), not a separate tab
    assert f"/api/tasks/{task['id']}/diff/change.patch" in response.text
    assert "abc12345" in response.text


def test_diff_modal_colourises_patch(db_path):
    from core import store, attachments
    task = _make_task(goal="ship")
    attachments.save_text(task["id"], "change.patch",
                          "diff --git a/x b/x\n@@ -0,0 +1,2 @@\n+added line\n-removed line\n context")
    r = _make_client().get(f"/api/tasks/{task['id']}/diff/change.patch")
    assert r.status_code == 200
    assert "modal--wide" in r.text and "class=\"diff\"" in r.text
    assert "dl add" in r.text and "dl del" in r.text and "dl hunk" in r.text


def test_diff_modal_missing_attachment(db_path):
    task = _make_task(goal="x")
    r = _make_client().get(f"/api/tasks/{task['id']}/diff/nope.patch")
    assert r.status_code == 200
    assert "not found" in r.text.lower()


def test_agents_page_and_list(db_path):
    from core.agents import init_agents_table
    init_agents_table()
    client = _make_client()
    assert client.get("/agents").status_code == 200
    listed = client.get("/api/agents/list")
    assert "engineer" in listed.text
    assert "built-in" in listed.text


def test_agents_new_form_has_tool_catalog(db_path):
    from core.agents import init_agents_table
    init_agents_table()
    r = _make_client().get("/api/agents/new-form")
    assert 'name="tools"' in r.text
    assert "get_context" in r.text and "commit" in r.text


def test_agents_create_and_delete_via_form(db_path):
    from core.agents import init_agents_table, get_agent
    init_agents_table()
    client = _make_client()
    r = client.post("/api/agents", data={"name": "docs-writer", "kind": "worker",
                                          "persona": "writes docs", "tools": ["get_context", "finish"]})
    assert r.status_code == 200
    assert get_agent("docs-writer")["tools"] == ["get_context", "finish"]
    assert client.post("/api/agents/docs-writer/delete").status_code == 200
    assert get_agent("docs-writer") is None


def test_agents_delete_builtin_blocked(db_path):
    from core.agents import init_agents_table
    init_agents_table()
    assert _make_client().post("/api/agents/engineer/delete").status_code == 422


def test_new_task_form_has_agent_dropdown(db_path):
    r = _make_client().get("/api/tasks/new-form")
    assert "Auto" in r.text and "orchestrator dispatches" in r.text
    assert "engineer (solo)" in r.text


def test_task_detail_shows_handling_agent(db_path):
    from core import store
    solo = store.create_task("solo task", repo="sisyphus", agent="engineer")
    auto = store.create_task("auto task", repo="sisyphus")
    c = _make_client()
    assert "engineer (solo)" in c.get(f"/api/tasks/{solo['id']}/detail").text
    assert "orchestrator" in c.get(f"/api/tasks/{auto['id']}/detail").text


# ── Command Bar: classify_command fallback ───────────────────────────────


def test_classify_command_fallback_automation():
    from core.ai_assist import classify_command
    with patch("core.ai_assist.run_prompt", side_effect=RuntimeError("no runner")):
        result = classify_command("run schema checks every day and report", [])
    assert result["kind"] == "automation"


def test_classify_command_fallback_mission():
    from core.ai_assist import classify_command
    long_text = (
        "Redesign the entire authentication system.\n"
        + "First, audit current endpoints.\n"
        + "Then design new OAuth flow.\n"
        + "Finally, migrate all users.\n"
        + ("x" * 200)
    )
    with patch("core.ai_assist.run_prompt", side_effect=RuntimeError("no runner")):
        result = classify_command(long_text, [])
    assert result["kind"] == "mission"


def test_classify_command_fallback_task():
    from core.ai_assist import classify_command
    with patch("core.ai_assist.run_prompt", side_effect=RuntimeError("no runner")):
        result = classify_command("fix the login button color", [])
    assert result["kind"] == "task"


def test_classify_command_fallback_title_truncation():
    from core.ai_assist import classify_command
    very_long_line = "This is a very long first line that definitely exceeds eighty characters for sure"
    with patch("core.ai_assist.run_prompt", side_effect=RuntimeError("no runner")):
        result = classify_command(very_long_line, [])
    assert len(result["title"]) <= 80
    assert result["title"] == very_long_line[:80]


# ── Command Bar: route tests ─────────────────────────────────────────────


def test_command_classify_renders_card(db_path):
    client = _make_client()
    mock_result = {"kind": "task", "title": "Fix login", "goal": "fix login button", "repo": "", "cron": ""}
    mock_repos = [{"name": "sisyphus", "source": "x", "branch": "main"}]
    with patch("core.ai_assist.classify_command", return_value=mock_result):
        with patch("core.repos.list_repos", return_value=mock_repos):
            response = client.post("/api/command/classify", data={"text": "fix login"})
    assert response.status_code == 200
    assert "class=" in response.text
    assert "cmd-badge" in response.text


def test_command_create_task(db_path):
    from core import store
    client = _make_client()
    with patch("core.orchestrator.emit") as mock_emit:
        response = client.post("/api/command/create-task", data={
            "goal": "command-created task",
            "repo": "sisyphus",
            "title": "My Task",
        })
    assert response.status_code == 200
    redirect = response.headers.get("HX-Redirect", "")
    assert redirect.startswith("/tasks/")
    task_id = redirect.split("/tasks/")[1]
    task = store.get_task(task_id)
    assert task is not None
    assert task["goal"] == "command-created task"
    assert task["title"] == "My Task"
    mock_emit.assert_called_once()


class TestReopenAndWorkingIndicator:
    def test_reopen_flips_done_to_waiting_with_note(self, db_path):
        from core import store
        t = store.create_task("g")
        store.set_status(t["id"], "done")
        r = _make_client().post(f"/api/tasks/{t['id']}/reopen")
        assert r.status_code == 200
        assert store.get_task(t["id"])["status"] == "waiting"
        assert "reopened" in store.get_thread(t["id"])[-1]["text"]

    def test_reopen_rejected_for_active_task(self, db_path):
        from core import store
        t = store.create_task("g")
        assert _make_client().post(f"/api/tasks/{t['id']}/reopen").status_code == 409

    def test_working_indicator_shows_running_role(self, db_path):
        from core import store
        t = store.create_task("g")
        store.set_status(t["id"], "active")
        store.post_message(t["id"], "system", kind="worker_started",
                           text="spawning engineer: do the thing")
        html = _make_client().get(f"/api/tasks/{t['id']}/detail").text
        assert "engineer" in html and "working" in html

    def test_queued_indicator_shows_queued_label(self, db_path):
        from core import store
        t = store.create_task("g")
        store.set_status(t["id"], "active")
        store.post_message(t["id"], "system", kind="worker_queued",
                           text="reviewer queued — waiting for a free worker slot")
        html = _make_client().get(f"/api/tasks/{t['id']}/detail").text
        assert "reviewer" in html and "queued" in html


# ── Live workers endpoints ────────────────────────────────────────────


class TestWorkersLive:
    def test_shows_minted_session_role_and_instruction(self, db_path):
        from core import sessions, store
        sessions.clear()
        task = store.create_task("live task")
        sessions.mint(task["id"], "engineer", instruction="fix the bug", kind="worker")

        r = _make_client().get("/api/workers/live")
        assert r.status_code == 200
        assert "engineer" in r.text
        assert "fix the bug" in r.text
        assert "worker" in r.text

    def test_shows_empty_state_when_cleared(self, db_path):
        from core import sessions
        sessions.clear()

        r = _make_client().get("/api/workers/live")
        assert r.status_code == 200
        assert "No agents running" in r.text

    def test_token_never_in_response(self, db_path):
        from core import sessions, store
        sessions.clear()
        task = store.create_task("secret task")
        token = sessions.mint(task["id"], "reviewer", instruction="review")
        s = sessions.resolve(token)
        assert s is not None and s.get("token") == token

        r = _make_client().get("/api/workers/live")
        assert r.status_code == 200
        assert token not in r.text

    def test_decider_kind_badge_shown(self, db_path):
        from core import sessions, store
        sessions.clear()
        task = store.create_task("decider task")
        sessions.mint(task["id"], "orchestrator", instruction="deciding next step", kind="decider")

        r = _make_client().get("/api/workers/live")
        assert r.status_code == 200
        assert "decider" in r.text
        assert "orchestrator" in r.text


class TestThreeDoorNav:
    def test_sidebar_has_exactly_three_doors(self, db_path):
        html = _make_client().get("/dashboard").text
        nav = html.split('<nav class="nav">')[1].split("</nav>")[0]
        assert nav.count("<a ") == 3
        for label in ("Home", "Tasks", "System"):
            assert label in nav

    def test_system_redirects_to_first_tab(self, db_path):
        r = _make_client().get("/system")
        assert "url=/agents" in r.text

    def test_system_pages_show_tabstrip_and_active_door(self, db_path):
        html = _make_client().get("/repos").text
        assert 'class="tabstrip"' in html
        for label in ("Agents", "Runners", "Repos", "Autopilot", "Statistics", "Daemon", "Backups"):
            assert label in html
        nav = html.split('<nav class="nav">')[1].split("</nav>")[0]
        assert nav.count("active") == 1 and "System" in nav

    def test_tasks_pages_show_board_planner_tabs(self, db_path):
        html = _make_client().get("/tasks").text
        assert 'class="tabstrip"' in html
        assert ">Board</a>" in html and ">Planner</a>" in html

    def test_dashboard_has_no_tabstrip(self, db_path):
        assert 'class="tabstrip"' not in _make_client().get("/dashboard").text


class TestFinishedGraceBadge:
    def test_recent_worker_finished_shows_grace_badge(self, db_path):
        from core import store
        t = store.create_task("g")
        store.set_status(t["id"], "active")
        store.post_message(t["id"], "system", kind="worker_started", text="spawning engineer: x")
        store.post_message(t["id"], "system", kind="worker_finished", text="engineer finished")
        html = _make_client().get(f"/api/tasks/{t['id']}/detail").text
        assert "engineer finished" in html and "m ago" in html

    def test_no_grace_badge_while_worker_runs(self, db_path):
        from core import store
        t = store.create_task("g")
        store.set_status(t["id"], "active")
        store.post_message(t["id"], "system", kind="worker_started", text="spawning engineer: x")
        assert "m ago" not in _make_client().get(f"/api/tasks/{t['id']}/detail").text
