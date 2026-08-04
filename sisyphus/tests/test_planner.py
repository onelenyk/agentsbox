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


def test_plan_turn_records_conversation(db_path, monkeypatch):
    p = planner.create_plan("p", repo="testrepo")
    planner.add_message(p["id"], "user", "add offline mode")   # simulate start_plan_turn
    monkeypatch.setattr("core.ai_assist.run_prompt", lambda pr, **k: json.dumps({
        "reply": "Got it — sketched the offline sync flow on the canvas.",
        "approach": "cache-first", "diagram": "", "steps": [],
        "risks": [], "questions": [], "affected": [],
    }))
    b = planner.plan_turn(p["id"], "add offline mode")
    assert b["reply"].startswith("Got it")
    msgs = planner.list_messages(p["id"])
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["text"] == b["reply"]


def test_plan_turn_unparseable_raises(db_path, monkeypatch):
    p = planner.create_plan("p", repo="testrepo")
    planner.update_plan(p["id"], brief=json.dumps({"approach": "keep me", "steps": [],
        "diagram": "", "risks": [], "questions": [], "affected": []}))
    monkeypatch.setattr("core.ai_assist.run_prompt", lambda pr, **k: "sorry, I can't help with that")
    with pytest.raises(ValueError):
        planner.plan_turn(p["id"], "do something")
    # existing brief is preserved, not clobbered with an empty shell
    assert planner.get_plan(p["id"])["brief_obj"]["approach"] == "keep me"


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


def test_auto_release_cascades(db_path, monkeypatch):
    _emit(monkeypatch)
    p = planner.create_plan("p", repo="testrepo")
    planner.update_plan(p["id"], auto_release=1)
    a = planner.add_item(p["id"], "A", repo="testrepo", agent="engineer")
    b = planner.add_item(p["id"], "B", repo="testrepo", after=[a["id"]])
    c = planner.add_item(p["id"], "C", repo="testrepo", after=[b["id"]])
    la = planner.launch_item(a["id"])

    # A done → B is auto-launched (not left as a ready draft)
    planner.on_task_terminal(la["task_id"], "done")
    b = planner.get_item(b["id"])
    assert b["status"] == "launched" and b["task_id"]
    assert planner.get_item(c["id"])["status"] == "draft"   # still blocked by B

    # B done → C auto-launches too — the whole chain runs hands-off
    planner.on_task_terminal(b["task_id"], "done")
    assert planner.get_item(c["id"])["status"] == "launched"


def test_auto_release_off_by_default(db_path, monkeypatch):
    _emit(monkeypatch)
    p = planner.create_plan("p", repo="testrepo")
    a = planner.add_item(p["id"], "A", repo="testrepo")
    b = planner.add_item(p["id"], "B", repo="testrepo", after=[a["id"]])
    la = planner.launch_item(a["id"])
    planner.on_task_terminal(la["task_id"], "done")
    assert planner.get_item(b["id"])["status"] == "draft"   # ready, but not auto-launched


def test_auto_merge_invoked_on_done(db_path, monkeypatch):
    _emit(monkeypatch)
    merged = []
    monkeypatch.setattr("core.planner._auto_merge_task", lambda tid: merged.append(tid))
    p = planner.create_plan("p", repo="testrepo")
    planner.update_plan(p["id"], auto_merge=1)
    a = planner.add_item(p["id"], "A", repo="testrepo", agent="engineer")
    la = planner.launch_item(a["id"])
    planner.on_task_terminal(la["task_id"], "done")
    assert merged == [la["task_id"]]

    # failed task → no merge attempt
    merged.clear()
    b = planner.add_item(p["id"], "B", repo="testrepo")
    lb = planner.launch_item(b["id"])
    planner.on_task_terminal(lb["task_id"], "failed")
    assert merged == []
