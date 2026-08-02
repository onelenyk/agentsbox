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
