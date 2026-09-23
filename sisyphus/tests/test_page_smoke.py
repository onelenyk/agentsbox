"""Smoke tests for the rendered UI — does the number reach the page?

Four bugs in two days shared one shape: a value was computed correctly and
never arrived where someone looks at it. open_mr built a task and never queued
it; mr-reviewer produced a verdict that never reached the MR; the dashboard
carried correct bar percentages that rendered at zero size; Repos asked about a
bare clone the design stopped keeping. Every unit test passed throughout,
because they check what a function returns, not what a reader sees.

So these assert two things per surface: the page renders at all (a template or
context error takes down a whole screen), and its indicators actually track the
data — rendered with different data, they must differ. A hardcoded or
never-wired indicator passes the first check and fails the second.

Limit worth stating: this reaches the HTML only. The bar-fill and spark bugs
lived in CSS — the markup was correct and these tests would have passed. Those
need a browser.
"""
import re

from fastapi.testclient import TestClient

from core import mergerequests, store, usage
from web.app import app


def _client():
    return TestClient(app)


def _text(resp):
    """Rendered HTML with tags stripped, so assertions read like a human does."""
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text[:400]}"
    return re.sub(r"<[^>]+>", " ", resp.text)


# Every screen, including the partials htmx swaps in — those carry the numbers.
PAGES = [
    "/dashboard", "/tasks", "/plans", "/repos", "/runners", "/agents",
    "/autopilots", "/stats", "/runtime", "/backups",
    "/api/dashboard/view", "/api/tasks/list", "/api/repos/list",
    "/api/stats/view", "/api/inbox/view",
]


class TestEveryPageRenders:
    def test_all_pages_render_with_an_empty_database(self, db_path):
        """A fresh install must not 500 anywhere — empty is a real state."""
        c = _client()
        for path in PAGES:
            r = c.get(path)
            assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:300]}"

    def test_all_pages_render_with_realistic_data(self, db_path):
        done = store.create_task("shipped thing", repo="sisyphus")
        store.set_status(done["id"], "done")
        store.create_task("running thing", repo="sisyphus")
        waiting = store.create_task("blocked thing", repo="sisyphus")
        store.post_message(waiting["id"], "orchestrator", kind="question", text="ok?")
        store.set_status(waiting["id"], "waiting")
        mergerequests.create_mr(task_id=done["id"], repo="sisyphus",
                                source_branch="sisyphus/task-1", target_branch="main",
                                title="A change")

        c = _client()
        for path in PAGES:
            r = c.get(path)
            assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:300]}"


class TestIndicatorsTrackTheData:
    """The check a hardcoded indicator cannot pass: change the data, and what
    the page says has to change with it."""

    def test_board_counts_follow_the_tasks(self, db_path):
        c = _client()
        before = _text(c.get("/api/tasks/list"))

        for i in range(3):
            t = store.create_task(f"finished work {i}", repo="sisyphus")
            store.set_status(t["id"], "done")
        after = _text(c.get("/api/tasks/list"))

        assert before != after
        assert "finished work 0" in after and "finished work 2" in after

    def test_a_task_lands_in_the_column_its_status_implies(self, db_path):
        waiting = store.create_task("needs a human decision", repo="sisyphus")
        store.post_message(waiting["id"], "orchestrator", kind="question",
                           text="which branch?")
        store.set_status(waiting["id"], "waiting")

        html = _client().get("/api/tasks/list").text
        # it must sit in the zone that asks for a human, not merely exist
        wants_you = html.split("Wants you", 1)[1]
        assert "needs a human decision" in wants_you.split("In flight", 1)[0]
        assert "which branch?" in wants_you          # the question itself, on the card

    def test_dashboard_reports_the_real_done_and_failed_counts(self, db_path):
        for i in range(4):
            t = store.create_task(f"ok {i}")
            store.set_status(t["id"], "done")
        for i in range(2):
            t = store.create_task(f"broke {i}")
            store.set_status(t["id"], "failed")

        body = _text(_client().get("/api/dashboard/view"))
        assert "4" in body and "2 failed" in body
        # the old label claimed completion while work was still active
        assert "% completion" not in body

    def test_cost_shown_is_the_cost_recorded(self, db_path):
        t = store.create_task("expensive", repo="sisyphus")
        usage.record_run(task_id=t["id"], agent="engineer", model="m",
                         usage={"input_tokens": 900, "output_tokens": 100,
                                "reasoning_tokens": 0, "cache_read": 0,
                                "cache_write": 0, "total_tokens": 1000,
                                "cost": 1.2345, "steps": 1})

        body = _text(_client().get("/api/dashboard/view"))
        assert "1.23" in body, "recorded cost never reached the dashboard"

    def test_repos_distinguish_reachable_from_missing(self, db_path, tmp_path):
        """The exact bug shape: a status that could only ever say one thing."""
        good = tmp_path / "real"
        (good / ".git").mkdir(parents=True)
        repos = [
            {"name": "live", "source": str(good), "branch": "main"},
            {"name": "gone", "source": str(tmp_path / "vanished"), "branch": "main"},
        ]
        from unittest.mock import patch
        with patch("core.repos.load_repos", return_value=repos):
            body = _text(_client().get("/api/repos/list"))

        assert "ready" in body and "unreachable" in body, \
            "both repos report the same state — the badge is not reading the data"

    def test_daemon_page_names_the_running_commit(self, db_path):
        """The ghost-daemon question. An empty value here is the failure mode
        that made a stale daemon invisible for two days."""
        body = _text(_client().get("/runtime"))
        assert "Running code" in body
        assert "unknown" not in body.split("Running code", 1)[1][:120]

    def test_daemon_page_shows_the_timeouts_that_are_actually_enforced(self, db_path):
        """The page advertised `default_timeout: 300` from a config key no code
        has ever read, while workers actually get 1800s. Sizing a long job off
        that page was wrong by 6x — it misled a reader the day this was found.
        Tie the displayed number to the constant that enforces it.
        """
        from core.worker import WORKER_TIMEOUT_SECONDS, WORKER_IDLE_TIMEOUT_SECONDS

        body = _text(_client().get("/runtime"))
        assert f"{WORKER_TIMEOUT_SECONDS // 60} min in all" in body
        assert f"{WORKER_IDLE_TIMEOUT_SECONDS // 60} min silent" in body
        assert "default_timeout" not in body, "dead config key is back on the page"

    def test_review_task_links_to_the_mr_it_reviews(self, db_path):
        origin = store.create_task("build the feature", repo="sisyphus")
        mr = mergerequests.create_mr(task_id=origin["id"], repo="sisyphus",
                                     source_branch="sisyphus/task-1",
                                     target_branch="main", title="The change")
        review = store.create_task(
            f"Review-of-MR: {mr['id']}\nOrigin-task: {origin['id']}\n\nReview it.",
            repo="sisyphus", agent="mr-reviewer")

        html = _client().get(f"/tasks/{review['id']}").text
        # NOT `mr["id"] in html` — the goal text quotes both ids, so that would
        # pass with no link on the page at all. Assert the actual affordance.
        assert f"/api/mr/{mr['id']}/view" in html, \
            "no way to reach the MR from the task reviewing it"
        assert f'href="/tasks/{origin["id"]}"' in html, \
            "no way back to the task that opened the MR"
