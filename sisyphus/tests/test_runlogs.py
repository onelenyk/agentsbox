"""Tests for core/runlogs.py — reading a task's run logs.

These files carry the only record of what a worker's CLI printed, and until now
nothing in the app could read them. The name of a log arrives from a URL, so the
traversal tests here are load-bearing rather than ceremonial.
"""
import os

from core import runlogs


def _write_log(ws, task_id, role, text):
    d = os.path.join(ws, "runs", task_id, "logs")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{task_id}-{role}.log")
    with open(p, "w") as fh:
        fh.write(text)
    return p


class TestListing:
    def test_lists_logs_with_readable_roles(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        _write_log(str(tmp_path), "task-1", "engineer", "a")
        _write_log(str(tmp_path), "task-1", "reviewer", "b")
        roles = {r["role"] for r in runlogs.list_logs("task-1")}
        assert roles == {"engineer", "reviewer"}

    def test_newest_first(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        old = _write_log(str(tmp_path), "task-1", "orchestrator", "old")
        new = _write_log(str(tmp_path), "task-1", "engineer", "new")
        os.utime(old, (1, 1))
        os.utime(new, (2, 2))
        assert runlogs.list_logs("task-1")[0]["role"] == "engineer"

    def test_no_logs_is_empty_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        assert runlogs.list_logs("task-nope") == []

    def test_ignores_non_log_files(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        _write_log(str(tmp_path), "task-1", "engineer", "a")
        d = os.path.join(str(tmp_path), "runs", "task-1", "logs")
        with open(os.path.join(d, "notes.txt"), "w") as fh:
            fh.write("x")
        assert [r["name"] for r in runlogs.list_logs("task-1")] == ["task-1-engineer.log"]


class TestReading:
    def test_reads_a_log(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        _write_log(str(tmp_path), "task-1", "engineer", "=== STDOUT ===\nhello")
        assert "hello" in runlogs.read_log("task-1", "task-1-engineer.log")

    def test_unknown_name_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        _write_log(str(tmp_path), "task-1", "engineer", "x")
        assert runlogs.read_log("task-1", "task-1-nosuch.log") is None

    def test_tail_is_truncated_with_a_notice(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        monkeypatch.setattr(runlogs, "TAIL_BYTES", 1000)
        _write_log(str(tmp_path), "task-1", "engineer", "x" * 5000 + "\nTAIL MARKER")
        out = runlogs.read_log("task-1", "task-1-engineer.log")
        assert "truncated" in out
        assert "TAIL MARKER" in out
        assert len(out) < 2000


class TestTraversalIsRefused:
    """The log name comes straight from a URL."""

    def test_parent_traversal(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        _write_log(str(tmp_path), "task-1", "engineer", "x")
        secret = tmp_path / "secret.log"
        secret.write_text("SECRET")
        assert runlogs.read_log("task-1", "../../../secret.log") is None

    def test_absolute_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        _write_log(str(tmp_path), "task-1", "engineer", "x")
        assert runlogs.read_log("task-1", "/etc/passwd") is None

    def test_another_tasks_log(self, tmp_path, monkeypatch):
        """A task may only read its own logs, even though both are real files."""
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        _write_log(str(tmp_path), "task-1", "engineer", "mine")
        _write_log(str(tmp_path), "task-2", "engineer", "theirs")
        assert runlogs.read_log("task-1", "task-2-engineer.log") is None
