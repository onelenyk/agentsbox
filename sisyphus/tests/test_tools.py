"""core.tools — what workers can run and the worker_env the Tools page edits."""
from unittest.mock import patch

from core import tools


def test_tools_resolve_against_the_worker_path(tmp_path):
    b = tmp_path / "bin"
    b.mkdir()
    exe = b / "maestro"
    exe.write_text("#!/bin/sh\necho 2.10.0\n")
    exe.chmod(0o755)
    with patch.object(tools, "effective_env", return_value={"PATH": str(b)}):
        rows = {r["binary"]: r for r in tools.tools()}
    assert rows["maestro"]["ok"] and rows["maestro"]["path"] == str(exe)
    assert rows["maestro"]["version"] == "2.10.0"
    assert rows["adb"]["ok"] is False and "SDK" in rows["adb"]["hint"]


def test_path_entries_name_their_source(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    monkeypatch.setenv("PATH", "/usr/bin")
    with patch.object(tools, "effective_env",
                      return_value={"PATH": f"{real}:/usr/bin:/nope"}), \
         patch("core.config.worker_env", return_value={"PATH": f"{real}:/usr/bin"}):
        rows = {r["path"]: r for r in tools.path_entries()}
    assert rows[str(real)]["source"] == "worker_env" and rows[str(real)]["exists"]
    assert rows["/usr/bin"]["source"] == "daemon"
    assert rows["/nope"]["source"] == "install dirs" and rows["/nope"]["exists"] is False


def test_settings_round_trip_keeps_other_worker_env_keys():
    cfg = {"worker_env": {"PATH": "/opt/a:${PATH}", "FOO": "bar"}, "repos": []}
    saved = {}
    with patch("core.repos._read_config_file", return_value=cfg), \
         patch("core.repos._write_config_file", side_effect=lambda c: saved.update(c)):
        assert tools.settings() == {"extra_path": ["/opt/a"], "ANDROID_HOME": "", "JAVA_HOME": ""}
        tools.save_settings(["/opt/a", " ", "/opt/b", "/opt/a"], {"ANDROID_HOME": "~/sdk", "JAVA_HOME": ""})
    assert saved["worker_env"] == {"PATH": "/opt/a:/opt/b:${PATH}", "FOO": "bar", "ANDROID_HOME": "~/sdk"}


def test_clearing_everything_removes_the_path_key():
    cfg = {"worker_env": {"PATH": "/opt/a:${PATH}"}}
    saved = {}
    with patch("core.repos._read_config_file", return_value=cfg), \
         patch("core.repos._write_config_file", side_effect=lambda c: saved.update(c)):
        tools.save_settings([], {})
    assert "worker_env" not in saved
