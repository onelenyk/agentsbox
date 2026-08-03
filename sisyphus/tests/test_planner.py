"""Mission Planner core (core/planner.py) — mocked runner, no tokens spent."""
import json

import pytest

from core import planner, store


def _emit(monkeypatch):
    calls = []
    monkeypatch.setattr("core.orchestrator.emit", lambda tid, ev: calls.append((tid, ev)))
    return calls


def test_plan_crud(db_path):
    p = planner.create_plan("Ship X", repo="testrepo")
    assert p["id"].startswith("pl-") and p["repo"] == "testrepo"
    assert planner.get_plan(p["id"])["title"] == "Ship X"
    planner.update_plan(p["id"], title="Ship X v2")
    assert planner.get_plan(p["id"])["title"] == "Ship X v2"
    assert planner.latest_plan()["id"] == p["id"]
    assert planner.delete_plan(p["id"]) is True
    assert planner.get_plan(p["id"]) is None


def test_items_and_dag_state(db_path):
    p = planner.create_plan("p")
    a = planner.add_item(p["id"], "fix", position=0)
    b = planner.add_item(p["id"], "review", after=[a["id"]], position=1)
    by = {x["id"]: x for x in planner.list_items(p["id"])}
    assert planner.item_state(a, by) == "ready"      # no deps
    assert planner.item_state(b, by) == "blocked"    # a not done
    assert not planner.releasable(b, by)
    # finish a → b becomes ready
    planner.update_item(a["id"], status="done")
    by = {x["id"]: x for x in planner.list_items(p["id"])}
    assert planner.releasable(planner.get_item(b["id"]), by)


def test_cycle_detection(db_path):
    p = planner.create_plan("p")
    a = planner.add_item(p["id"], "a")
    b = planner.add_item(p["id"], "b", after=[a["id"]])
    planner.update_item(a["id"], after=[b["id"]])   # a→b→a cycle
    assert planner.has_cycle(planner.list_items(p["id"])) is True


def test_plan_turn_saves_brief(db_path, monkeypatch):
    p = planner.create_plan("p", repo="testrepo")
    brief_json = json.dumps({
        "approach": "Do the thing.",
        "diagram": "graph TD; A-->B",
        "steps": [
            {"goal": "fix bug", "repo": "testrepo", "agent": "engineer", "after": []},
            {"goal": "review", "repo": "testrepo", "agent": "reviewer", "after": [0]},
            {"goal": "bad", "agent": "ghost", "after": [99]},
        ],
        "risks": ["might break X"], "questions": ["which branch?"], "affected": ["calc.py"],
    })
    monkeypatch.setattr("core.ai_assist.run_prompt", lambda pr, **k: brief_json)
    b = planner.plan_turn(p["id"], "fix the offline bug")
    assert b["approach"] == "Do the thing." and b["diagram"].startswith("graph")
    assert len(b["steps"]) == 3
    assert b["steps"][2]["agent"] == ""          # unknown agent dropped
    assert planner.get_plan(p["id"])["brief_obj"]["risks"] == ["might break X"]


def test_convert_to_drafts_maps_deps(db_path, monkeypatch):
    p = planner.create_plan("p", repo="testrepo")
    monkeypatch.setattr("core.ai_assist.run_prompt", lambda pr, **k: json.dumps({
        "approach": "", "diagram": "", "risks": [], "questions": [], "affected": [],
        "steps": [
            {"goal": "step A", "repo": "testrepo", "agent": "", "after": []},
            {"goal": "step B", "repo": "testrepo", "agent": "", "after": [0]},
        ],
    }))
    planner.plan_turn(p["id"], "go")
    items = planner.convert_to_drafts(p["id"])
    assert len(items) == 2 and items[0]["goal"] == "step A"
    assert items[1]["after"] == [items[0]["id"]]     # index 0 → A's id
    assert not planner.has_cycle(items)


def test_launch_item(db_path, monkeypatch):
    calls = _emit(monkeypatch)
    p = planner.create_plan("p", repo="testrepo")
    a = planner.add_item(p["id"], "do A", repo="testrepo", agent="engineer")
    b = planner.add_item(p["id"], "do B", after=[a["id"]])

    # b is blocked → launch refused
    with pytest.raises(ValueError):
        planner.launch_item(b["id"])

    la = planner.launch_item(a["id"])
    assert la["status"] == "launched" and la["task_id"]
    assert (la["task_id"], "created") in calls
    assert store.get_task(la["task_id"])["goal"] == "do A"

    # a's task done → planner advances the item; b becomes releasable
    planner.on_task_terminal(la["task_id"], "done")
    assert planner.get_item(a["id"])["status"] == "done"
    by = {x["id"]: x for x in planner.list_items(p["id"])}
    assert planner.releasable(planner.get_item(b["id"]), by)


def test_on_task_terminal_failed(db_path, monkeypatch):
    _emit(monkeypatch)
    p = planner.create_plan("p", repo="testrepo")
    a = planner.add_item(p["id"], "do A", repo="testrepo")
    la = planner.launch_item(a["id"])
    planner.on_task_terminal(la["task_id"], "failed")
    assert planner.get_item(a["id"])["status"] == "failed"
