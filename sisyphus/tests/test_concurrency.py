"""Global worker-concurrency cap (orchestrator._get_worker_gate / _spawn_worker_safe)."""
import asyncio

import pytest

from core import orchestrator


@pytest.mark.asyncio
async def test_worker_gate_bounds_concurrency(db_path, monkeypatch):
    live = 0
    peak = 0

    async def fake_spawn(task_id, role, instruction, repo_name=None, **kw):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.02)
        live -= 1

    monkeypatch.setattr(orchestrator, "spawn_worker", fake_spawn)
    orchestrator._worker_gate = orchestrator._PriorityGate(2)  # cap = 2

    # Real task rows: _spawn_worker_safe posts a worker_queued marker, and
    # messages.task_id has a foreign key to tasks.
    from core import store
    tasks = [store.create_task(f"goal {i}") for i in range(5)]

    await asyncio.gather(*[
        orchestrator._spawn_worker_safe(t["id"], "engineer", "x", None)
        for t in tasks
    ])

    assert peak == 2, f"expected concurrency capped at 2, saw peak {peak}"


@pytest.mark.asyncio
async def test_gate_from_config_default(db_path):
    orchestrator._worker_gate = None  # force lazy build from config
    gate = orchestrator._get_worker_gate()
    assert isinstance(gate, orchestrator._PriorityGate)


async def _wait_for_waiters(gate, n: int, timeout: float = 5) -> None:
    """Poll until the gate reports at least ``n`` queued waiters. The gate keeps
    its waiters in ``{True: deque, False: deque}`` (high/review first); count
    both lanes. Wrapped in asyncio.wait_for by callers so a regression fails
    instead of hanging."""
    async def queued() -> None:
        while sum(len(q) for q in gate._waiters.values()) < n:
            await asyncio.sleep(0.001)
    await asyncio.wait_for(queued(), timeout=timeout)


@pytest.mark.asyncio
async def test_a_queued_reviewer_overtakes_queued_engineers(db_path, monkeypatch):
    """The failure this guards: with both slots busy, a reviewer — the cheap,
    read-only step that unblocks a whole task — waited ~10 minutes at the back
    of a FIFO behind queued engineering runs."""
    started = []
    holder_running = asyncio.Event()
    release_holder = asyncio.Event()

    from core import store
    tasks = [store.create_task(f"goal {i}") for i in range(4)]
    holder_id = tasks[0]["id"]

    async def fake_spawn(task_id, role, instruction, repo_name=None, **kw):
        started.append(role)
        if task_id == holder_id:
            holder_running.set()
            # Hold the slot until the test has queued everyone behind it. This
            # is the ordering the old `await asyncio.sleep(0.001)` guessed at;
            # a real condition cannot be starved by load.
            await release_holder.wait()

    monkeypatch.setattr(orchestrator, "spawn_worker", fake_spawn)
    orchestrator._worker_gate = orchestrator._PriorityGate(1)  # one slot: strict order
    gate = orchestrator._worker_gate

    holder = asyncio.create_task(
        orchestrator._spawn_worker_safe(holder_id, "engineer", "x", None))
    await asyncio.wait_for(holder_running.wait(), timeout=5)   # holder owns the slot
    waiters = [
        asyncio.create_task(orchestrator._spawn_worker_safe(tasks[1]["id"], "engineer", "x", None)),
        asyncio.create_task(orchestrator._spawn_worker_safe(tasks[2]["id"], "engineer", "x", None)),
    ]
    await _wait_for_waiters(gate, 2)                            # engineers are queued first
    reviewer = asyncio.create_task(
        orchestrator._spawn_worker_safe(tasks[3]["id"], "reviewer", "x", None))
    await _wait_for_waiters(gate, 3)                            # reviewer queued behind them
    release_holder.set()
    await asyncio.gather(holder, *waiters, reviewer)

    assert started[0] == "engineer"     # the original holder
    assert started[1] == "reviewer", f"reviewer did not overtake: {started}"


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_leak_a_slot(db_path):
    gate = orchestrator._PriorityGate(1)
    await gate.acquire()
    waiter = asyncio.create_task(gate.acquire())
    await _wait_for_waiters(gate, 1)    # waiter is queued, not guessed
    waiter.cancel()
    try:
        await waiter
    except asyncio.CancelledError:
        pass
    gate.release()
    # The slot must be reusable — a leaked slot would hang here forever.
    await asyncio.wait_for(gate.acquire(), timeout=1)
    gate.release()


def test_review_roles_are_recognised():
    assert orchestrator._is_review_role("reviewer")
    assert orchestrator._is_review_role("mr-reviewer")
    assert not orchestrator._is_review_role("engineer")
    assert not orchestrator._is_review_role("")
