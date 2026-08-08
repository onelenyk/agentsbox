"""Global worker-concurrency cap (orchestrator._get_worker_sem / _spawn_worker_safe)."""
import asyncio

import pytest

from core import orchestrator


@pytest.mark.asyncio
async def test_worker_semaphore_bounds_concurrency(db_path, monkeypatch):
    live = 0
    peak = 0

    async def fake_spawn(task_id, role, instruction, repo_name=None):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.02)
        live -= 1

    monkeypatch.setattr(orchestrator, "spawn_worker", fake_spawn)
    orchestrator._worker_sem = asyncio.Semaphore(2)  # cap = 2

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
async def test_semaphore_from_config_default(db_path):
    orchestrator._worker_sem = None  # force lazy build from config
    sem = orchestrator._get_worker_sem()
    assert isinstance(sem, asyncio.Semaphore)
