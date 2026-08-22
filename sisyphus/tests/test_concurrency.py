"""Global worker-concurrency cap (orchestrator._get_worker_gate / _spawn_worker_safe)."""
import asyncio

import pytest

from core import orchestrator


@pytest.mark.asyncio
async def test_worker_gate_bounds_concurrency(db_path, monkeypatch):
    live = 0
    peak = 0

    async def fake_spawn(task_id, role, instruction, repo_name=None):
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


@pytest.mark.asyncio
async def test_a_queued_reviewer_overtakes_queued_engineers(db_path, monkeypatch):
    """The failure this guards: with both slots busy, a reviewer — the cheap,
    read-only step that unblocks a whole task — waited ~10 minutes at the back
    of a FIFO behind queued engineering runs."""
    started = []

    async def fake_spawn(task_id, role, instruction, repo_name=None):
        started.append(role)
        await asyncio.sleep(0.01)

    monkeypatch.setattr(orchestrator, "spawn_worker", fake_spawn)
    orchestrator._worker_gate = orchestrator._PriorityGate(1)  # one slot: strict order

    from core import store
    tasks = [store.create_task(f"goal {i}") for i in range(4)]

    async def occupy():
        await orchestrator._spawn_worker_safe(tasks[0]["id"], "engineer", "x", None)

    holder = asyncio.create_task(occupy())
    await asyncio.sleep(0.001)          # holder owns the slot
    waiters = [
        asyncio.create_task(orchestrator._spawn_worker_safe(tasks[1]["id"], "engineer", "x", None)),
        asyncio.create_task(orchestrator._spawn_worker_safe(tasks[2]["id"], "engineer", "x", None)),
    ]
    await asyncio.sleep(0.001)          # engineers are queued first
    reviewer = asyncio.create_task(
        orchestrator._spawn_worker_safe(tasks[3]["id"], "reviewer", "x", None))
    await asyncio.gather(holder, *waiters, reviewer)

    assert started[0] == "engineer"     # the original holder
    assert started[1] == "reviewer", f"reviewer did not overtake: {started}"


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_leak_a_slot(db_path):
    gate = orchestrator._PriorityGate(1)
    await gate.acquire()
    waiter = asyncio.create_task(gate.acquire())
    await asyncio.sleep(0.001)
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
