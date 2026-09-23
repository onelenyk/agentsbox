"""Paused — a task interrupted with nothing asked of anyone.

Before this, closing Sisyphus and pressing Stop parked a task as `waiting`,
the state of a question put to a human. It landed among the questions with
"Reply to resume", got answered "Huh ?" twice, and each answer drove a pass
that started its own engineer: two workers on one worktree (task-cf2a500f).
"""
import asyncio
from unittest.mock import patch

from fastapi.testclient import TestClient

from core import cancellation, orchestrator, store
from core.worker import was_stopped_by_a_human
from web.app import app


def _client():
    return TestClient(app)


class TestPauseAndResume:
    def test_a_pause_needs_its_kind(self, db_path):
        t = store.create_task("g")
        try:
            store.set_status(t["id"], "paused")
        except ValueError:
            pass
        else:
            raise AssertionError("a pause without a kind was accepted")

    def test_leaving_paused_clears_the_kind(self, db_path):
        t = store.create_task("g")
        store.set_status(t["id"], "paused", pause_kind="stopped")
        store.set_status(t["id"], "active")
        assert store.get_task(t["id"])["pause_kind"] is None

    def test_resume_is_once(self, db_path):
        t = store.create_task("g")
        orchestrator.pause(t["id"], "stopped", "stopped by you")
        with patch("core.orchestrator.emit") as emit:
            assert orchestrator.resume(t["id"], "resumed by you") is True
            assert orchestrator.resume(t["id"], "resumed by you") is False
        emit.assert_called_once_with(t["id"], "continue")
        assert store.get_task(t["id"])["status"] == "active"


class TestTheButton:
    def test_stop_then_resume(self, db_path):
        cancellation.clear()
        t = store.create_task("g", repo="sisyphus")
        _client().post(f"/api/tasks/{t['id']}/stop")
        task = store.get_task(t["id"])
        assert (task["status"], task["pause_kind"]) == ("paused", "stopped")

        with patch("core.orchestrator.emit") as emit:
            assert _client().post(f"/api/tasks/{t['id']}/resume").status_code == 200
            # A second click finds nothing paused: no second pass, no second worker.
            assert _client().post(f"/api/tasks/{t['id']}/resume").status_code == 409
        emit.assert_called_once_with(t["id"], "continue")

    def test_resume_brings_back_the_paused_children_too(self, db_path):
        cancellation.clear()
        parent = store.create_task("fan out")
        kid = store.create_task("kid", parent_id=parent["id"])
        _client().post(f"/api/tasks/{parent['id']}/stop")
        with patch("core.orchestrator.emit"):
            _client().post(f"/api/tasks/{parent['id']}/resume")
        assert store.get_task(kid["id"])["status"] == "active"

    def test_the_board_shows_why_and_a_way_back(self, db_path):
        t = store.create_task("paused thing", repo="sisyphus")
        orchestrator.pause(t["id"], "closed", "Sisyphus was closed")
        page = _client().get("/api/tasks/list").text
        assert "paused · Sisyphus was closed · resumes when Sisyphus opens" in page
        assert f"/api/tasks/{t['id']}/resume" in page


class TestWhatWakesIt:
    def test_a_worker_event_does_not_resume_a_paused_task(self, db_path):
        t = store.create_task("g")
        orchestrator.pause(t["id"], "stopped", "stopped by you")
        task = store.get_task(t["id"])
        assert orchestrator.pass_blocked_by(task, event_type="worker_finished")
        assert orchestrator.pass_blocked_by(task, event_type="human_replied") is None
        assert orchestrator.pass_blocked_by(task, spontaneous=True)

    def test_boot_resumes_only_what_closing_paused(self, db_path, monkeypatch):
        monkeypatch.setattr("core.orphans.reap", lambda: None)
        closed = store.create_task("closed")
        stopped = store.create_task("stopped")
        orchestrator.pause(closed["id"], "closed", "Sisyphus was closed")
        orchestrator.pause(stopped["id"], "stopped", "stopped by you")
        with patch("core.orchestrator.emit"):
            orchestrator.reconcile_on_boot()
        assert store.get_task(closed["id"])["status"] == "active"
        assert store.get_task(stopped["id"])["status"] == "paused"

    def test_a_run_the_daemon_stopped_is_an_interruption(self, db_path):
        thread = [{"kind": "worker_started", "text": ""},
                  {"kind": "worker_finished",
                   "text": "engineer was stopped before it reported: Sisyphus was closed. "}]
        assert was_stopped_by_a_human(thread)


class TestOneWorkerPerTask:
    def test_the_queued_marker_lands_before_anything_awaits(self, db_path, monkeypatch):
        """The race behind two engineers on one task: the marker used to be
        posted from inside the spawned coroutine, after the next pass had
        already looked for it."""
        t = store.create_task("g", repo="r")

        async def fake_spawn(*a, **kw):
            await asyncio.sleep(0)
        monkeypatch.setattr(orchestrator, "spawn_worker", fake_spawn)

        async def go():
            bg = orchestrator._start_worker(t["id"], "engineer", "x", None)
            # Nothing has awaited yet — the next pass must already see it.
            assert orchestrator._worker_in_flight(t["id"])
            await bg
        asyncio.run(go())
