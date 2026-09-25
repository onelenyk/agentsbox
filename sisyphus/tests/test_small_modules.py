"""Tests for the small modules that had none: attachments, cancellation, roles.

Small does not mean unimportant. attachments takes a filename from an agent and
turns it into a path; cancellation is what makes a Stop button do anything; roles
is the allowlist deciding which tools a worker can reach at all.
"""
import asyncio

import pytest

from core import attachments, cancellation, roles


class TestAttachmentNames:
    """The filename comes from an agent, so the name check is a boundary."""

    @pytest.mark.parametrize("bad", [
        "../escape.txt", "sub/dir.txt", "..\\windows.txt",
        ".hidden", "", "/absolute.txt",
    ])
    def test_unsafe_names_are_refused_on_read(self, bad, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        assert attachments.read("task-1", bad) is None

    @pytest.mark.parametrize("bad", ["../escape.txt", "sub/dir.txt", ".hidden", ""])
    def test_unsafe_names_are_refused_on_save(self, bad, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        wt = tmp_path / "wt"
        wt.mkdir()
        assert attachments.save_from_worktree("task-1", str(wt), bad) is False

    def test_a_plain_name_round_trips(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        attachments.save_text("task-1", "notes.md", "hello")
        assert attachments.read("task-1", "notes.md") == "hello"

    def test_missing_file_reads_as_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        assert attachments.read("task-1", "absent.txt") is None

    def test_one_task_cannot_read_anothers_attachment(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        attachments.save_text("task-1", "secret.txt", "mine")
        assert attachments.read("task-2", "secret.txt") is None


class TestAttachmentCopy:
    def test_copies_out_of_the_worktree(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / "report.txt").write_text("findings")
        assert attachments.save_from_worktree("task-1", str(wt), "report.txt") is True
        # Durable: it must survive the worktree being cleaned up.
        import shutil
        shutil.rmtree(wt)
        assert attachments.read("task-1", "report.txt") == "findings"

    def test_absent_source_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        wt = tmp_path / "wt"
        wt.mkdir()
        assert attachments.save_from_worktree("task-1", str(wt), "nope.txt") is False

    def test_a_directory_is_not_an_attachment(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        wt = tmp_path / "wt"
        (wt / "adir").mkdir(parents=True)
        assert attachments.save_from_worktree("task-1", str(wt), "adir") is False


class TestCancellation:
    def setup_method(self):
        cancellation.clear()

    def teardown_method(self):
        cancellation.clear()

    def test_request_signals_every_worker_on_the_task(self):
        async def go():
            a = cancellation.register("task-1", "engineer")
            b = cancellation.register("task-1", "reviewer")
            n = cancellation.request("task-1")
            return n, a.is_set(), b.is_set()
        n, a_set, b_set = asyncio.run(go())
        assert n == 2 and a_set and b_set

    def test_request_on_an_idle_task_reports_zero(self):
        """Zero is load-bearing: the caller still has to park the task, or Stop
        on an idle task would silently do nothing."""
        assert cancellation.request("task-nothing") == 0

    def test_other_tasks_are_untouched(self):
        async def go():
            mine = cancellation.register("task-1", "engineer")
            theirs = cancellation.register("task-2", "engineer")
            cancellation.request("task-1")
            return mine.is_set(), theirs.is_set()
        mine, theirs = asyncio.run(go())
        assert mine and not theirs

    def test_unregister_removes_the_task_when_last_role_goes(self):
        async def go():
            cancellation.register("task-1", "engineer")
            assert cancellation.is_running("task-1")
            cancellation.unregister("task-1", "engineer")
            return cancellation.is_running("task-1")
        assert asyncio.run(go()) is False

    def test_unregister_keeps_the_task_while_another_role_runs(self):
        async def go():
            cancellation.register("task-1", "engineer")
            cancellation.register("task-1", "reviewer")
            cancellation.unregister("task-1", "engineer")
            return cancellation.is_running("task-1")
        assert asyncio.run(go()) is True

    def test_active_tasks_is_sorted(self):
        async def go():
            for t in ("task-c", "task-a", "task-b"):
                cancellation.register(t, "engineer")
            return cancellation.active_tasks()
        assert asyncio.run(go()) == ["task-a", "task-b", "task-c"]

    def test_unregistering_something_absent_is_harmless(self):
        cancellation.unregister("task-never", "engineer")


class TestRoles:
    def test_every_role_can_report_and_read_context(self):
        """A role that cannot call get_context has nothing to work from, and one
        that cannot finish or decide can never close its session — which the
        worker reads as a crash."""
        for name in roles.list_roles():
            tools = roles.role_tools(name)
            assert "get_context" in tools, name
            assert any(t in tools for t in
                       ("finish", "approve", "request_changes")) or \
                   any(t.startswith("decide_") for t in tools), name

    def test_unknown_role_raises_and_names_the_known_ones(self):
        """Loudly, not silently: a typo'd role that quietly resolved to an empty
        toolset would spawn a worker that cannot do or report anything, which
        reads downstream as a crashed run."""
        with pytest.raises(ValueError) as e:
            roles.get_role("not-a-role")
        assert "not-a-role" in str(e.value)
        for known in roles.list_roles():
            assert known in str(e.value)

        with pytest.raises(ValueError):
            roles.role_tools("not-a-role")

    def test_read_only_roles_cannot_commit(self):
        for name in ("explorer", "reviewer"):
            if name in roles.list_roles():
                assert "commit" not in roles.role_tools(name), name
                assert "open_mr" not in roles.role_tools(name), name

    def test_reviewer_personas_defer_to_the_recorded_check(self):
        """Both reviewer personas must tell the reviewer to lean on the daemon's
        recorded verdict rather than re-run the whole suite in a sandbox, and to
        treat a missing verdict as "waiting", not CHANGES REQUESTED."""
        for name in ("reviewer", "mr-reviewer"):
            persona = roles.get_role(name)["persona"]
            flat = persona.replace("\n", " ")
            assert "Automated check" in flat, name
            assert "do NOT re-run the full suite" in flat, name
            assert "run only the tests relevant to the diff" in flat, name
            assert "waiting for the recorded check" in flat, name
            assert "not a reason for CHANGES REQUESTED" in flat, name
