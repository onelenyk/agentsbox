"""The task lifecycle as a whole: stop, resume, reopen, delete — and the
transitions between them.

Every piece here was found missing or broken by using the system rather than
reading it. Stopping work meant `pgrep opencode` and `kill`, which left a
worker_started with no worker_finished and made the task unrunnable until a
daemon restart. Meanwhile launcher._monitored_communicate had implemented
cancellation correctly all along — the cancel_event was threaded through every
layer and no caller ever filled it in.
"""
import asyncio
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from core import cancellation, orchestrator, sessions, store
from web.app import app


def _client():
    return TestClient(app)


class TestCancellationRegistry:
    def test_request_signals_every_worker_on_the_task(self, db_path):
        cancellation.clear()
        a = cancellation.register("task-1", "engineer")
        b = cancellation.register("task-1", "reviewer")
        other = cancellation.register("task-2", "engineer")

        assert cancellation.request("task-1") == 2
        assert a.is_set() and b.is_set()
        assert not other.is_set(), "cancelling one task must not touch another"

    def test_request_on_an_idle_task_reports_zero(self, db_path):
        """Zero is meaningful: the caller must still park the task, or a stop on
        an idle task silently does nothing."""
        cancellation.clear()
        assert cancellation.request("task-nothing-running") == 0

    def test_unregister_clears_the_task_entry(self, db_path):
        cancellation.clear()
        cancellation.register("task-1", "engineer")
        cancellation.unregister("task-1", "engineer")
        assert not cancellation.is_running("task-1")
        assert cancellation.active_tasks() == []


class TestStop:
    def test_stop_parks_the_task_and_signals_the_worker(self, db_path):
        cancellation.clear()
        task = store.create_task("long running work", repo="sisyphus")
        event = cancellation.register(task["id"], "engineer")

        r = _client().post(f"/api/tasks/{task['id']}/stop")

        assert r.status_code == 200
        assert event.is_set(), "the running worker was never told to stop"
        assert store.get_task(task["id"])["status"] == "waiting"
        assert any("stopped by the human" in m["text"]
                   for m in store.get_thread(task["id"]))

    def test_stop_works_when_nothing_is_running(self, db_path):
        """An active task with no live worker must still park, not 404."""
        cancellation.clear()
        task = store.create_task("idle but active")
        r = _client().post(f"/api/tasks/{task['id']}/stop")
        assert r.status_code == 200
        assert store.get_task(task["id"])["status"] == "waiting"

    def test_stopping_a_finished_task_is_refused(self, db_path):
        task = store.create_task("already done")
        store.set_status(task["id"], "done")
        assert _client().post(f"/api/tasks/{task['id']}/stop").status_code == 409

    def test_stop_on_a_missing_task_is_404(self, db_path):
        assert _client().post("/api/tasks/task-nope/stop").status_code == 404

    def test_stopping_a_parent_stops_its_fan_out(self, db_path):
        """Children run their own workers. Parking only the root leaves them
        spending and rolling results up into a task the human already stopped."""
        cancellation.clear()
        parent = store.create_task("fan out across repos")
        kid_a = store.create_task("child a", parent_id=parent["id"])
        kid_b = store.create_task("child b", parent_id=parent["id"])
        ev_a = cancellation.register(kid_a["id"], "engineer")
        ev_b = cancellation.register(kid_b["id"], "engineer")

        assert _client().post(f"/api/tasks/{parent['id']}/stop").status_code == 200

        assert ev_a.is_set() and ev_b.is_set(), "children kept working"
        assert store.get_task(kid_a["id"])["status"] == "waiting"
        assert store.get_task(kid_b["id"])["status"] == "waiting"
        assert any("parent" in m["text"] for m in store.get_thread(kid_a["id"]))

    def test_stopping_a_parent_leaves_finished_children_alone(self, db_path):
        """A child that already succeeded must not be dragged back to waiting."""
        cancellation.clear()
        parent = store.create_task("fan out")
        done_kid = store.create_task("already finished", parent_id=parent["id"])
        store.set_status(done_kid["id"], "done")

        _client().post(f"/api/tasks/{parent['id']}/stop")

        assert store.get_task(done_kid["id"])["status"] == "done"


class TestStopActuallyHolds:
    """The subtle half. A cancelled worker posts worker_finished on its way out,
    which drives a pass — and without a guard the orchestrator immediately
    spawns the next worker, so "stop" would last about a second."""

    def test_a_dying_workers_event_does_not_restart_a_stopped_task(self, db_path):
        task = store.create_task("stopped mid-flight")
        store.set_status(task["id"], "waiting")

        blocked = orchestrator.pass_blocked_by(store.get_task(task["id"]),
                                               event_type="worker_finished")
        assert blocked and "does not resume it" in blocked

    def test_a_human_reply_does_resume_it(self, db_path):
        task = store.create_task("stopped, then resumed")
        store.set_status(task["id"], "waiting")

        assert orchestrator.pass_blocked_by(store.get_task(task["id"]),
                                            event_type="human_replied") is None

    def test_an_external_signal_also_resumes_it(self, db_path):
        """A webhook (CI finished, MR merged) is the other legitimate waker."""
        task = store.create_task("parked pending CI")
        store.set_status(task["id"], "waiting")

        assert orchestrator.pass_blocked_by(store.get_task(task["id"]),
                                            event_type="external") is None


class TestResumeAfterStop:
    def test_reply_resumes_a_stopped_task_end_to_end(self, db_path):
        cancellation.clear()
        task = store.create_task("stop then resume", repo="sisyphus")
        _client().post(f"/api/tasks/{task['id']}/stop")
        assert store.get_task(task["id"])["status"] == "waiting"

        with patch("core.orchestrator.emit") as mock_emit:
            r = _client().post(f"/api/tasks/{task['id']}/messages",
                               data={"text": "carry on"})
        assert r.status_code == 200
        mock_emit.assert_called_once_with(task["id"], "human_replied")
        assert any(m["author"] == "human" for m in store.get_thread(task["id"]))


class TestCancelledIsNotFailed:
    """Live incident: after a stop, the resumed task was immediately failed.
    The orchestrator reads the worker_finished line to decide, and a cancelled
    worker reported with the same wording as a crashed one — "ended without
    reporting a result" — so it concluded "[fail] explorer ended without a
    result" on work that was merely interrupted."""

    class _Result:
        def __init__(self, status, error="", summary=""):
            self.status, self.error, self.summary = status, error, summary

    def test_a_stopped_worker_is_reported_as_stopped_not_failed(self):
        from core.worker import worker_end_message
        text = worker_end_message("explorer", self._Result("cancelled"))
        assert "STOPPED BY A HUMAN" in text
        assert "Do not fail the task" in text
        assert "ended without reporting a result" not in text

    def test_a_crashed_worker_still_reads_as_a_failure(self):
        from core.worker import worker_end_message
        text = worker_end_message("engineer", self._Result("error", error="boom"))
        assert "ended without reporting a result" in text
        assert "boom" in text
        assert "STOPPED BY A HUMAN" not in text

    def test_a_timeout_is_not_mistaken_for_a_stop(self):
        from core.worker import worker_end_message
        text = worker_end_message("engineer",
                                  self._Result("timeout", error="exceeded 1800s"))
        assert "status=timeout" in text and "exceeded 1800s" in text
        assert "STOPPED BY A HUMAN" not in text


class TestPinnedTaskSurvivesAStop:
    """A pinned (single-agent) task is driven by plain code: no result on the
    thread means fail. So stopping one failed it instantly, and replying re-ran
    the same branch and failed it again — it could never be resumed."""

    def test_a_stopped_run_is_recognised_as_stopped(self, db_path):
        from core.worker import worker_end_message, was_stopped_by_a_human

        class R:
            status, error, summary = "cancelled", "", ""

        thread = [{"kind": "worker_started", "text": "go"},
                  {"kind": "worker_finished", "text": worker_end_message("explorer", R())}]
        assert was_stopped_by_a_human(thread)

    def test_a_crashed_run_is_not(self, db_path):
        thread = [{"kind": "worker_started", "text": "go"},
                  {"kind": "worker_finished",
                   "text": "explorer ended without reporting a result (status=error): boom"}]
        from core.worker import was_stopped_by_a_human
        assert not was_stopped_by_a_human(thread)

    def test_a_later_successful_run_hides_an_earlier_stop(self, db_path):
        """Stop, resume, finish properly — the old stop must not keep re-running
        the agent forever."""
        from core.worker import STOPPED_MARKER, was_stopped_by_a_human
        thread = [
            {"kind": "worker_started", "text": "go"},
            {"kind": "worker_finished", "text": f"explorer was {STOPPED_MARKER} ..."},
            {"kind": "worker_started", "text": "go again"},
            {"kind": "result", "text": "all done"},
            {"kind": "worker_finished", "text": "explorer finished"},
        ]
        assert not was_stopped_by_a_human(thread)

    @pytest.mark.asyncio
    async def test_a_stopped_pinned_task_reruns_instead_of_failing(self, db_path):
        from core.worker import STOPPED_MARKER

        task = store.create_task("pinned work", repo="sisyphus", agent="explorer")
        store.post_message(task["id"], "system", kind="worker_started", text="go")
        store.post_message(task["id"], "system", kind="worker_finished",
                           text=f"explorer was {STOPPED_MARKER} before it reported.")
        store.post_message(task["id"], "human", kind="answer", text="carry on, but shorter")

        with patch("core.orchestrator._spawn_worker_safe", return_value=None):
            decision, _ = await orchestrator.handle_event(task["id"], "human_replied")

        assert decision.action == "spawn_worker", \
            f"a stopped pinned task must re-run, got {decision.action}: {decision.why}"
        assert store.get_task(task["id"])["status"] != "failed"


class TestReopen:
    def test_reopen_only_applies_to_terminal_tasks(self, db_path):
        task = store.create_task("still running")
        assert _client().post(f"/api/tasks/{task['id']}/reopen").status_code == 409

    def test_reopen_parks_as_waiting_not_active(self, db_path):
        """Deliberate: reopening states intent, a reply states what to do next."""
        task = store.create_task("was done")
        store.set_status(task["id"], "done")
        assert _client().post(f"/api/tasks/{task['id']}/reopen").status_code == 200
        assert store.get_task(task["id"])["status"] == "waiting"


class TestDelete:
    def test_delete_removes_the_task(self, db_path):
        task = store.create_task("to be deleted")
        assert _client().post(f"/api/tasks/{task['id']}/delete").status_code == 200
        assert store.get_task(task["id"]) is None

    def test_delete_stops_a_running_worker_too(self, db_path):
        """Deleting a task whose worker is mid-run used to leave the subprocess
        running against a task that no longer exists."""
        cancellation.clear()
        task = store.create_task("delete me while busy", repo="sisyphus")
        event = cancellation.register(task["id"], "engineer")

        _client().post(f"/api/tasks/{task['id']}/delete")

        assert event.is_set(), "the worker kept running after its task was deleted"
