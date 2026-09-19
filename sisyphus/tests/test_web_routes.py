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


# ── Tests: runner add / detect / type list ───────────────────────────────


def test_runners_new_form_offers_only_launchable_types(db_path):
    """The form must not advertise a type that has no launch profile."""
    from core.runners import RUNNER_TYPES
    init_runners_table()
    client = _make_client()
    body = client.get("/api/runners/new-form").text
    for t in RUNNER_TYPES:
        assert f'value="{t}"' in body
    for gone in ("ollama", "openai-api", "custom"):
        assert f'value="{gone}"' not in body


def test_runners_add_claude(db_path):
    """A Claude runner can be added from the page and lands with its own type."""
    from core.runners import get_runner_by_name
    init_runners_table()
    client = _make_client()
    r = client.post("/api/runners", data={
        "name": "claude", "binary": "claude", "type": "claude-cli",
        "model": "", "api_key_env": "", "base_url": "",
    })
    assert r.status_code == 200
    row = get_runner_by_name("claude")
    assert row is not None and row["type"] == "claude-cli"


def test_runners_add_rejects_unlaunchable_type_readably(db_path):
    init_runners_table()
    client = _make_client()
    r = client.post("/api/runners", data={
        "name": "nope", "binary": "ollama", "type": "ollama",
    })
    assert r.status_code == 400
    # The reason rides HX-Trigger so the page can show it, not a bare 500.
    assert "runnerError" in r.headers.get("HX-Trigger", "")
    assert "ollama" in r.headers["HX-Trigger"]


def test_runners_add_duplicate_name_is_reported(db_path):
    init_runners_table()
    client = _make_client()
    data = {"name": "dup", "binary": "claude", "type": "claude-cli"}
    assert client.post("/api/runners", data=data).status_code == 200
    r = client.post("/api/runners", data=data)
    assert r.status_code == 400
    assert "already exists" in r.headers.get("HX-Trigger", "")


def test_runners_detect_never_suggests_openrouter_key_for_claude(db_path):
    """The autodetect panel used to hardcode OPENROUTER_API_KEY for every
    binary it found — for a Claude row that key would be forwarded as
    ANTHROPIC_API_KEY."""
    init_runners_table()
    client = _make_client()
    detected = [{"name": "claude", "type": "claude-cli", "binary": "claude",
                 "path": "/usr/local/bin/claude"}]
    with patch("core.runner_detect.detect", return_value=detected):
        body = client.get("/api/runners/detect").text
    assert "claude-cli" in body
    assert "OPENROUTER_API_KEY" not in body


def test_runners_detect_matches_configured_by_resolved_path(db_path):
    """A row storing an absolute path is the same runtime detect() reports by
    bare name — the panel must not offer to add it again."""
    from core.runners import add_runner
    init_runners_table()
    add_runner("mine", "/opt/bin/opencode", "opencode")
    client = _make_client()
    detected = [{"name": "opencode", "type": "opencode", "binary": "opencode",
                 "path": "/opt/bin/opencode"}]
    with patch("core.runner_detect.detect", return_value=detected), \
         patch("core.runner_detect.resolve", side_effect=lambda b: b if b.startswith("/") else "/opt/bin/opencode"):
        body = client.get("/api/runners/detect").text
    assert "configured" in body
    assert "+ Add as runtime" not in body


def test_runners_detect_suggests_free_name_on_collision(db_path):
    from core.runners import add_runner
    init_runners_table()
    add_runner("claude", "something-else", "opencode")
    client = _make_client()
    detected = [{"name": "claude", "type": "claude-cli", "binary": "claude",
                 "path": "/usr/local/bin/claude"}]
    with patch("core.runner_detect.detect", return_value=detected):
        body = client.get("/api/runners/detect").text
    assert "claude-2" in body


# ── Tests: GET / (root redirect) ─────────────────────────────────────────


def test_root(db_path):
    """Root URL returns 200 with redirect HTML to /tasks."""
    client = _make_client()
    response = client.get("/")
    assert response.status_code == 200
    assert "refresh" in response.text.lower()
    assert "/dashboard" in response.text


# ── Tests: styled 404 for the HTML surface ───────────────────────────────


def test_unknown_page_renders_html_404_with_section_links(db_path):
    """A mistyped URL (e.g. /autopilot instead of /autopilots) gets a styled
    page with a way back — not a raw JSON dead end. Only a browser (Accept:
    text/html) outside /api gets it."""
    client = _make_client()
    response = client.get("/autopilot", headers={"Accept": "text/html"})
    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]
    assert "does not exist" in response.text
    for section in ("/dashboard", "/tasks", "/plans", "/agents", "/runners",
                    "/repos", "/autopilots", "/stats", "/runtime", "/backups"):
        assert section in response.text


def test_unknown_api_path_still_returns_json_404(db_path):
    """API/JSON callers keep the default 404 exactly as before — the styled
    page must never leak into /api paths."""
    client = _make_client()
    response = client.get("/api/v1/nonexistent", headers={"Accept": "text/html"})
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_unknown_page_without_html_accept_stays_json(db_path):
    """A non-browser client (default Accept) is untouched: raw JSON 404, no page."""
    client = _make_client()
    response = client.get("/nonexistent")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


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


def test_result_body_rendered_once_on_detail(db_path):
    """The result body renders exactly once on the detail page: the top RESULT
    block is the single full rendering, and the engineer/result thread message
    collapses to a one-line "shown above" row instead of duplicating it."""
    from core import store

    task = _make_task(goal="single result")
    store.post_message(task["id"], "engineer", kind="result",
                       text="**RESULT MARKER** body")
    store.post_message(task["id"], "system", kind="worker_finished",
                       text="engineer finished")
    client = _make_client()
    html = client.get(f"/api/tasks/{task['id']}/detail").text
    # the top RESULT block renders the markdown exactly once
    assert html.count("<p><strong>RESULT MARKER</strong> body</p>") == 1
    # the thread collapses the result to a one-line "shown above" row…
    assert "shown above" in html
    assert 'id="result-' in html
    assert 'msg__kind">/result' in html
    # …and keeps only the RAW source behind the fold, not another rendering
    assert html.count("**RESULT MARKER** body") == 1


def test_diffstat_body_renders_as_compact_table(db_path):
    """Diffstat-shaped message bodies ("path | 5 +++++") render as a compact
    monospace table instead of raw prose."""
    from core import store

    task = _make_task(goal="diffstat result")
    store.post_message(
        task["id"], "engineer", kind="result",
        text="web/templates/task_detail_content.html | 5 +++++\n"
             "web/static/app.css | 3 +++",
    )
    client = _make_client()
    html = client.get(f"/api/tasks/{task['id']}/detail").text
    assert "diffstat-table" in html
    assert "ds-path" in html and "ds-bar" in html
    assert "task_detail_content.html" in html
    assert "+++++" in html
    # the machinery/dialogue source text is NOT dumped as a raw pre line
    assert "5 +++++" not in html


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
    # the reply field survives the 10s inbox refresh: stable task-derived id + hx-preserve
    assert f'id="reply-text-{task["id"]}"' in r.text
    assert "hx-preserve" in r.text


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


def _open_mr(review_status=None):
    from core import store
    task = store.create_task("merge it", repo="test-repo")
    mr = mergerequests.create_mr(task["id"], "test-repo", "feature/x", "main")
    if review_status is not None:
        mergerequests.set_review_status(mr["id"], review_status)
    return mr


def test_mr_merge_refused_for_changes_requested_without_force(db_path):
    mr = _open_mr(review_status="changes_requested")
    client = _make_client()
    with patch("core.mergerequests.merge_branch") as mock_merge:
        r = client.post(f"/api/mr/{mr['id']}/merge")
    assert r.status_code == 200
    assert "review is changes_requested" in r.text
    assert "merge blocked" in r.text
    mock_merge.assert_not_called()
    assert mergerequests.get_mr(mr["id"])["status"] == "open"


def test_mr_merge_refused_for_pending_without_force(db_path):
    mr = _open_mr(review_status="pending")
    client = _make_client()
    with patch("core.mergerequests.merge_branch") as mock_merge:
        r = client.post(f"/api/mr/{mr['id']}/merge")
    assert r.status_code == 200
    assert "review is pending" in r.text
    mock_merge.assert_not_called()
    assert mergerequests.get_mr(mr["id"])["status"] == "open"


def test_mr_merge_refused_when_reviewer_died_without_force(db_path):
    mr = _open_mr(review_status="died")
    client = _make_client()
    with patch("core.mergerequests.merge_branch") as mock_merge:
        r = client.post(f"/api/mr/{mr['id']}/merge")
    assert r.status_code == 200
    assert "reviewer died" in r.text
    mock_merge.assert_not_called()
    assert mergerequests.get_mr(mr["id"])["status"] == "open"


def test_mr_modal_says_a_review_died_not_silence(db_path):
    mr = _open_mr(review_status="died")
    r = _make_client().get(f"/api/mr/{mr['id']}/view")
    assert r.status_code == 200
    assert "review died" in r.text


def test_mr_merge_allowed_for_changes_requested_with_force(db_path):
    mr = _open_mr(review_status="changes_requested")
    client = _make_client()
    with patch("core.mergerequests.merge_branch",
               return_value={"ok": True, "sha": "abc12345", "target": "main"}):
        r = client.post(f"/api/mr/{mr['id']}/merge", data={"force": "1"})
    assert r.status_code == 200
    assert mergerequests.get_mr(mr["id"])["status"] == "merged"


def test_mr_merge_allowed_for_approved(db_path):
    mr = _open_mr(review_status="approved")
    client = _make_client()
    with patch("core.mergerequests.merge_branch",
               return_value={"ok": True, "sha": "abc12345", "target": "main"}):
        r = client.post(f"/api/mr/{mr['id']}/merge")
    assert r.status_code == 200
    assert mergerequests.get_mr(mr["id"])["status"] == "merged"


def test_mr_merge_blocked_when_no_review_is_recorded(db_path):
    """NULL means nobody ever reviewed. It used to pass every gate — all three
    of them checked for the two verdicts they could name and let the absence of
    a verdict through, which is how twelve of the twenty-six merges in the
    ledger ended up citing nothing at all."""
    mr = _open_mr(review_status=None)
    client = _make_client()
    with patch("core.mergerequests.merge_branch",
               return_value={"ok": True, "sha": "abc12345", "target": "main"}):
        r = client.post(f"/api/mr/{mr['id']}/merge")
    assert r.status_code == 200
    assert mergerequests.get_mr(mr["id"])["status"] == "open"
    assert "no review verdict is recorded" in r.text
    assert "Merge anyway" in r.text          # the override is still one click away


def test_mr_merge_blocked_when_the_recorded_check_failed(db_path):
    """An approval an agent typed does not outrank a number the daemon
    produced."""
    mr = _open_mr(review_status="approved")
    mergerequests.set_check_result(mr["id"], "failed", output="3 failed",
                                   command="pytest -q")
    client = _make_client()
    with patch("core.mergerequests.merge_branch",
               return_value={"ok": True, "sha": "abc12345", "target": "main"}):
        r = client.post(f"/api/mr/{mr['id']}/merge")
    assert mergerequests.get_mr(mr["id"])["status"] == "open"
    assert "check FAILED" in r.text


def test_the_modal_shows_the_verdict_a_merge_would_rest_on(db_path):
    """A human pressing Merge could not see the daemon's own check at all: it
    lived on the MR row and in the origin task's thread, never in the modal
    where the decision is made."""
    mr = _open_mr(review_status="approved")
    mergerequests.set_check_result(mr["id"], "passed", output="1097 passed",
                                   command="pytest -q", head="abc123def456")
    r = _make_client().get(f"/api/mr/{mr['id']}/view")
    assert "review approved" in r.text
    assert "check passed (pytest -q)" in r.text and "abc123de" in r.text


def test_mr_modal_renders_merge_anyway_for_changes_requested(db_path):
    mr = _open_mr(review_status="changes_requested")
    client = _make_client()
    r = client.get(f"/api/mr/{mr['id']}/view")
    assert r.status_code == 200
    assert "Merge anyway" in r.text
    assert "force" in r.text


def test_mr_modal_renders_merge_button_for_approved(db_path):
    mr = _open_mr(review_status="approved")
    client = _make_client()
    r = client.get(f"/api/mr/{mr['id']}/view")
    assert r.status_code == 200
    assert "Merge into" in r.text
    assert "Merge anyway" not in r.text


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
    from core import attachments
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


def test_dashboard_classify_form_has_in_flight_affordances(db_path):
    """The classify command is a slow model call; the form must disable itself
    and show a progress hint while the request is in flight, so it never reads
    as a dead button."""
    html = _make_client().get("/dashboard").text
    form_tag = html.split('hx-post="/api/command/classify"')[1].split(">")[0]
    assert 'hx-disabled-elt="this"' in form_tag
    assert 'hx-indicator="#cmd-classify-indicator"' in form_tag
    assert 'id="cmd-classify-indicator"' in html
    assert "Classifying" in html


def test_command_result_buttons_have_in_flight_affordances(db_path):
    """The create-task/plan/automation buttons each take time too — they must
    disable themselves and show a shared progress hint, matching the classify
    form."""
    mock_result = {"kind": "task", "title": "Fix login", "goal": "fix login button", "repo": "", "cron": ""}
    mock_repos = [{"name": "sisyphus", "source": "x", "branch": "main"}]
    client = _make_client()
    with patch("core.ai_assist.classify_command", return_value=mock_result):
        with patch("core.repos.list_repos", return_value=mock_repos):
            response = client.post("/api/command/classify", data={"text": "fix login"})
    assert response.status_code == 200
    for endpoint in ("create-task", "create-plan", "create-automation"):
        button_tag = response.text.split(f'hx-post="/api/command/{endpoint}"')[1].split(">")[0]
        assert 'hx-disabled-elt="this"' in button_tag
        assert 'hx-indicator="#cmd-working-indicator"' in button_tag
    assert 'id="cmd-working-indicator"' in response.text
    assert "Working" in response.text


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


class TestTaskRetry:
    def test_retry_failed_creates_new_active_task_with_crosslinks(self, db_path):
        from core import store
        old = store.create_task("retry me", repo="sisyphus", agent="engineer")
        store.set_status(old["id"], "failed")
        store.post_message(old["id"], "system", kind="note",
                           text="some old attempt message")
        with patch("core.orchestrator.emit") as mock_emit:
            r = _make_client().post(f"/api/tasks/{old['id']}/retry")
        assert r.status_code == 200
        new_id = r.headers.get("HX-Redirect", "").split("/tasks/")[1]
        new = store.get_task(new_id)
        assert new is not None
        assert new["id"] != old["id"]
        assert new["status"] == "active"
        assert new["goal"] == old["goal"]
        assert new["repo"] == old["repo"]
        assert new["agent"] == old["agent"]
        assert new["base_branch"] is None
        old_thread = store.get_thread(old["id"])
        assert old_thread[-1]["text"] == f"retried from scratch as {new_id}"
        new_thread = store.get_thread(new_id)
        assert len(new_thread) == 1
        assert new_thread[0]["text"] == (f"retry of {old['id']} — previous attempt's "
                                         "thread and branch deliberately ignored")
        mock_emit.assert_called_once_with(new_id, "created")

    def test_retry_rejected_for_active_task(self, db_path):
        from core import store
        t = store.create_task("g")
        assert _make_client().post(f"/api/tasks/{t['id']}/retry").status_code == 409

    def test_retry_button_and_confirm_on_detail_page(self, db_path):
        from core import store
        t = store.create_task("g")
        store.set_status(t["id"], "done")
        html = _make_client().get(f"/api/tasks/{t['id']}/detail").text
        assert f'hx-post="/api/tasks/{t["id"]}/retry"' in html
        assert "Retry from scratch" in html
        assert "clean history" in html


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


class TestFlatNav:
    """The three-door design hid seven pages behind the word "System" — a user
    had to already know Backups lived there to find them. Every section is one
    sidebar click from anywhere now, and the tabstrip is gone with the doors."""

    def test_every_section_is_one_click_from_anywhere(self, db_path):
        html = _make_client().get("/dashboard").text
        nav = html.split('<nav class="nav">')[1].split("</nav>")[0]
        assert nav.count("<a ") == 10
        for label in ("Home", "Tasks", "Planner", "Agents", "Runners", "Repos",
                      "Autopilot", "Statistics", "Daemon", "Backups"):
            assert label in nav

    def test_system_still_redirects_for_old_links(self, db_path):
        r = _make_client().get("/system")
        assert "url=/agents" in r.text

    def test_the_current_page_is_marked_active(self, db_path):
        html = _make_client().get("/repos").text
        nav = html.split('<nav class="nav">')[1].split("</nav>")[0]
        assert nav.count("active") == 1
        assert 'active' in [l for l in nav.splitlines() if "/repos" in l][0]

    def test_the_tabstrip_is_gone(self, db_path):
        for page in ("/dashboard", "/tasks", "/repos"):
            assert 'class="tabstrip"' not in _make_client().get(page).text


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


class TestBoardCards:
    def test_live_card_shows_working_pulse(self, db_path):
        from core import sessions, store
        sessions.clear()
        t = store.create_task("goal line\nmore detail here")
        store.set_status(t["id"], "active")
        sessions.mint(t["id"], "engineer", instruction="do x", kind="worker")
        try:
            html = _make_client().get("/api/tasks/list").text
        finally:
            sessions.clear()
        assert "task-card--live" in html
        assert "engineer working" in html

    def test_waiting_card_shows_question(self, db_path):
        from core import store
        t = store.create_task("g")
        store.post_message(t["id"], "orchestrator", kind="question", text="Which repo should I use?")
        store.set_status(t["id"], "waiting")
        html = _make_client().get("/api/tasks/list").text
        assert "Which repo should I use?" in html

    def test_now_card_shows_stagebar_and_last_event(self, db_path):
        from core import store
        t = store.create_task("Build the thing")
        store.set_status(t["id"], "active")
        store.post_message(t["id"], "engineer", kind="commit", text="feat: half done (abc123)")
        html = _make_client().get("/api/tasks/list").text
        assert "stagebar" in html
        assert "feat: half done" in html          # last dialogue event on the card
        assert 'title="code"' in html             # stage dots labeled


def test_runner_edit_can_clear_optional_fields(db_path):
    """An opencode row picks its provider from the model prefix, so a leftover
    api_key_env/base_url describes a route that does not exist — the UI has to
    be able to remove them. Treating "" as "leave alone" made that impossible."""
    from core.runners import add_runner, get_runner
    init_runners_table()
    r = add_runner("clearable", "opencode", "opencode",
                   api_key_env="OPENROUTER_API_KEY", base_url="https://x")
    client = _make_client()
    resp = client.post(f"/api/runners/{r['id']}/edit", data={
        "name": "clearable", "binary": "opencode", "type": "opencode",
        "model": "", "api_key_env": "", "base_url": "",
    })
    assert resp.status_code == 200
    row = get_runner(r["id"])
    assert row["api_key_env"] == ""
    assert row["base_url"] == ""


def test_runner_edit_keeps_required_fields_on_blank(db_path):
    from core.runners import add_runner, get_runner
    init_runners_table()
    r = add_runner("keepname", "opencode", "opencode")
    client = _make_client()
    client.post(f"/api/runners/{r['id']}/edit", data={"name": "", "binary": "", "type": ""})
    row = get_runner(r["id"])
    assert row["name"] == "keepname" and row["binary"] == "opencode"


# ── The board's watch rail ───────────────────────────────────────────────


class TestWatchRail:
    """The rail reports who is on duty. Every row must come from something the
    system recorded — a live session or a heartbeat a loop stamped."""

    def test_rail_reports_each_registered_watcher(self, db_path):
        from core import heartbeat
        heartbeat.clear()
        heartbeat.register("dispatcher", kind="event", note="waiting for an event")
        heartbeat.register("watchdog", every=120)
        heartbeat.beat("watchdog", note="nothing stalled")
        heartbeat.register("forge poll", every=300)
        heartbeat.mark_off("forge poll", "forge_poll.enabled is false")
        try:
            html = _make_client().get("/api/board/watch").text
        finally:
            heartbeat.clear()
        # the depth is whatever the queue holds right now — assert the shape,
        # not a number another test may have left behind
        assert "dispatcher" in html and "waiting · queue" in html
        assert "nothing stalled" in html
        assert "off · forge_poll.enabled is false" in html
        assert "led dead" not in html          # nothing here is broken

    def test_rail_is_honest_when_nothing_is_running(self, db_path):
        from core import heartbeat, sessions
        heartbeat.clear()
        sessions.clear()
        html = _make_client().get("/api/board/watch").text
        assert "No agents running." in html
        assert "not configured" in html        # the product owner does not exist yet
        assert "the daemon's loops have not started" in html

    def test_rail_lists_a_running_agent(self, db_path):
        from core import heartbeat, sessions, store
        heartbeat.clear()
        sessions.clear()
        t = store.create_task("Wire the rail", repo="sisyphus")
        sessions.mint(t["id"], "engineer", instruction="add the panel",
                      model="deepseek/v4", runner="opencode")
        try:
            html = _make_client().get("/api/board/watch").text
        finally:
            sessions.clear()
        assert "engineer" in html and "add the panel" in html and "deepseek/v4" in html

    def test_board_page_carries_the_rail(self, db_path):
        html = _make_client().get("/tasks").text
        assert 'id="watch"' in html
        assert "/api/board/watch" in html


def test_autopilot_runs_collapse_into_one_group_card(db_path):
    """Two daily crons produce ~60 identical archive cards a month; the board
    folds each autopilot's runs into one row so shipped human work stays
    visible. Live columns never fold — a cron run that needs you is a card."""
    from core import store
    from core.autopilot import create_autopilot
    client = _make_client()
    create_autopilot(name="Nightly repo scan", trigger_type="schedule",
                     cron="0 3 * * *", goal_template="scan")
    runs = [store.create_task("scan all repos", status="done",
                              title=f"Nightly repo scan · 2026-09-{day}")
            for day in ("01", "02", "03")]
    store.create_task("a real delivery", status="done", title="Fix the gate")

    html = client.get("/api/tasks/list").text
    assert html.count("🤖 Nightly repo scan") == 1     # one group row, not three cards
    assert "3 runs" in html
    latest = max(runs, key=lambda t: t["updated_at"])
    assert latest["id"] in html                        # the group opens the latest run
    assert sum(t["id"] in html for t in runs) == 1     # the other two are folded away
    assert "Fix the gate" in html                      # human work stays a card


def test_a_cron_run_that_needs_you_stays_a_card(db_path):
    from core import store
    from core.autopilot import create_autopilot
    client = _make_client()
    create_autopilot(name="morning-mr-review", trigger_type="schedule",
                     cron="7 6 * * *", goal_template="review")
    store.create_task("review MRs", status="waiting",
                      title="morning-mr-review · 2026-09-03")
    html = client.get("/api/tasks/list").text
    assert "morning-mr-review · 2026-09-03" in html    # full card in Needs you
    assert "1 run" not in html
