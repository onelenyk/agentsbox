"""Startup preflight (core/preflight.py)."""
from core import preflight


def test_ready_when_db_and_binary_ok(db_path, monkeypatch):
    monkeypatch.setattr("core.runners.get_default_runner",
                        lambda: {"name": "r", "binary": "python3"})
    r = preflight.check()
    assert r["db"] is True and r["ok"] is True and r["runner"] == "r"


def test_degraded_when_binary_missing(db_path, monkeypatch):
    monkeypatch.setattr("core.runners.get_default_runner",
                        lambda: {"name": "r", "binary": "definitely-not-a-real-binary-xyz"})
    r = preflight.check()
    assert r["ok"] is False and "not found" in r["reason"]


def test_degraded_when_no_default_runner(db_path, monkeypatch):
    monkeypatch.setattr("core.runners.get_default_runner", lambda: None)
    r = preflight.check()
    assert r["ok"] is False and "no enabled default runner" in r["reason"]
