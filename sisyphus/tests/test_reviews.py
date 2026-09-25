"""Tests for core/reviews.py — the auto-review hook module."""

from core import mergerequests, reviews, store


def _mr(task_id="task-x", **kw):
    kw.setdefault("repo", "test-repo")
    kw.setdefault("source_branch", "feature/x")
    kw.setdefault("target_branch", "main")
    kw.setdefault("title", "Test MR")
    return mergerequests.create_mr(task_id=task_id, **kw)


class TestEnsureReviewTask:
    def test_creates_pinned_mr_reviewer_task(self, db_path):
        mr = _mr()
        task = reviews.ensure_review_task(mr)
        assert task is not None
        assert task["agent"] == "mr-reviewer"
        assert task["repo"] == "test-repo"
        assert f"Review-of-MR: {mr['id']}" in task["goal"]
        assert f"Origin-task: {mr['task_id']}" in task["goal"]

    def test_the_instruction_points_at_where_the_verdict_actually_is(self, db_path):
        """It used to say "its full output is on the thread" — the ORIGIN
        task's thread, which a review-of-MR task cannot read. The verdict now
        travels with the diff, so the instruction has to name that."""
        goal = reviews.ensure_review_task(_mr())["goal"]
        assert "get_mr_diff returns that verdict" in goal
        assert "full output is on the thread" not in goal
        # and it must still say what to do when the two disagree
        assert "the record wins" in goal.replace("\n", " ")

    def test_the_goal_tells_the_reviewer_to_defer_to_the_recorded_check(self, db_path):
        """The reviewer must rely on the daemon's recorded verdict instead of
        re-running the whole suite in a sandbox, and must not request changes
        merely because no verdict is recorded yet."""
        goal = reviews.ensure_review_task(_mr())["goal"]
        flat = goal.replace("\n", " ")
        assert "do NOT re-run the full suite" in flat
        assert "run only the tests relevant to the diff" in flat
        assert "waiting for the recorded check" in flat
        assert "not a reason for CHANGES REQUESTED" in flat

    def test_dedup_second_call_returns_none(self, db_path):
        mr = _mr()
        t1 = reviews.ensure_review_task(mr)
        assert t1 is not None
        t2 = reviews.ensure_review_task(mr)
        assert t2 is None

    def test_allows_new_after_old_is_terminal(self, db_path):
        mr = _mr()
        t1 = reviews.ensure_review_task(mr)
        assert t1 is not None
        store.set_status(t1["id"], "done")
        t2 = reviews.ensure_review_task(mr)
        assert t2 is not None
        assert t2["id"] != t1["id"]

    def test_allows_new_after_old_failed(self, db_path):
        mr = _mr()
        t1 = reviews.ensure_review_task(mr)
        assert t1 is not None
        store.set_status(t1["id"], "failed")
        t2 = reviews.ensure_review_task(mr)
        assert t2 is not None
        assert t2["id"] != t1["id"]


class TestParseReviewMarker:
    def test_roundtrip(self, db_path):
        goal = (
            "Review-of-MR: mr-abc123\n"
            "Origin-task: task-xyz789\n"
            "\n"
            "Review this MR. Use get_mr_diff, then approve or request_changes.\n"
        )
        marker = reviews.parse_review_marker(goal)
        assert marker is not None
        assert marker["mr_id"] == "mr-abc123"
        assert marker["origin_task"] == "task-xyz789"

    def test_returns_none_on_ordinary_goal(self, db_path):
        goal = "Fix the login bug in auth module"
        assert reviews.parse_review_marker(goal) is None

    def test_returns_none_on_empty(self, db_path):
        assert reviews.parse_review_marker("") is None

    def test_returns_none_on_missing_origin(self, db_path):
        goal = "Review-of-MR: mr-abc123\n\nJust review this MR."
        marker = reviews.parse_review_marker(goal)
        assert marker is not None
        assert marker["mr_id"] == "mr-abc123"
        assert marker["origin_task"] == ""


class TestReviewDeath:
    """A review-of-MR task that dies (all runs empty) must record the failure
    on the MR row and tell the origin task — not leave the MR 'pending'
    forever (regression: mr-f7913881, where the MR was then unmergeable with
    nobody told)."""

    def test_records_death_on_mr_and_tells_origin(self, db_path):
        origin = store.create_task("do some work", agent="engineer")
        mr = _mr(task_id=origin["id"])
        mergerequests.set_review_status(mr["id"], "pending")
        reviews.record_review_death(
            f"Review-of-MR: {mr['id']}\nOrigin-task: {mr['task_id']}\n\nReview this MR.",
            reason="mr-reviewer ended without a result (2 attempts)",
        )
        got = mergerequests.get_mr(mr["id"])
        assert got["review_status"] == "died"
        assert mergerequests.merge_block_reason(got) is not None
        msgs = store.get_thread(mr["task_id"])
        assert any("review" in m["text"] and "died" in m["text"] for m in msgs)

    def test_noop_for_non_review_goal(self, db_path):
        origin = store.create_task("some work", agent="engineer")
        reviews.record_review_death("fix the login bug", reason="x")
        assert store.get_thread(origin["id"]) == []


class TestMergeRequestsMigration:
    def test_review_status_migration_is_idempotent(self, db_path):
        mergerequests.init_mr_table()
        mergerequests.init_mr_table()

        from core.db import get_conn
        conn = get_conn()
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(merge_requests)").fetchall()}
        conn.close()
        assert "review_status" in cols

    def test_set_review_status_works(self, db_path):
        mr = _mr()
        mergerequests.set_review_status(mr["id"], "approved")
        got = mergerequests.get_mr(mr["id"])
        assert got["review_status"] == "approved"

    def test_set_review_status_null_by_default(self, db_path):
        mr = _mr()
        got = mergerequests.get_mr(mr["id"])
        assert got["review_status"] is None
