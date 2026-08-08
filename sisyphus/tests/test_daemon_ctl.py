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
    monkeypatch.setattr(d, "stop", lambda: {"via": "launchd"})
    monkeypatch.setattr(d, "is_running", lambda: False)
    monkeypatch.setattr(d, "_port_listen_pid", lambda: 999)
    monkeypatch.setattr(d, "_managed_pid", lambda: None)
    result = d.restart()
    assert "error" in result
    assert result["foreign_pid"] == 999
