"""The daemon's end — core.lifecycle. Closing the app must stop the work: the
loops, the workers, and any process the daemon started. Nothing outlives it."""
import asyncio
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from core import cancellation, lifecycle, orphans, store


@pytest.fixture(autouse=True)
def _clean():
    lifecycle.reset()
    cancellation.clear()
    yield
    lifecycle.reset()
    cancellation.clear()


async def test_shutdown_cancels_every_loop(db_path):
    async def forever():
        while True:
            await asyncio.sleep(1)
    a = lifecycle.start_loop(forever(), "a")
    b = lifecycle.start_loop(forever(), "b")
    await lifecycle.shutdown(grace=0)
    assert a.cancelled() and b.cancelled()


async def test_a_running_worker_is_stopped_and_its_task_parked(db_path):
    t = store.create_task("long job", repo="sisyphus")
    store.set_status(t["id"], "active")
    event = cancellation.register(t["id"], "engineer")

    async def worker():                      # stands in for spawn_worker
        await event.wait()
        cancellation.unregister(t["id"], "engineer")
    running = asyncio.create_task(worker())

    out = await lifecycle.shutdown(reason="Sisyphus was closed", grace=2)
    await running
    assert out["parked"] == [t["id"]]
    task = store.get_task(t["id"])
    # Paused, not waiting: closing Sisyphus asks nobody anything, and the next
    # boot picks it back up.
    assert (task["status"], task["pause_kind"]) == ("paused", "closed")
    assert any("paused — Sisyphus was closed. It resumes on its own when Sisyphus opens again."
               in m["text"] for m in store.get_thread(t["id"]))


async def test_no_process_the_daemon_owns_outlives_it(db_path):
    """A decider mid-run is not in the cancellation registry — it is killed by pid."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    orphans._OWNED.add(proc.pid)
    try:
        out = await lifecycle.shutdown(grace=0)
        assert out["terminated"][proc.pid] in ("terminated", "killed")
        assert proc.wait(timeout=5) is not None
        assert proc.pid not in orphans._OWNED
    finally:
        if proc.poll() is None:
            proc.kill()


async def test_shutdown_runs_once(db_path):
    await lifecycle.shutdown(grace=0)
    assert await lifecycle.shutdown(grace=0) == {"already": True}


async def test_the_daemon_leaves_when_its_parent_does(monkeypatch):
    asked = []
    monkeypatch.setattr(lifecycle, "request_exit", asked.append)
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    await asyncio.wait_for(lifecycle.watch_parent(gone.pid, poll=0.05), timeout=2)
    assert asked == ["the Sisyphus app went away"]


def test_shutdown_endpoint_is_loopback_only(db_path, monkeypatch):
    from web.app import app
    asked = []
    monkeypatch.setattr(lifecycle, "request_exit", asked.append)
    remote = TestClient(app, client=("10.0.0.5", 5000))
    assert remote.post("/api/v1/shutdown").status_code == 403
    local = TestClient(app, client=("127.0.0.1", 5000))
    r = local.post("/api/v1/shutdown")
    assert r.status_code == 202 and r.json() == {"stopping": True}
