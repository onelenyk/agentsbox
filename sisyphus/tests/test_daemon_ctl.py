"""Daemon control helpers (core/daemon_ctl.py) — pure/rendered parts only.
launchctl/process side-effects are not exercised here."""
import plistlib

from core import daemon_ctl as d


def test_render_plist_fills_all_placeholders():
    xml = d.render_plist(python="/usr/bin/python3", workdir="/repo", logdir="/repo/logs")
    assert "__PYTHON__" not in xml and "__WORKDIR__" not in xml and "__LOGDIR__" not in xml
    doc = plistlib.loads(xml.encode())
    assert doc["Label"] == "com.sisyphus.daemon"
    assert doc["ProgramArguments"][0] == "/usr/bin/python3"
    assert "uvicorn" in doc["ProgramArguments"]
    assert doc["WorkingDirectory"] == "/repo"
    assert doc["KeepAlive"] == {"SuccessfulExit": False}
    assert doc["RunAtLoad"] is True
    assert doc["StandardErrorPath"] == "/repo/logs/daemon.err.log"


def test_render_plist_defaults_to_repo_and_sys_executable():
    xml = d.render_plist()
    doc = plistlib.loads(xml.encode())
    assert doc["WorkingDirectory"] == str(d.REPO_ROOT)
    assert doc["ProgramArguments"][0]  # some interpreter path


def test_status_running(monkeypatch):
    monkeypatch.setattr(d, "health", lambda: {"ok": True, "version": "2.0"})
    monkeypatch.setattr(d, "is_installed", lambda: True)
    # Stub the process probes so status() never spawns launchctl/lsof/ps —
    # the worker sandbox forbids spawning `ps` (Operation not permitted), and
    # these two functions are the only path that reaches it.
    monkeypatch.setattr(d, "_managed_pid", lambda: None)
    monkeypatch.setattr(d, "_port_listen_pid", lambda: None)
    s = d.status()
    assert s == {"running": True, "version": "2.0", "supervised": True, "url": d.HEALTH_URL}


def test_status_down(monkeypatch):
    monkeypatch.setattr(d, "health", lambda: None)
    monkeypatch.setattr(d, "is_installed", lambda: False)
    s = d.status()
    assert s["running"] is False and s["version"] is None and s["supervised"] is False


def test_foreign_pid_on_port_mismatch(monkeypatch):
    monkeypatch.setattr(d, "_managed_pid", lambda: 100)
    monkeypatch.setattr(d, "_port_listen_pid", lambda: 200)
    assert d._foreign_pid_on_port() == 200


def test_foreign_pid_on_port_match(monkeypatch):
    monkeypatch.setattr(d, "_managed_pid", lambda: 100)
    monkeypatch.setattr(d, "_port_listen_pid", lambda: 100)
    assert d._foreign_pid_on_port() is None


def test_foreign_pid_on_port_free(monkeypatch):
    monkeypatch.setattr(d, "_managed_pid", lambda: 100)
    monkeypatch.setattr(d, "_port_listen_pid", lambda: None)
    assert d._foreign_pid_on_port() is None


def test_foreign_pid_on_port_unmanaged(monkeypatch):
    monkeypatch.setattr(d, "_managed_pid", lambda: None)
    monkeypatch.setattr(d, "_port_listen_pid", lambda: 200)
    assert d._foreign_pid_on_port() is None


def test_status_detects_foreign_process(monkeypatch):
    monkeypatch.setattr(d, "health", lambda: {"ok": True, "version": "2.0"})
    monkeypatch.setattr(d, "is_installed", lambda: True)
    monkeypatch.setattr(d, "_managed_pid", lambda: 100)
    monkeypatch.setattr(d, "_port_listen_pid", lambda: 200)

    class _FakePs:
        stdout = "Mon Jan  1 12:00:00 2024"
        def strip(self):
            return self.stdout

    monkeypatch.setattr(d.subprocess, "run",
                        lambda cmd, capture_output=True, text=True: _FakePs())
    s = d.status()
    assert s["running"] is True
    assert s["version"] == "2.0"
    assert s["supervised"] is True
    assert s.get("foreign_pid") == 200
    assert s.get("managed_pid") == 100
    assert "foreign_start" in s


def test_status_no_foreign_process(monkeypatch):
    monkeypatch.setattr(d, "health", lambda: {"ok": True, "version": "2.0"})
    monkeypatch.setattr(d, "is_installed", lambda: True)
    monkeypatch.setattr(d, "_managed_pid", lambda: 100)
    monkeypatch.setattr(d, "_port_listen_pid", lambda: 100)
    s = d.status()
    assert s["running"] is True
    assert "foreign_pid" not in s


def test_start_refuses_foreign_process(monkeypatch):
    monkeypatch.setattr(d, "_port_listen_pid", lambda: 999)
    monkeypatch.setattr(d, "_managed_pid", lambda: None)
    result = d.start()
    assert "error" in result
    assert result["foreign_pid"] == 999
    assert "kill 999" in result["error"]


def test_restart_refuses_foreign_process(monkeypatch):
    monkeypatch.setattr(d, "health", lambda: {"ok": True, "workers": []})
    monkeypatch.setattr(d, "stop", lambda force=False: {"via": "launchd"})
    monkeypatch.setattr(d, "is_running", lambda: False)
    monkeypatch.setattr(d, "_port_listen_pid", lambda: 999)
    monkeypatch.setattr(d, "_managed_pid", lambda: None)
    result = d.restart()
    assert "error" in result
    assert result["foreign_pid"] == 999


class TestStoppingDoesNotSilentlyKillWork:
    """Killing the daemon kills its workers: the run dies, its worktree is left
    behind, and the task only recovers on the next boot's reconcile — at the
    cost of the whole run. That happened here on a live task and nothing warned
    anybody."""

    def test_stop_refuses_while_a_worker_is_running(self, monkeypatch):
        monkeypatch.setattr(d, "health", lambda: {"ok": True, "workers": [
            {"task_id": "task-1", "role": "engineer", "elapsed_mins": 13}]})
        called = []
        monkeypatch.setattr(d, "is_installed", lambda: called.append("stopped") or False)

        res = d.stop()

        assert [w["task_id"] for w in res["blocked"]] == ["task-1"]
        assert called == [], "it must not have started stopping"

    def test_restart_refuses_too(self, monkeypatch):
        monkeypatch.setattr(d, "health", lambda: {"ok": True, "workers": [
            {"task_id": "task-1", "role": "mr-reviewer", "elapsed_mins": 2}]})
        monkeypatch.setattr(d, "start", lambda: (_ for _ in ()).throw(
            AssertionError("must not restart")))

        assert d.restart()["blocked"]

    def test_force_goes_through(self, monkeypatch):
        monkeypatch.setattr(d, "health", lambda: {"ok": True, "workers": [
            {"task_id": "task-1", "role": "engineer", "elapsed_mins": 1}]})
        monkeypatch.setattr(d, "is_installed", lambda: True)
        monkeypatch.setattr(d, "_launchctl", lambda *a: None)

        assert d.stop(force=True) == {"via": "launchd"}

    def test_an_idle_daemon_stops_as_before(self, monkeypatch):
        monkeypatch.setattr(d, "health", lambda: {"ok": True, "workers": []})
        monkeypatch.setattr(d, "is_installed", lambda: True)
        monkeypatch.setattr(d, "_launchctl", lambda *a: None)

        assert "blocked" not in d.stop()

    def test_a_daemon_that_cannot_answer_names_no_workers(self, monkeypatch):
        """A down or older daemon reports nothing. That is not proof the
        machine is idle, but there is nothing we could name, and refusing to
        stop a daemon we cannot even reach would be worse."""
        monkeypatch.setattr(d, "health", lambda: None)
        assert d.running_workers() == []

    def test_the_refusal_names_the_run_and_how_long_it_has_been_going(self):
        text = d.describe_workers([{"task_id": "task-97a05500", "role": "engineer",
                                    "elapsed_mins": 13}])
        assert "engineer" in text and "task-97a05500" in text and "13m" in text
