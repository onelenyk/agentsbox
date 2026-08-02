"""Board Copilot (core/copilot.py) — mocked runner, no tokens spent."""
from core import copilot, store


def _emit(monkeypatch):
    calls = []
    monkeypatch.setattr("core.orchestrator.emit", lambda tid, ev: calls.append((tid, ev)))
    return calls


def test_board_snapshot_lists_tasks(db_path):
    store.create_task("do a thing", repo="testrepo", status="active")
    store.create_task("research X", status="waiting")
    snap = copilot.board_snapshot()
    assert "ACTIVE (1)" in snap and "WAITING (1)" in snap
    assert "do a thing" in snap and "Spend today" in snap


def test_chat_plain_reply(db_path, monkeypatch):
    monkeypatch.setattr("core.ai_assist.run_prompt", lambda p, **k: '{"reply": "Nothing is stuck."}')
    assert copilot.chat("what's stuck?", []) == {"reply": "Nothing is stuck."}


def test_chat_rejects_unknown_repo(db_path, monkeypatch):
    monkeypatch.setattr(
        "core.ai_assist.run_prompt",
        lambda p, **k: '{"reply": "I\'ll create it.", "action": {"type": "create_task", "goal": "x", "repo": "definitely-not-a-repo-xyz", "agent": ""}}')
    out = copilot.chat("...", [])
    assert "action" not in out and "couldn't prepare" in out["reply"]


def test_chat_action_ok_when_repo_empty(db_path, monkeypatch):
    monkeypatch.setattr(
        "core.ai_assist.run_prompt",
        lambda p, **k: '{"reply": "Creating it.", "action": {"type": "create_task", "goal": "research Postgres", "repo": "", "agent": ""}}')
    out = copilot.chat("research postgres", [])
    assert out["action"]["type"] == "create_task"


def test_validate_action(db_path):
    t = store.create_task("g", repo="")
    assert copilot.validate_action({"type": "create_task", "goal": "x"})[0]
    assert not copilot.validate_action({"type": "create_task", "goal": ""})[0]
    assert copilot.validate_action({"type": "reply_task", "task_id": t["id"], "text": "hi"})[0]
    assert not copilot.validate_action({"type": "reply_task", "task_id": "nope", "text": "hi"})[0]
    assert not copilot.validate_action({"type": "frobnicate"})[0]


def test_execute_create_and_reply(db_path, monkeypatch):
    calls = _emit(monkeypatch)
    res = copilot.execute_action({"type": "create_task", "goal": "add a function", "repo": "", "agent": ""})
    assert res.startswith("Created task")
    tid = [t["id"] for t in store.list_tasks()][0]
    assert (tid, "created") in calls

    copilot.execute_action({"type": "reply_task", "task_id": tid, "text": "please continue"})
    assert (tid, "human_replied") in calls
    thread = store.get_thread(tid)
    assert any(m["author"] == "human" and "continue" in m["text"] for m in thread)


def test_execute_autopilot_toggle(db_path):
    from core import autopilot
    a = autopilot.create_autopilot(name="ap", trigger_type="schedule", interval_seconds=60)
    copilot.execute_action({"type": "pause_autopilot", "id": a["id"]})
    assert autopilot.get_autopilot(a["id"])["enabled"] == 0
    copilot.execute_action({"type": "resume_autopilot", "id": a["id"]})
    assert autopilot.get_autopilot(a["id"])["enabled"] == 1
