"""Persistent copilot sessions (core/copilot_store.py)."""
from core import copilot_store as cs


def test_session_and_messages(db_path):
    s = cs.create_session()
    assert s["id"].startswith("cs-") and s["title"] == "New chat"
    cs.add_message(s["id"], "user", "what is stuck?")
    cs.add_message(s["id"], "assistant", "Nothing.")
    msgs = cs.get_messages(s["id"])
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["text"] == "what is stuck?"
    # title is set from the first user message
    assert cs.get_session(s["id"])["title"] == "what is stuck?"


def test_latest_and_list(db_path):
    a = cs.create_session()
    b = cs.create_session()
    cs.add_message(a["id"], "user", "later")  # bumps a's updated_at
    assert cs.latest_session()["id"] == a["id"]
    ids = [s["id"] for s in cs.list_sessions()]
    assert ids[0] == a["id"] and b["id"] in ids


def test_latest_or_create(db_path):
    assert cs.latest_session() is None
    s = cs.latest_or_create()
    assert s and cs.latest_or_create()["id"] == s["id"]  # doesn't create a second


def test_delete_removes_messages(db_path):
    s = cs.create_session()
    cs.add_message(s["id"], "user", "hi")
    assert cs.delete_session(s["id"]) is True
    assert cs.get_session(s["id"]) is None
    assert cs.get_messages(s["id"]) == []
