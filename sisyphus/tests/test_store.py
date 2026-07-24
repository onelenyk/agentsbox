"""Tests for core/store.py — the Task + Thread model."""
import pytest

from core.store import (
    create_task,
    get_task,
    get_thread,
    list_tasks,
    post_message,
    render_thread,
    set_status,
)


class TestTasks:
    def test_create_defaults_to_active(self, db_path):
        t = create_task("map the core modules", repo="sisyphus")
        assert t["id"].startswith("task-")
        assert t["goal"] == "map the core modules"
        assert t["repo"] == "sisyphus"
        assert t["status"] == "active"

    def test_get_task(self, db_path):
        t = create_task("g")
        assert get_task(t["id"])["id"] == t["id"]
        assert get_task("task-nope") is None

    def test_list_and_filter(self, db_path):
        a = create_task("a")
        b = create_task("b")
        set_status(b["id"], "done")
        assert {t["id"] for t in list_tasks()} == {a["id"], b["id"]}
        assert [t["id"] for t in list_tasks(status="done")] == [b["id"]]

    def test_set_status_valid(self, db_path):
        t = create_task("g")
        for s in ("waiting", "active", "done", "failed"):
            set_status(t["id"], s)
            assert get_task(t["id"])["status"] == s

    def test_set_status_invalid_rejected(self, db_path):
        t = create_task("g")
        with pytest.raises(ValueError):
            set_status(t["id"], "bogus")


class TestThread:
    def test_post_and_get(self, db_path):
        t = create_task("g")
        post_message(t["id"], author="orchestrator", kind="note", text="starting")
        post_message(t["id"], author="explorer", kind="result", text="found 7 modules")
        thread = get_thread(t["id"])
        assert [m["seq"] for m in thread] == [1, 2]
        assert [m["author"] for m in thread] == ["orchestrator", "explorer"]
        assert thread[1]["text"] == "found 7 modules"

    def test_seq_is_monotonic_per_task(self, db_path):
        a = create_task("a")
        b = create_task("b")
        post_message(a["id"], "human", text="1")
        post_message(b["id"], "human", text="1")  # b starts at its own seq 1
        post_message(a["id"], "human", text="2")
        assert [m["seq"] for m in get_thread(a["id"])] == [1, 2]
        assert [m["seq"] for m in get_thread(b["id"])] == [1]

    def test_attachment_stored(self, db_path):
        t = create_task("g")
        m = post_message(t["id"], "explorer", kind="result", text="see analysis",
                         attachment="analysis.md")
        assert m["attachment"] == "analysis.md"

    def test_posting_bumps_task_updated_at(self, db_path):
        t = create_task("g")
        before = get_task(t["id"])["updated_at"]
        post_message(t["id"], "human", text="hi")
        assert get_task(t["id"])["updated_at"] >= before

    def test_render_thread(self, db_path):
        t = create_task("map modules", repo="sisyphus")
        post_message(t["id"], "orchestrator", kind="note", text="spawning explorer")
        post_message(t["id"], "explorer", kind="result", text="done", attachment="a.md")
        rendered = render_thread(t["id"])
        assert "Task: map modules" in rendered
        assert "Status: active" in rendered
        assert "[orchestrator/note] spawning explorer" in rendered
        assert "[explorer/result] done  [attachment: a.md]" in rendered

    def test_render_empty_thread(self, db_path):
        t = create_task("g")
        assert "(no messages yet)" in render_thread(t["id"])
