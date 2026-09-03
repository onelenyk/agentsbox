from core import sessions


class TestMintExtendedFields:
    def test_mint_stores_all_new_fields(self):
        sessions.clear()
        token = sessions.mint(
            "task-1", "engineer", worktree="/tmp/wt",
            instruction="fix the login button",
            model="gpt-4", runner="opencode", kind="worker",
        )
        s = sessions.resolve(token)
        assert s is not None
        assert s["instruction"] == "fix the login button"
        assert s["model"] == "gpt-4"
        assert s["runner"] == "opencode"
        assert s["kind"] == "worker"

    def test_mint_new_fields_have_defaults(self):
        sessions.clear()
        token = sessions.mint("task-1", "engineer")
        s = sessions.resolve(token)
        assert s["instruction"] == ""
        assert s["model"] == ""
        assert s["runner"] == ""
        assert s["kind"] == "worker"


class TestListActive:
    def test_list_active_omits_token(self):
        sessions.clear()
        sessions.mint("task-a", "engineer", instruction="do x")
        sessions.mint("task-b", "reviewer", instruction="review y")
        active = sessions.list_active()
        assert len(active) == 2
        for s in active:
            assert "token" not in s

    def test_list_active_sorted_by_created_at_ascending(self):
        sessions.clear()
        sessions.mint("task-a", "engineer")
        import time
        time.sleep(0.01)
        sessions.mint("task-b", "reviewer")
        active = sessions.list_active()
        assert active[0]["created_at"] <= active[1]["created_at"]


class TestListForTask:
    def test_list_for_task_filters_by_task_id(self):
        sessions.clear()
        sessions.mint("task-1", "engineer", instruction="do x")
        sessions.mint("task-2", "reviewer", instruction="review y")
        sessions.mint("task-1", "explorer", instruction="explore z")
        result = sessions.list_for_task("task-1")
        assert len(result) == 2
        for s in result:
            assert s["task_id"] == "task-1"
            assert "token" not in s

    def test_list_for_task_returns_empty_for_unknown_task(self):
        sessions.clear()
        sessions.mint("task-1", "engineer")
        assert sessions.list_for_task("nope") == []


class TestClear:
    def test_clear_drops_all_sessions(self):
        sessions.clear()
        sessions.mint("task-a", "engineer")
        sessions.mint("task-b", "reviewer")
        assert len(sessions.list_active()) == 2
        sessions.clear()
        assert len(sessions.list_active()) == 0


class TestTokenNeverLeaks:
    def test_list_active_never_exposes_token(self):
        sessions.clear()
        sessions.mint("task-x", "engineer", instruction="do")
        for s in sessions.list_active():
            assert "token" not in s

    def test_list_for_task_never_exposes_token(self):
        sessions.clear()
        sessions.mint("task-x", "reviewer")
        for s in sessions.list_for_task("task-x"):
            assert "token" not in s


class TestTellingThinkingFromDead:
    """A run posts to the thread only when the agent chooses to call `comment`.
    So "13 minutes and no message" reads identically for a worker doing careful
    work and one that is already a corpse. The daemon has always known the
    difference — it stamps the same activity to enforce the idle timeout — it
    just never told anyone."""

    def test_a_fresh_session_has_produced_nothing(self):
        sessions.clear()
        sessions.mint("task-1", "engineer")
        assert sessions.running()[0]["last_output_at"] == ""

    def test_output_is_stamped_on_the_run(self):
        sessions.clear()
        sessions.mint("task-1", "engineer")
        sessions.note_output("task-1")
        assert sessions.running()[0]["last_output_at"]

    def test_it_stamps_every_session_of_that_task_and_no_other(self):
        sessions.clear()
        sessions.mint("task-1", "engineer")
        sessions.mint("task-1", "reviewer")
        sessions.mint("task-2", "engineer")
        sessions.note_output("task-1")
        by_task = {}
        for s in sessions.running():
            by_task.setdefault(s["task_id"], []).append(bool(s["last_output_at"]))
        assert by_task["task-1"] == [True, True]
        assert by_task["task-2"] == [False]

    def test_a_stamp_for_a_task_with_no_session_is_harmless(self):
        """The callback fires from the launcher's read loop; a session revoked
        mid-run must not turn that into an exception."""
        sessions.clear()
        sessions.note_output("task-gone")

    def test_running_reports_what_the_cli_needs_to_name_a_run(self):
        sessions.clear()
        sessions.mint("task-1", "engineer")
        row = sessions.running()[0]
        assert set(row) == {"task_id", "role", "elapsed_mins", "last_output_at"}
