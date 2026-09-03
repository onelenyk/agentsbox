"""Per-task / daily budget guard (orchestrator._budget_exceeded + handle_event)."""
import pytest

from core import orchestrator, store, usage


def _spend(task_id, cost):
    usage.record_run(task_id, "engineer", "m", {
        "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
        "cache_read": 0, "cache_write": 0, "total_tokens": 0,
        "cost": cost, "steps": 1,
    })


def _caps(task=0, daily=0):
    def get_config(key, default=None):
        return {"task_budget_usd": task, "daily_budget_usd": daily}.get(key, default)
    return get_config


def test_budget_exceeded_over_and_under(db_path, monkeypatch):
    t = store.create_task("g", repo="r")
    _spend(t["id"], 6.0)
    monkeypatch.setattr("core.config.get_config", _caps(task=5.0))
    reason = orchestrator._budget_exceeded(t["id"])
    assert reason and "task budget" in reason

    monkeypatch.setattr("core.config.get_config", _caps(task=10.0))
    assert orchestrator._budget_exceeded(t["id"]) is None


def test_daily_budget(db_path, monkeypatch):
    t = store.create_task("g", repo="r")
    _spend(t["id"], 3.0)
    monkeypatch.setattr("core.config.get_config", _caps(daily=2.0))
    assert "daily budget" in orchestrator._budget_exceeded(t["id"])


def test_disabled_caps_never_trip(db_path, monkeypatch):
    t = store.create_task("g", repo="r")
    _spend(t["id"], 999.0)
    monkeypatch.setattr("core.config.get_config", _caps(task=0, daily=0))
    assert orchestrator._budget_exceeded(t["id"]) is None


@pytest.mark.asyncio
async def test_handle_event_parks_when_over_budget(db_path, monkeypatch):
    t = store.create_task("g", repo="r")
    _spend(t["id"], 6.0)
    monkeypatch.setattr("core.config.get_config", _caps(task=5.0))
    decision, bg = await orchestrator.handle_event(t["id"], "worker_finished")
    assert decision.action == "ask_human"
    assert store.get_task(t["id"])["status"] == "waiting"


@pytest.mark.asyncio
async def test_human_reply_bypasses_budget(db_path, monkeypatch):
    t = store.create_task("g", repo="r")
    _spend(t["id"], 6.0)
    monkeypatch.setattr("core.config.get_config", _caps(task=5.0))

    async def decide(task, thread):
        return orchestrator.Decision(action="note", why="proceeding")

    decision, bg = await orchestrator.handle_event(t["id"], "human_replied", decide_fn=decide)
    assert decision.why != "budget guard"


def test_daily_cap_defaults_on_when_unset(db_path, monkeypatch):
    t = store.create_task("g", repo="r")
    _spend(t["id"], 11.0)
    # a config with NO daily_budget_usd key at all: get_config returns the default
    monkeypatch.setattr("core.config.get_config",
                        lambda k, d=None: {"task_budget_usd": 0}.get(k, d))
    reason = orchestrator._budget_exceeded(t["id"])
    assert reason and "daily budget" in reason and "$10.00 cap" in reason


def test_explicit_zero_still_means_off(db_path, monkeypatch):
    t = store.create_task("g", repo="r")
    _spend(t["id"], 999.0)
    monkeypatch.setattr("core.config.get_config", _caps(task=0, daily=0))
    assert orchestrator._budget_exceeded(t["id"]) is None
