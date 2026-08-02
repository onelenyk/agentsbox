"""Runner binary auto-detection (core/runner_detect.py)."""
import os

from core import runner_detect


def test_resolve_absolute_path(tmp_path):
    exe = tmp_path / "mybin"
    exe.write_text("#!/bin/sh\n")
    os.chmod(exe, 0o755)
    assert runner_detect.resolve(str(exe)) == str(exe)
    assert runner_detect.resolve(str(tmp_path / "missing")) is None


def test_resolve_on_path(monkeypatch):
    monkeypatch.setattr(runner_detect.shutil, "which",
                        lambda b: "/usr/bin/opencode" if b == "opencode" else None)
    assert runner_detect.resolve("opencode") == "/usr/bin/opencode"


def test_resolve_from_extra_dir(tmp_path, monkeypatch):
    # not on PATH, but present in a known install dir
    binroot = tmp_path / "bin"
    binroot.mkdir()
    exe = binroot / "opencode"
    exe.write_text("#!/bin/sh\n")
    os.chmod(exe, 0o755)
    monkeypatch.setattr(runner_detect.shutil, "which", lambda b: None)
    monkeypatch.setattr(runner_detect, "_EXTRA_DIRS", [str(binroot)])
    assert runner_detect.resolve("opencode") == str(exe)


def test_resolve_missing_returns_none(monkeypatch):
    monkeypatch.setattr(runner_detect.shutil, "which", lambda b: None)
    monkeypatch.setattr(runner_detect, "_EXTRA_DIRS", [])
    assert runner_detect.resolve("definitely-not-real") is None


def test_detect_lists_found(monkeypatch):
    monkeypatch.setattr(runner_detect, "resolve",
                        lambda b: "/opt/opencode" if b == "opencode" else None)
    found = runner_detect.detect()
    assert found == [{"name": "opencode", "type": "opencode",
                      "binary": "opencode", "path": "/opt/opencode"}]
