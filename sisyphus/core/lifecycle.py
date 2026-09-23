"""The daemon's life: its background loops, and how it ends.

Sisyphus used to have no end. Boot started six loops and nothing ever stopped
them; a SIGTERM ended uvicorn and left the workers behind — reparented to PID
1, still spending the API key, invisible to the next daemon (core.orphans
exists because of it). Now that the desktop app owns the daemon — open the
app, the work runs; close it, the work stops — stopping has to be a real,
ordered thing:

  1. no new work: cancel every loop, the dispatcher first among them;
  2. every task with a worker running is signalled the way the Stop button
     signals it (core.cancellation) and parked as ``waiting`` with a line
     that says why — a reply resumes it, exactly as after a Stop;
  3. the workers get a grace period to exit through the launcher's own
     SIGTERM → SIGKILL path, which closes their thread markers cleanly;
  4. whatever this process still owns after that — an orchestrator decision
     mid-run, a worker that ignored its event — is terminated by pid. Only
     pids this daemon spawned and still owns (core.orphans._OWNED).

Three things lead here: uvicorn's lifespan ending (any SIGTERM or Ctrl-C),
``POST /api/v1/shutdown`` from the app, and the parent watch — when the app
that started us disappears without saying goodbye.
"""
import asyncio
import logging
import os
import signal
from typing import List, Optional

logger = logging.getLogger(__name__)

WORKER_GRACE_SECONDS = 15.0
PARENT_POLL_SECONDS = 1.0

_LOOPS: List[asyncio.Task] = []
_STOPPING = False
_REASON = "Sisyphus was closed"


def start_loop(coro, name: str) -> asyncio.Task:
    """Run a background loop the daemon will stop on its way out."""
    task = asyncio.create_task(coro, name=name)
    _LOOPS.append(task)
    return task


def stopping() -> bool:
    return _STOPPING


def request_exit(reason: str) -> None:
    """End the process the ordinary way: a SIGTERM to ourselves, which uvicorn
    turns into a graceful exit that runs the lifespan — and so shutdown()."""
    global _REASON
    _REASON = reason
    logger.info("exit requested: %s", reason)
    os.kill(os.getpid(), signal.SIGTERM)


async def shutdown(reason: Optional[str] = None, grace: float = WORKER_GRACE_SECONDS) -> dict:
    """Stop everything this daemon started. Idempotent. Returns what it did."""
    global _STOPPING
    if _STOPPING:
        return {"already": True}
    _STOPPING = True
    reason = reason or _REASON
    from core import cancellation, orphans, store

    # 1. No new work.
    for task in _LOOPS:
        task.cancel()
    await asyncio.gather(*_LOOPS, return_exceptions=True)
    _LOOPS.clear()

    # 2. Park every task a worker is running on, as Stop does.
    parked = []
    for task_id in cancellation.active_tasks():
        cancellation.request(task_id, why=reason)
        t = store.get_task(task_id)
        if t and t["status"] not in ("done", "failed"):
            store.set_status(task_id, "waiting", cause=reason)
            store.post_message(task_id, "system", kind="note",
                               text=f"stopped — {reason}. Reply to resume.")
            parked.append(task_id)

    # 3. Let them go through the launcher's own exit path.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + grace
    while cancellation.active_tasks() and loop.time() < deadline:
        await asyncio.sleep(0.2)

    # 4. Anything we still own does not outlive us.
    leftover = sorted(orphans._OWNED)
    outcomes = {}
    if leftover:
        outcomes = await asyncio.to_thread(lambda: {pid: orphans._terminate(pid) for pid in leftover})
        for pid, how in outcomes.items():
            orphans.release(pid, still_running=how in ("survived", "denied"))

    logger.info("shutdown (%s): parked %d task(s), terminated %d process(es)",
                reason, len(parked), len(outcomes))
    return {"reason": reason, "parked": parked, "terminated": outcomes}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def watch_parent(pid: int, poll: float = PARENT_POLL_SECONDS) -> None:
    """Exit when the process that started us is gone.

    The app stops the daemon politely when it quits; this is for when it
    cannot — a crash, a force quit. Without it the daemon would live on as a
    daemon nobody owns, holding the port the next app launch needs.
    """
    while True:
        if os.getppid() != pid or not _alive(pid):
            request_exit("the Sisyphus app went away")
            return
        await asyncio.sleep(poll)


def reset() -> None:
    """Test helper."""
    global _STOPPING, _REASON
    _LOOPS.clear()
    _STOPPING = False
    _REASON = "Sisyphus was closed"
