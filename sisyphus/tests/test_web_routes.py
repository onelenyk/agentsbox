"""
API endpoint tests for FastAPI web routes using TestClient.

All tests use the ``db_path`` fixture from conftest for DB setup.
"""

import os
import sys
from pathlib import Path
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


def _probe_patches(groups=None, login=None):
    groups = groups if groups is not None else [{"id": "alias", "label": "Latest", "models": [
        {"id": "opus", "name": "opus → Claude Opus 5", "ctx": "1M", "price": "$5 / $25"}]}]
    login = login or {"ok": True, "summary": "Signed in as me@example.com", "detail": "via claude.ai"}
    return (patch("core.runner_detect.resolve", return_value="/bin/claude"),
            patch("core.runner_catalog.models", return_value=groups),
            patch("core.runner_catalog.login_status", return_value=login))


def test_runner_form_body_shows_the_runners_models_and_sign_in(db_path):
    init_runners_table()
    a, b, c = _probe_patches()
    with a, b, c:
        body = _make_client().get("/api/runners/form-body?type=claude-cli").text
    assert 'value="opus"' in body and "Claude Opus 5" in body
    assert "Signed in as me@example.com" in body
    assert 'value="login" checked' in body          # a new runner starts on its own sign-in


def test_runner_edit_form_keeps_an_unlisted_model_as_typed(db_path):
    from core.runners import add_runner
    init_runners_table()
    row = add_runner("c", "claude", "claude-cli", model="claude-custom-x")
    a, b, c = _probe_patches()
    with a, b, c:
        body = _make_client().get(f"/api/runners/form-body?type=claude-cli&runner_id={row['id']}").text
    assert 'name="model_custom" value="claude-custom-x"' in body
    assert '<input type="radio" name="model" value="" checked>' in body


def test_runner_added_with_a_pasted_key_keeps_it_in_the_keychain(db_path):
    from core.runners import get_runner_by_name
    init_runners_table()
    with patch("core.keychain.store") as store:
        r = _make_client().post("/api/runners", data={
            "name": "go", "binary": "opencode", "type": "opencode", "auth_mode": "key",
            "api_key": "sk-go-12345678", "model": "opencode-go/qwen3.7-max"})
    assert r.status_code == 200
    row = get_runner_by_name("go")
    assert row["auth_mode"] == "key" and row["model"] == "opencode-go/qwen3.7-max"
    assert "sk-go" not in str(row)
    store.assert_called_once_with(row["id"], "sk-go-12345678")


def test_key_mode_without_a_key_is_refused(db_path):
    init_runners_table()
    r = _make_client().post("/api/runners", data={
        "name": "k", "binary": "claude", "type": "claude-cli", "auth_mode": "key"})
    assert r.status_code == 400 and "paste the API key" in r.headers["HX-Trigger"]


def test_leaving_key_mode_removes_the_stored_key(db_path):
    from core.runners import add_runner, get_runner
    init_runners_table()
    row = add_runner("k", "claude", "claude-cli", auth_mode="key", api_key_env="ANTHROPIC_API_KEY")
    with patch("core.keychain.delete") as delete:
        r = _make_client().post(f"/api/runners/{row['id']}/edit", data={
            "name": "k", "binary": "claude", "type": "claude-cli", "auth_mode": "login",
            "model": "sonnet", "model_custom": ""})
    assert r.status_code == 200
    after = get_runner(row["id"])
    assert after["auth_mode"] == "login" and after["api_key_env"] == "" and after["model"] == "sonnet"
    delete.assert_called_once_with(row["id"])


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
    for section in ("/dashboard", "/tasks", "/agents", "/runners",
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
    assert 'msg__kind">result' in html      # the row still says what kind it is
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


def test_task_box_allows_no_repo(db_path):
    """The task box offers a repo-less option (backend #3 support)."""
    html = _make_client().get("/tasks").text
    box = html.split('class="taskbox"')[1].split("</form>")[0]
    # The box carries the repo list and a repo field that may stay empty — how
    # many repos happen to be configured is not this test's business.
    assert "data-repos=" in box and 'name="repo"' in box
    assert 'name="repo" required' not in box


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
    assert "askc--" in r.text and "/view" in r.text   # it wants a decision from the human
    assert task["id"] in r.text


def test_a_subtask_hangs_under_its_parent(db_path):
    """A plan's steps are the plan, not four more items competing with it in
    the same list."""
    from core import store
    with patch("core.orchestrator.emit"):
        parent = store.create_task("ticket", repo="test-repo")
        child = store.create_task("iOS", repo="test-repo", parent_id=parent["id"])
    for t in (parent, child):
        store.set_status(t["id"], "done")

    html = _make_client().get("/api/tasks/list").text
    assert parent["key"] in html and child["key"] in html
    assert "trow--child" in html                       # the child is drawn as one
    assert html.index(parent["key"]) < html.index(child["key"])   # under its parent


def test_each_status_gets_its_own_surface(db_path):
    """A status is what the task is doing to you, so the four of them are four
    different things on the page, not one card shape in four colours."""
    from core import sessions, store
    sessions.clear()
    client = _make_client()

    fresh = store.create_task("just created", repo="test-repo")           # queued
    asking = store.create_task("needs an answer", repo="test-repo")
    store.post_message(asking["id"], "engineer", kind="question", text="Which database?")
    store.set_status(asking["id"], "waiting")
    running = store.create_task("under way", repo="test-repo")
    store.post_message(running["id"], "engineer", kind="commit", text="feat: half of it")
    store.set_status(running["id"], "active")
    finished = store.create_task("all over", repo="test-repo")
    store.set_status(finished["id"], "done")

    html = client.get("/api/tasks/list").text
    try:
        # it wants you: the question itself, not a badge saying there is one
        assert "Which database?" in html and "askc__reply" in html
        # in flight: progress, and a queued task says it is only queued
        assert "stagebar" in html and "feat: half of it" in html
        assert "queued · waiting for the orchestrator" in html
        assert html.index("just created") > html.index("under way")   # queued below running
        # over: a row in the sprint list, not a card
        assert "trow--done" in html
        for t in (fresh, asking, running, finished):
            assert t["key"] in html         # every task names itself the same way
    finally:
        sessions.clear()


def test_a_quiet_board_says_nothing_is_waiting_on_you(db_path):
    html = _make_client().get("/api/tasks/list").text
    assert "Nothing is waiting on you." in html


def test_every_sprint_is_on_the_page_newest_open(db_path):
    """A week is a sprint. All of them render — folded, but present: showing
    one week at a time hides the comparison the grouping exists for."""
    from core import store
    from core.db import get_conn
    client = _make_client()
    now = store.create_task("this week", repo="test-repo")
    old = store.create_task("long ago", repo="test-repo")
    for t in (now, old):
        store.set_status(t["id"], "done")
    conn = get_conn()
    conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", ("2026-08-05T10:00:00+00:00", old["id"]))
    conn.commit(); conn.close()

    html = client.get("/api/tasks/list").text
    assert "This week" in html and "Week 32" in html
    assert "this week" in html and "long ago" in html          # both weeks present
    assert html.count('<details class="sprint"') == 2          # both weeks rendered
    assert html.count("open>") == 1                            # only the newest unfolded


def test_a_row_carries_what_you_filter_by(db_path):
    """Repo, the roles that worked it, status, who asked — as data the filter
    reads, whether or not the row prints them."""
    from core import store
    from core.usage import record_run
    t = store.create_task("tagged work", repo="test-repo")
    store.set_status(t["id"], "done")
    run = {"input_tokens": 1, "output_tokens": 1, "reasoning_tokens": 0, "cache_read": 0,
           "cache_write": 0, "total_tokens": 2, "cost": 0.01, "steps": 1}
    record_run(t["id"], "engineer", "m", run, 1000)
    record_run(t["id"], "reviewer", "m", run, 1000)
    html = _make_client().get("/api/tasks/list").text
    row = html.split(t["id"], 1)[0].rsplit("<li", 1)[1] + html.split(t["id"], 1)[1].split("</li>", 1)[0]
    assert 'data-repo="test-repo"' in row
    assert 'data-who="engineer reviewer"' in row          # every role, in order
    assert 'data-kind="done"' in row
    assert 'title="test-repo"' in row                     # the repo lives on the key
    # and the filters offer those values
    assert '<option value="test-repo">' in html
    assert '<option value="engineer">' in html and '<option value="reviewer">' in html


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
    assert "Outcome" in response.text                    # the output, labelled as one
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


def test_task_box_hides_agent_choice_under_more(db_path):
    html = _make_client().get("/tasks").text
    box = html.split('class="taskbox"')[1].split("</form>")[0]
    assert "engineer" in box.split("data-agents='", 1)[1].split("'", 1)[0]   # offered to @
    assert "the orchestrator picks" in box                                  # and the default says so


def test_task_detail_shows_handling_agent(db_path):
    from core import store
    solo = store.create_task("solo task", repo="sisyphus", agent="engineer")
    auto = store.create_task("auto task", repo="sisyphus")
    c = _make_client()
    assert "engineer (solo)" in c.get(f"/api/tasks/{solo['id']}/detail").text
    assert "orchestrator" in c.get(f"/api/tasks/{auto['id']}/detail").text


# ── Task box & project pages ─────────────────────────────────────────────

_REPOS = [{"name": "alpha", "source": "/src/alpha", "branch": "main"},
          {"name": "beta", "source": "/src/beta", "branch": "main"}]


def test_home_and_tasks_start_work_from_the_same_box(db_path):
    """One way to start a task: no classify step, no modal, no templates."""
    client = _make_client()
    for page in ("/dashboard", "/tasks"):
        html = client.get(page).text
        assert 'class="taskbox" hx-post="/api/tasks"' in html
        assert "/api/command/" not in html
        assert "/api/tasks/new-form" not in html


def test_retired_creation_routes_are_gone(db_path):
    client = _make_client()
    assert client.get("/api/tasks/new-form").status_code == 404
    for path in ("/api/command/classify", "/api/command/create-task",
                 "/api/command/create-plan", "/api/command/create-automation",
                 "/api/tasks/ai-parse"):
        assert client.post(path, data={"text": "x", "goal": "x"}).status_code in (404, 405)


def test_sidebar_lists_each_repo_as_a_project(db_path):
    with patch("core.repos.load_repos", return_value=_REPOS):
        html = _make_client().get("/tasks").text
    assert 'href="/p/alpha"' in html and 'href="/p/beta"' in html
    assert 'href="/plans"' not in html


def test_project_page_fixes_the_repo_and_narrows_the_board(db_path):
    with patch("core.repos.load_repos", return_value=_REPOS):
        html = _make_client().get("/p/alpha").text
    box = html.split('class="taskbox"')[1].split("</form>")[0]
    assert '<input type="hidden" name="repo" value="alpha">' in box
    assert 'class="taskbox__repo"' not in box
    assert 'hx-get="/api/tasks/list?repo=alpha"' in html
    assert 'href="/repos/alpha"' in html
    assert 'class="active" title="alpha' in html


def test_unknown_project_is_a_404(db_path):
    with patch("core.repos.load_repos", return_value=_REPOS):
        r = _make_client().get("/p/nope", headers={"accept": "text/html"})
    assert r.status_code == 404


def test_board_filters_by_repo(db_path):
    from core import store
    store.create_task("alpha work", repo="alpha", title="Alpha card")
    store.create_task("beta work", repo="beta", title="Beta card")
    client = _make_client()
    narrowed = client.get("/api/tasks/list?repo=alpha").text
    assert "Alpha card" in narrowed and "Beta card" not in narrowed
    everything = client.get("/api/tasks/list").text
    assert "Alpha card" in everything and "Beta card" in everything


def test_task_box_creates_the_task_on_its_repo(db_path):
    from core import store
    with patch("core.orchestrator.emit") as mock_emit:
        r = _make_client().post("/api/tasks", data={"goal": "tidy the README", "repo": "alpha"})
    assert r.status_code == 200 and "taskCreated" in r.headers.get("HX-Trigger", "")
    assert "created" in r.text and "#alpha" in r.text                       # the receipt
    [t] = [t for t in store.list_tasks() if t["goal"] == "tidy the README"]
    assert t["repo"] == "alpha" and not t.get("agent")
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
        assert "No agent is running" in r.text

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
        with patch("core.repos.load_repos", return_value=[]):
            html = _make_client().get("/dashboard").text
        nav = html.split('<nav class="nav">')[1].split("</nav>")[0]
        assert nav.count("<a ") == 9
        for label in ("Home", "Tasks", "Agents", "Runners", "Repos",
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
        import re
        from core import store
        t = store.create_task("g")
        store.set_status(t["id"], "active")
        store.post_message(t["id"], "system", kind="worker_started", text="spawning engineer: x")
        html = _make_client().get(f"/api/tasks/{t['id']}/detail").text
        # markup only: the page's own clock-formatting script says "m ago" too
        markup = re.sub(r"<script.*?</script>", "", html, flags=re.S)
        assert "m ago" not in markup


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
        assert "live-dot" in html
        assert "engineer" in html

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
        assert "No agent is running." in html
        assert "Product owner" not in html and "What next" not in html
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

    def test_the_rail_is_unhooked_from_the_board_for_now(self, db_path):
        """Temporarily off the board (2026-09-20) while the page is reworked.
        The route still answers, so putting it back is a template change."""
        html = _make_client().get("/tasks").text
        assert 'id="watch"' not in html
        assert _make_client().get("/api/board/watch").status_code == 200


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


# ── Sidebar status lines (health payload) ───────────────────────────────


def _sys_block(html: str) -> str:
    return html.split('<div class="sys">')[1].split("</aside>")[0]


def test_sidebar_sys_status_lines(db_path):
    """The .sys block says what an operator needs at a glance: daemon kept as
    is, the old "alerts" line renamed to "browser" (still click-to-enable),
    a notify on/off line (configured via SISYPHUS_NOTIFY_URL), a backup-age
    line — and none of the meaningless old lines."""
    block = _sys_block(_make_client().get("/dashboard").text)
    assert "daemon" in block
    assert "browser" in block
    assert 'onclick="enableNotifications()"' in block   # behavior unchanged
    assert "notify" in block
    assert "SISYPHUS_NOTIFY_URL" in block               # title explains config
    assert "backup" in block
    assert "alerts" not in block                        # renamed away
    assert "ui v2.0" not in block and "v2.0" not in block  # dropped


def test_health_reports_notify_on_and_off(db_path):
    """The endpoint's notify field tracks core.notify.enabled() exactly."""
    client = _make_client()
    with patch("core.notify.enabled", return_value=True):
        assert client.get("/api/v1/health").json()["notify"] is True
    with patch("core.notify.enabled", return_value=False):
        assert client.get("/api/v1/health").json()["notify"] is False


def test_health_reports_backup_age_fresh(db_path):
    """A fresh snapshot comes back with its age and a non-stale flag."""
    from datetime import datetime
    client = _make_client()
    fresh = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    backups = [{"name": "sisyphus-20260101-000000.db", "created_at": fresh}]
    with patch("core.backup.list_backups", return_value=backups):
        data = client.get("/api/v1/health").json()
    assert data["backup"]["age_s"] is not None
    assert 0 <= data["backup"]["age_s"] < 60
    assert data["backup"]["stale"] is False


def test_health_reports_no_backup_as_stale(db_path):
    """No snapshots at all reads red on the line."""
    client = _make_client()
    with patch("core.backup.list_backups", return_value=[]):
        data = client.get("/api/v1/health").json()
    assert data["backup"] == {"age_s": None, "stale": True}


def test_backup_status_red_state_unit():
    """The red-state rule, unit level: stale when there are no snapshots or the
    newest is older than 48h, green otherwise."""
    from datetime import datetime, timedelta
    from web.api import _backup_status

    assert _backup_status([]) == {"age_s": None, "stale": True}

    fresh = (datetime.now() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    st = _backup_status([{"name": "a.db", "created_at": fresh}])
    assert st["stale"] is False and st["age_s"] is not None

    old = (datetime.now() - timedelta(hours=49)).strftime("%Y-%m-%d %H:%M:%S")
    st = _backup_status([{"name": "a.db", "created_at": old}])
    assert st["age_s"] > 48 * 3600
    assert st["stale"] is True

    # An unparseable stamp is unknown — treat it like "none", i.e. stale.
    assert _backup_status([{"name": "a.db", "created_at": "garbage"}]) == \
        {"age_s": None, "stale": True}


class TestATaskOpensOverTheBoard:
    """Reading a task used to cost you the board: your scroll position, your
    filters and every sprint you unfolded, rebuilt on the way back."""

    def test_rows_open_the_panel_and_push_the_task_url(self, db_path):
        from core import store
        t = store.create_task("open me", repo="test-repo")
        store.set_status(t["id"], "done")
        html = _make_client().get("/api/tasks/list").text
        row = html.split(t["id"], 1)[1]
        assert f'hx-get="/api/tasks/{t["id"]}/panel"' in html
        assert 'hx-target="#overlay-body"' in row
        assert f'hx-push-url="/tasks/{t["id"]}"' in row
        # the plain href survives for a middle click, a new tab and no JS
        assert f'href="/tasks/{t["id"]}"' in html

    def test_the_panel_is_the_same_shell_the_page_uses(self, db_path):
        from core import store
        t = store.create_task("panel me", repo="test-repo")
        store.set_status(t["id"], "active")
        panel = _make_client().get(f"/api/tasks/{t['id']}/panel").text
        assert 'id="task-detail"' in panel                 # it refreshes itself
        assert f'hx-get="/api/tasks/{t["id"]}/detail"' in panel
        assert "dataChanged from:body" in panel            # driven by the pulse
        assert "every 60s" in panel                        # safety net on a live task
        assert "panel me" in panel and "Request" in panel

    def test_a_finished_task_panel_keeps_no_timer_at_all(self, db_path):
        from core import store
        t = store.create_task("quiet", repo="test-repo")
        store.set_status(t["id"], "done")
        panel = _make_client().get(f"/api/tasks/{t['id']}/panel").text
        assert "every " not in panel                        # nothing ticks on a finished task
        assert "dataChanged from:body" in panel             # it still hears the daemon

    def test_an_unknown_task_panel_is_a_404(self, db_path):
        assert _make_client().get("/api/tasks/task-nope/panel").status_code == 404

    def test_the_deep_link_still_renders_the_whole_page(self, db_path):
        from core import store
        t = store.create_task("linked", repo="test-repo")
        html = _make_client().get(f"/tasks/{t['id']}").text
        assert "<nav class=\"nav\">" in html              # full chrome, not a fragment
        assert 'id="task-detail"' in html and "linked" in html

    def test_the_board_carries_the_overlay_and_its_escape(self, db_path):
        html = _make_client().get("/tasks").text
        assert 'id="task-overlay"' in html and 'id="overlay-body"' in html
        assert "closeTaskPanel" in html


class TestASprintIsAWeekWhereTheReaderLives:
    """Stamps are stored in UTC. A sprint is a week in the week the reader
    lives in — east of UTC, a task started just after midnight on Monday was
    filed under "last week", because in UTC it was still Sunday night."""

    def test_a_utc_stamp_is_read_in_local_time(self):
        import os
        import time
        from web.routes import _sprint_of

        os.environ["TZ"] = "Europe/Kyiv"      # UTC+3 in September
        time.tzset()
        try:
            # Monday 21 Sep 2026, 00:12 local == Sunday 20 Sep, 21:12 UTC
            assert _sprint_of("2026-09-20T21:12:00+00:00")["id"] == "2026-W39"
            # and an hour earlier is genuinely still the week before
            assert _sprint_of("2026-09-20T20:12:00+00:00")["id"] == "2026-W38"
        finally:
            del os.environ["TZ"]
            time.tzset()

    def test_a_naive_stamp_is_taken_as_given(self):
        from web.routes import _sprint_of
        assert _sprint_of("2026-09-21T00:12:00")["id"] == "2026-W39"

    def test_nonsense_falls_back_to_this_week_rather_than_crashing(self):
        from datetime import date
        from web.routes import _sprint_of
        year, week, _ = date.today().isocalendar()
        assert _sprint_of("not a date")["id"] == f"{year}-W{week:02d}"


class TestATaskTitleCanBeSelected:
    """Rows, cards and asks are links wrapping their own text. A link claims
    the drag, so a title could be opened but never selected or copied — and
    the mouseup at the end of the attempt read as "open me"."""

    def test_task_links_release_the_drag(self, db_path):
        from core import store
        t = store.create_task("copy my title", repo="test-repo")
        store.set_status(t["id"], "done")
        html = _make_client().get("/api/tasks/list").text
        row = html.split(t["id"], 1)[1]
        assert 'draggable="false"' in row

    def test_the_page_knows_a_drag_from_a_click(self, db_path):
        html = _make_client().get("/tasks").text
        assert "dragIsNotAClick" in html

    def test_a_sprint_heading_is_selectable_text(self, db_path):
        css = (Path(__file__).parent.parent / "web" / "static" / "app.css").read_text()
        head = css.split(".sprint__head {", 1)[1].split("}", 1)[0]
        assert "user-select: none" not in head


class TestAutomationRunsAreTasksAmongTasks:
    """An automation does not produce a different kind of work — fire() makes
    an ordinary task, with the same lifecycle as yours. What differs is who
    asked for it, so that is what the row says; and one filter hides them."""

    def _fire(self, days=("2026-09-15",), **ap_kw):
        from core import store
        from core.autopilot import create_autopilot, fire
        from core.db import get_conn
        ap = create_autopilot(name="Nightly repo scan", trigger_type="schedule",
                              cron="0 3 * * *", goal_template="scan", **ap_kw)
        made = []
        with patch("core.orchestrator.emit"):
            for day in days:
                tid = fire(ap)
                store.set_status(tid, "done")
                made.append((day, tid))
        conn = get_conn()
        for day, tid in made:
            conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (f"{day}T12:00:00+00:00", tid))
        conn.commit(); conn.close()
        return ap, [tid for _, tid in made]

    def test_fire_records_who_asked(self, db_path):
        from core import store
        ap, (tid,) = self._fire()
        assert store.get_task(tid)["origin"] == f"autopilot:{ap['id']}"

    def test_each_run_is_its_own_row_marked_with_its_automation(self, db_path):
        ap, tids = self._fire(days=("2026-09-15", "2026-09-16", "2026-09-17"))
        html = _make_client().get("/api/tasks/list").text
        for tid in tids:
            row = html.split(tid, 1)[1].split("</li>", 1)[0]
            assert "↻ daily 03:00" in row            # the tag says when it fires
            assert "Nightly repo scan" in row          # the title says which automation
            assert 'data-origin="auto"' in html.split(tid, 1)[0].rsplit("<li", 1)[1]
        assert "autogroup" not in html                  # no separate object any more

    def test_the_tooltip_says_when_it_fires(self, db_path):
        self._fire()
        html = _make_client().get("/api/tasks/list").text
        assert "daily 03:00" in html and "0 3 * * *" not in html

    def test_a_paused_automation_is_said_and_quieted(self, db_path):
        self._fire(enabled=0)
        html = _make_client().get("/api/tasks/list").text
        assert "↻ paused" in html and "tag--idle" in html

    def test_a_cron_that_can_never_fire_is_said(self, db_path):
        from core.autopilot import update_autopilot
        ap, _ = self._fire()
        update_autopilot(ap["id"], cron="not a cron")
        html = _make_client().get("/api/tasks/list").text
        assert "↻ broken schedule" in html and "can never fire" in html

    def test_a_persons_task_leaves_asked_by_empty(self, db_path):
        """Only a machine asking is news; the repo is on the key already."""
        from core import store
        t = store.create_task("real work", repo="test-repo")
        store.set_status(t["id"], "done")
        html = _make_client().get("/api/tasks/list").text
        assert 'data-origin="you"' in html
        row = html.split(t["id"], 1)[1].split("</li>", 1)[0]
        assert "tag--origin" not in row
        assert '<span class="trow__asked"></span>' in row

    def test_the_filter_offers_who_asked(self, db_path):
        self._fire()
        html = _make_client().get("/api/tasks/list").text
        assert 'data-f-field="origin"' in html                 # the filter names who asked
        assert '<option value="auto">an automation</option>' in html

    def test_each_week_keeps_its_own_runs_and_counts_them(self, db_path):
        self._fire(days=("2026-09-15", "2026-09-16", "2026-09-08"))
        html = _make_client().get("/api/tasks/list").text
        assert html.count("↻ daily 03:00") == 3         # every run, in its own week
        assert ">2</span> tasks" in html and ">1</span> tasks" in html


class TestTheRowSaysWhyOrNothing:
    """The why column is empty when a task went the ordinary way, and says the
    one clause worth reading when it did not."""

    def _row_html(self, html, task_id):
        return html.split(task_id, 1)[0].rsplit("<li", 1)[1] + html.split(task_id, 1)[1].split("</li>", 1)[0]

    def test_an_ordinary_task_says_nothing(self, db_path):
        from core import store
        t = store.create_task("plain work", repo="test-repo")
        store.set_status(t["id"], "done")
        row = self._row_html(_make_client().get("/api/tasks/list").text, t["id"])
        assert '<span class="trow__why"></span>' in row
        assert "trow__state--done" in row                     # done is a quiet word, not a pill

    def test_a_failure_says_why(self, db_path):
        from core import store
        t = store.create_task("scan", repo="test-repo")
        store.post_message(t["id"], "orchestrator", kind="note",
                           text="[fail] reviewer ended without a result (2 attempts)")
        store.set_status(t["id"], "failed")
        row = self._row_html(_make_client().get("/api/tasks/list").text, t["id"])
        assert "no result from the reviewer ×2" in row and "why--bad" in row
        assert "ended without a result (2 attempts)" in row   # the whole note, in the tooltip

    def test_a_closed_mr_is_not_merged_not_done(self, db_path):
        from core import store
        t = store.create_task("proposed then dropped", repo="test-repo")
        store.set_status(t["id"], "done")
        mr = mergerequests.create_mr(t["id"], "test-repo", "feature/x", "main")
        mergerequests.set_review_status(mr["id"], "approved")
        mergerequests.set_status(mr["id"], "closed")
        row = self._row_html(_make_client().get("/api/tasks/list").text, t["id"])
        assert "trow__state--not-merged" in row
        assert "approved, closed unmerged" in row

    def test_a_merge_over_a_verdict_is_said_quietly(self, db_path):
        from core import store
        t = store.create_task("landed anyway", repo="test-repo")
        store.set_status(t["id"], "done")
        mr = mergerequests.create_mr(t["id"], "test-repo", "feature/x", "main")
        mergerequests.set_review_status(mr["id"], "changes_requested")
        mergerequests.mark_merged(mr["id"], "abc1234")
        row = self._row_html(_make_client().get("/api/tasks/list").text, t["id"])
        assert "changes requested · merged anyway" in row and "why--muted" in row


class TestAReviewHangsUnderWhatItReviewed:
    def test_the_review_is_a_child_of_the_task_whose_mr_it_read(self, db_path):
        from core import reviews, store
        work = store.create_task("the work", repo="test-repo")
        store.set_status(work["id"], "done")
        mr = mergerequests.create_mr(work["id"], "test-repo", "feature/x", "main")
        # the same header ensure_review_task writes: Review-of-MR / Origin-task
        goal = f"Review-of-MR: {mr['id']}\nOrigin-task: {work['id']}\n\nreview it"
        assert reviews.parse_review_marker(goal)["origin_task"] == work["id"]
        rev = store.create_task(goal, repo="test-repo", title=f"Review {mr['id']}: the work")
        store.post_message(rev["id"], "reviewer", kind="result", text="APPROVED: looks right")
        store.set_status(rev["id"], "done")

        html = _make_client().get("/api/tasks/list").text
        assert html.index(work["key"]) < html.index(rev["key"])
        child = html.split(rev["id"], 1)[0].rsplit("<li", 1)[1]
        assert "trow--child" in child
        assert f"Review of {mr['id']}" in html
        assert "verdict: approved" in html


def test_the_backlog_names_its_columns_once_on_the_rows_grid(db_path):
    """One header over every week, cut from the same grid variable as the
    rows, so a column's name sits over the column."""
    from core import store
    t = store.create_task("anything", repo="test-repo")
    store.set_status(t["id"], "done")
    html = _make_client().get("/api/tasks/list").text
    assert html.count('class="thead"') == 1
    head = html.split('class="thead"', 1)[1].split("</div>", 1)[0]
    for name in ("Key", "Title", "Asked by", "Worked", "Why", "Status", "When"):
        assert f">{name}<" in head, name
    css = (Path(__file__).parent.parent / "web" / "static" / "app.css").read_text()
    assert css.count("grid-template-columns: var(--c-grid)") >= 2   # header and rows alike


class TestReplyingToAFinishedTaskReopensIt:
    """The orchestrator ignores every event on a done or failed task, so a
    reply typed into one posted and then vanished — nothing ran, nothing said
    so."""

    def test_the_reply_reopens_and_is_heard(self, db_path):
        from core import store
        t = store.create_task("finished", repo="test-repo")
        store.set_status(t["id"], "done")
        with patch("core.orchestrator.emit") as emit:
            r = _make_client().post(f"/api/tasks/{t['id']}/messages", data={"text": "one more thing"})
        assert r.status_code == 200
        assert store.get_task(t["id"])["status"] == "waiting"     # the state a reply resumes
        emit.assert_called_once_with(t["id"], "human_replied")
        assert store.list_events(t["id"])[-1]["cause"] == "reopened by your reply"
        kinds = [(m["author"], m["kind"]) for m in store.get_thread(t["id"])]
        assert kinds[-2:] == [("system", "note"), ("human", "answer")]

    def test_a_reply_to_a_live_task_changes_nothing_about_its_status(self, db_path):
        from core import store
        t = store.create_task("live", repo="test-repo")
        with patch("core.orchestrator.emit"):
            _make_client().post(f"/api/tasks/{t['id']}/messages", data={"text": "note"})
        assert store.get_task(t["id"])["status"] == "active"
        assert store.list_events(t["id"]) == []


# ── Sprint board: this week as lanes ─────────────────────────────────────


def _srow(id, kind, when, **kw):
    from web.routes import _sprint_of
    return {"id": id, "kind": kind, "when": when, "sprint": _sprint_of(when), "live": None,
            "children": [], **kw}


def test_sprint_board_holds_open_work_from_any_week_and_only_this_weeks_closed():
    from datetime import date
    from web.routes import sprint_board
    today = date(2026, 9, 23)                       # a Wednesday
    top = [
        _srow("a", "in-progress", "2026-09-22T10:00:00"),
        _srow("b", "in-progress", "2026-09-22T11:00:00"),
        _srow("c", "needs-you", "2026-09-10T10:00:00"),   # carried over
        _srow("d", "to-merge", "2026-09-21T10:00:00"),
        _srow("e", "shipped", "2026-09-22T09:00:00"),
        _srow("f", "done", "2026-09-12T09:00:00"),        # closed last week: history
        _srow("g", "failed", "2026-09-23T08:00:00"),
    ]
    b = sprint_board(top, running_ids={"a"}, today=today)
    lanes = {l["key"]: [c["id"] for c in l["cards"]] for l in b["lanes"]}
    assert lanes == {"queued": ["b"], "running": ["a"], "you": ["c", "d"], "done": ["e"], "failed": ["g"]}
    carried = {c["id"]: c["carried"] for l in b["lanes"] for c in l["cards"]}
    assert carried["c"] == "Week 37" and carried["a"] == ""
    assert (b["total"], b["closed"], b["pct"], b["days_left"]) == (6, 2, 33, 4)


def test_the_board_draws_the_sprint_view_when_asked(db_path):
    from core.runners import init_runners_table
    init_runners_table()
    body = _make_client().get("/api/tasks/list?view=sprint").text
    assert 'class="sboard"' in body and "Needs you" in body
    assert "days left" in body or "last day" in body


# ── Wants you: answering from the card, creating with files ──────────────


def test_an_answer_from_a_card_comes_back_as_a_receipt(db_path):
    from core import store
    t = store.create_task("pick a db", "")
    store.set_status(t["id"], "waiting")
    with patch("core.orchestrator.emit"):
        r = _make_client().post(f"/api/tasks/{t['id']}/messages", data={"text": "2. Postgres", "from_card": "1"})
    assert r.status_code == 200 and "askc-done" in r.text and "2. Postgres" in r.text
    assert "HX-Trigger" not in r.headers                    # the board catches up on its own pulse
    assert store.get_thread(t["id"])[-1]["text"] == "2. Postgres"


def test_a_task_created_with_a_file_carries_it_from_the_first_pass(db_path, tmp_path, monkeypatch):
    from core import store
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    with patch("web.routes.emit", create=True), patch("core.orchestrator.emit"):
        r = _make_client().post("/api/tasks", data={"goal": "fix the crash"},
                                files=[("files", ("../shot one.png", b"\x89PNG data", "image/png"))])
    assert r.status_code == 200 and "1 file" in r.text
    t = store.list_tasks()[0]
    note = store.get_thread(t["id"])[0]
    assert note["attachment"] == "shot-one.png" and note["author"] == "human"
    assert (tmp_path / "tasks" / t["id"] / "attachments" / "shot-one.png").read_bytes() == b"\x89PNG data"


def test_a_notification_says_which_kind_of_wait_it_is(db_path):
    from core import store
    t = store.create_task("scan", "")
    store.post_message(t["id"], "orchestrator", kind="question",
                       text="I couldn't produce a next-step decision (the orchestrator agent returned nothing, 3 attempts).")
    store.set_status(t["id"], "waiting")
    items = _make_client().get("/api/notifications/poll").json()["items"]
    assert [i["verb"] for i in items if i["task_id"] == t["id"]] == ["is stuck"]


def test_both_views_offer_the_same_filter(db_path):
    from core import store
    store.create_task("filterable", "test-repo")
    for url in ("/api/tasks/list", "/api/tasks/list?view=sprint"):
        html = _make_client().get(url).text
        assert 'data-f-field="repo"' in html, url
        assert 'data-f-field="origin"' in html, url
        assert "data-filter-item" in html and "data-filter-group" in html, url


def test_the_sprint_board_lets_you_answer_where_the_list_does(db_path):
    """A question was answerable on the list and merely readable on the sprint
    board; both draw the same card now."""
    from core import store
    t = store.create_task("Give me review", "test-repo")
    store.post_message(t["id"], "orchestrator", kind="question",
                       text="What should I review? Please pick one:\n1. The latest commits\n2. The open MRs")
    store.set_status(t["id"], "waiting")
    html = _make_client().get("/api/tasks/list?view=sprint").text
    assert "askc--question" in html and "The latest commits" in html
    assert f'hx-post="/api/tasks/{t["id"]}/messages"' in html
