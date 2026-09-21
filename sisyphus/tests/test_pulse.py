"""One cheap question for the whole UI — core/pulse.py.

Every live surface used to re-fetch itself on a timer whether or not anything
had happened: the board every 7 seconds at ~135 KB a time. The version string
here is what lets them stay still instead.
"""
import time
from unittest.mock import patch

from core import pulse, sessions, store


def test_an_idle_daemon_returns_the_same_version(db_path):
    sessions.clear()
    first = pulse.version()
    time.sleep(0.01)
    assert pulse.version() == first     # nothing happened, nothing moved


def test_a_new_task_moves_it(db_path):
    sessions.clear()
    before = pulse.version()
    store.create_task("something", repo="test-repo")
    assert pulse.version() != before


def test_a_new_message_moves_it(db_path):
    sessions.clear()
    t = store.create_task("something", repo="test-repo")
    before = pulse.version()
    store.post_message(t["id"], "engineer", kind="note", text="found it")
    assert pulse.version() != before


def test_a_status_change_moves_it(db_path):
    sessions.clear()
    t = store.create_task("something", repo="test-repo")
    before = pulse.version()
    store.set_status(t["id"], "done")
    assert pulse.version() != before


def test_a_worker_starting_moves_it(db_path):
    sessions.clear()
    before = pulse.version()
    sessions.mint("task-x", "engineer", kind="worker")
    try:
        assert pulse.version() != before
    finally:
        sessions.clear()


class TestAliveWhileAWorkerRuns:
    """The detail page's clock-driven lines — elapsed minutes, "last output 40s
    ago" — are not written to the database, so a version built only from rows
    would freeze the one page somebody is actually watching."""

    def test_the_version_ticks_on_its_own_during_a_run(self, db_path):
        sessions.clear()
        sessions.mint("task-x", "engineer", kind="worker")
        try:
            with patch("time.time", return_value=1_000_000.0):
                a = pulse.version()
            with patch("time.time", return_value=1_000_000.0 + pulse.LIVE_TICK_SECONDS):
                b = pulse.version()
            assert a != b
        finally:
            sessions.clear()

    def test_and_stands_still_when_nothing_runs(self, db_path):
        sessions.clear()
        with patch("time.time", return_value=1_000_000.0):
            a = pulse.version()
        with patch("time.time", return_value=1_000_500.0):
            b = pulse.version()
        assert a == b


def test_the_endpoint_answers_in_under_a_kilobyte(db_path):
    from fastapi.testclient import TestClient
    from web.app import app

    r = TestClient(app).get("/api/v1/pulse")
    assert r.status_code == 200
    assert r.json()["v"]
    assert len(r.content) < 1024
