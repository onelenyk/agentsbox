"""Tests for core/store.py — the Task + Thread model."""
import pytest

from core.store import (
    create_task,
    delete_task,
    get_task,
    get_thread,
    list_children,
    list_tasks,
    post_message,
    render_thread,
    result_view,
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


class TestFanout:
    def test_child_carries_parent_id(self, db_path):
        parent = create_task("ticket PROJ-123")   # repo-less coordinator
        ios = create_task("iOS impl", repo="ios-app", parent_id=parent["id"])
        assert ios["parent_id"] == parent["id"]
        assert get_task(ios["id"])["parent_id"] == parent["id"]

    def test_top_level_task_has_no_parent(self, db_path):
        t = create_task("plain task", repo="sisyphus")
        assert t["parent_id"] is None

    def test_list_children(self, db_path):
        parent = create_task("ticket")
        ios = create_task("iOS", repo="ios-app", parent_id=parent["id"])
        android = create_task("Android", repo="android-app", parent_id=parent["id"])
        other = create_task("unrelated", repo="x")
        kids = list_children(parent["id"])
        assert [c["id"] for c in kids] == [ios["id"], android["id"]]
        assert other["id"] not in [c["id"] for c in kids]

    def test_list_children_empty_for_leaf(self, db_path):
        t = create_task("leaf", repo="x")
        assert list_children(t["id"]) == []


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


class TestWindowing:
    def test_short_thread_is_full_even_for_prompt(self, db_path):
        t = create_task("g", repo="sisyphus")
        for i in range(5):
            post_message(t["id"], "explorer", kind="note", text=f"note {i}")
        rendered = render_thread(t["id"], for_prompt=True)
        assert "note 0" in rendered and "note 4" in rendered
        assert "omitted" not in rendered

    def test_long_thread_windows_head_keeps_important(self, db_path):
        t = create_task("big task", repo="sisyphus")
        # a load-bearing result early in the head
        post_message(t["id"], "orchestrator", kind="note", text="[spawn_worker] start")
        post_message(t["id"], "explorer", kind="result", text="IMPORTANT early finding")
        # lots of routine worker chatter to push past the window
        for i in range(50):
            post_message(t["id"], "explorer", kind="note", text=f"routine chatter {i}")
        post_message(t["id"], "human", kind="answer", text="a recent human reply")

        full = render_thread(t["id"])
        windowed = render_thread(t["id"], for_prompt=True, max_messages=40, tail=10)

        assert len(windowed) < len(full)                 # actually shorter
        assert "IMPORTANT early finding" in windowed      # important head kept
        assert "[spawn_worker] start" in windowed          # orchestrator decision kept
        assert "a recent human reply" in windowed          # tail kept
        assert "omitted" in windowed                       # routine head collapsed
        # early routine chatter dropped, recent-tail chatter kept
        assert "routine chatter 0" not in windowed


class TestDelete:
    def test_delete_removes_task_and_thread(self, db_path):
        t = create_task("g", repo="sisyphus")
        post_message(t["id"], "orchestrator", kind="note", text="hi")
        assert delete_task(t["id"]) is True
        assert get_task(t["id"]) is None
        assert get_thread(t["id"]) == []

    def test_delete_unknown_returns_false(self, db_path):
        assert delete_task("task-nope") is False

    def test_delete_parent_cascades_to_children(self, db_path):
        parent = create_task("ticket")
        ios = create_task("iOS", repo="ios-app", parent_id=parent["id"])
        android = create_task("Android", repo="android-app", parent_id=parent["id"])
        post_message(ios["id"], "engineer", kind="commit", text="did it")

        assert delete_task(parent["id"]) is True
        assert get_task(parent["id"]) is None
        assert get_task(ios["id"]) is None          # child gone too
        assert get_task(android["id"]) is None
        assert get_thread(ios["id"]) == []          # child's thread gone

    def test_delete_child_leaves_parent(self, db_path):
        parent = create_task("ticket")
        ios = create_task("iOS", repo="ios-app", parent_id=parent["id"])
        assert delete_task(ios["id"]) is True
        assert get_task(parent["id"]) is not None   # deleting a child spares the parent


class TestResultView:
    def test_local_change_surfaces_commits_and_patch(self, db_path):
        t = create_task("g", repo="sisyphus")
        post_message(t["id"], "engineer", kind="commit", text="add x (abc12345)")
        post_message(t["id"], "engineer", kind="mr",
                     text="Add x\n\nBranch: sisyphus/x\n\n x | 1 +", attachment="change.patch")
        post_message(t["id"], "engineer", kind="result", text="did the thing")
        rv = result_view(t["id"])
        assert rv["summary"] == "did the thing"
        assert rv["commits"] == ["add x (abc12345)"]
        assert rv["change"]["attachment"] == "change.patch"
        assert rv["change"]["url"] is None

    def test_forge_change_extracts_url(self, db_path):
        t = create_task("g", repo="sisyphus")
        post_message(t["id"], "engineer", kind="mr", text="Add x: https://gh/pr/7")
        rv = result_view(t["id"])
        assert rv["change"]["url"] == "https://gh/pr/7"

    def test_no_change_is_none(self, db_path):
        t = create_task("g", repo="sisyphus")
        assert result_view(t["id"])["change"] is None


class TestTaskAgent:
    def test_agent_defaults_to_none(self, db_path):
        t = create_task("g", repo="x")
        assert t["agent"] is None

    def test_agent_persisted(self, db_path):
        t = create_task("g", repo="x", agent="engineer")
        assert t["agent"] == "engineer"
        assert get_task(t["id"])["agent"] == "engineer"

    def test_create_with_title(self, db_path):
        t = create_task("long goal text\nwith multiple lines", title="Short Title")
        assert t["title"] == "Short Title"
        assert t["goal"] == "long goal text\nwith multiple lines"

    def test_create_without_title(self, db_path):
        t = create_task("some goal")
        assert t["title"] is None

    def test_display_title_set(self, db_path):
        from core.store import task_display_title
        t = create_task("any goal", title="My Title")
        assert task_display_title(t) == "My Title"

    def test_display_title_fallback_short(self, db_path):
        from core.store import task_display_title
        t = create_task("short goal", title=None)
        assert task_display_title(t) == "short goal"

    def test_display_title_fallback_long(self, db_path):
        from core.store import task_display_title
        long = "x" * 100
        t = create_task(long, title=None)
        result = task_display_title(t)
        assert len(result) <= 83
        assert result.endswith("\u2026")

    def test_display_title_fallback_first_line(self, db_path):
        from core.store import task_display_title
        t = create_task("line1\nline2\nline3", title=None)
        assert task_display_title(t) == "line1"

    def test_display_title_whitespace_title(self, db_path):
        from core.store import task_display_title
        t = create_task("the goal", title="")
        assert task_display_title(t) == "the goal"


class TestMessageLane:
    """Classification table for message_lane — the human-UI lane split."""

    @staticmethod
    def _m(author="engineer", kind="note", text="", attachment=None):
        return {"author": author, "kind": kind, "text": text, "attachment": attachment}

    def test_conversation_kinds_are_dialogue(self):
        from core.store import message_lane
        for kind in ("answer", "question", "result", "mr", "plan", "external",
                     "child_finished"):
            assert message_lane(self._m(kind=kind)) == "dialogue", kind

    def test_commit_is_dialogue(self):
        from core.store import message_lane
        assert message_lane(self._m(kind="commit", text="add x (abc123)")) == "dialogue"

    def test_worker_lifecycle_is_machinery(self):
        from core.store import message_lane
        for kind in ("worker_started", "worker_queued", "worker_finished"):
            assert message_lane(self._m(author="system", kind=kind)) == "machinery", kind

    def test_system_note_is_machinery(self):
        from core.store import message_lane
        assert message_lane(self._m(author="system", kind="note",
                                    text="task reopened by human")) == "machinery"

    def test_orchestrator_note_machinery_tags(self):
        from core.store import message_lane
        for tag in ("spawn_worker", "note", "noop"):
            m = self._m(author="orchestrator", text=f"[{tag}] some reasoning")
            assert message_lane(m) == "machinery", tag

    def test_orchestrator_note_dialogue_tags(self):
        from core.store import message_lane
        for tag in ("done", "fail", "ask_human", "error", "spawn_task"):
            m = self._m(author="orchestrator", text=f"[{tag}] some reasoning")
            assert message_lane(m) == "dialogue", tag

    def test_orchestrator_note_unrecognized_tag_is_dialogue(self):
        from core.store import message_lane
        m = self._m(author="orchestrator", text="[future_action] new thing")
        assert message_lane(m) == "dialogue"

    def test_orchestrator_note_untagged_is_dialogue(self):
        from core.store import message_lane
        m = self._m(author="orchestrator", text="plain untagged note")
        assert message_lane(m) == "dialogue"

    def test_worker_note_with_attachment_is_dialogue(self):
        from core.store import message_lane
        m = self._m(author="engineer", text="draft diff", attachment="change.patch")
        assert message_lane(m) == "dialogue"

    def test_worker_note_plain_progress_is_machinery(self):
        from core.store import message_lane
        assert message_lane(self._m(author="engineer", text="running tests…")) == "machinery"

    def test_custom_agent_note_follows_worker_rule(self):
        from core.store import message_lane
        # the author set is open-ended — custom agents like mr-reviewer exist
        assert message_lane(self._m(author="mr-reviewer", text="looking…")) == "machinery"
        assert message_lane(self._m(author="mr-reviewer", attachment="x.patch")) == "dialogue"

    def test_unknown_kind_fails_open_to_dialogue(self):
        from core.store import message_lane
        assert message_lane(self._m(kind="mystery_kind")) == "dialogue"
