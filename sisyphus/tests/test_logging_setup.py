"""Tests for core/logging_setup.py — where the daemon's log actually lands.

The path was derived from the module's own location, so a daemon started with
an isolated SISYPHUS_DB and WORKSPACE_ROOT still wrote into the real log. Those
startup lines then read as unexplained restarts of the running daemon.
"""
import importlib
from pathlib import Path

import pytest


@pytest.fixture
def fresh():
    """setup_logging is idempotent by design, so reload it per test."""
    import core.logging_setup as m
    importlib.reload(m)
    yield m
    importlib.reload(m)


def test_defaults_to_logs_beside_the_source(fresh, monkeypatch):
    monkeypatch.delenv("SISYPHUS_LOG_DIR", raising=False)
    p = fresh.setup_logging()
    assert p.name == "sisyphus.log"
    assert p.parent.name == "logs"


def test_override_redirects_the_log(fresh, monkeypatch, tmp_path):
    monkeypatch.setenv("SISYPHUS_LOG_DIR", str(tmp_path / "iso"))
    p = fresh.setup_logging()
    assert p == tmp_path / "iso" / "sisyphus.log"
    assert (tmp_path / "iso").is_dir(), "the directory is created, not assumed"


def test_override_creates_nested_directories(fresh, monkeypatch, tmp_path):
    monkeypatch.setenv("SISYPHUS_LOG_DIR", str(tmp_path / "a" / "b" / "c"))
    assert fresh.setup_logging().parent == tmp_path / "a" / "b" / "c"


def test_blank_override_falls_back_rather_than_using_cwd(fresh, monkeypatch):
    monkeypatch.setenv("SISYPHUS_LOG_DIR", "   ")
    assert fresh.setup_logging().parent.name == "logs"


def test_an_isolated_daemon_cannot_reach_the_real_log(fresh, monkeypatch, tmp_path):
    """The property that matters: given an override, nothing under it resolves
    back to the repo's own logs directory."""
    real = Path(fresh.__file__).resolve().parent.parent / "logs"
    monkeypatch.setenv("SISYPHUS_LOG_DIR", str(tmp_path / "iso"))
    assert fresh.setup_logging().parent.resolve() != real.resolve()


class TestPollAccessFilter:
    """The UI polls a handful of views on a timer; their successful GETs made
    up 37% of the access log and drowned the lines that mean something."""

    @staticmethod
    def _rec(method="GET", path="/api/inbox/view", status=200):
        import logging
        return logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 0,
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:1", method, path, "1.1", status), None)

    def test_poll_get_200_is_dropped(self):
        from core.logging_setup import drop_poll_access
        for path in ("/api/inbox/view", "/api/dashboard/view", "/api/workers/live",
                     "/api/tasks/list", "/api/v1/health",
                     "/api/tasks/task-abc/detail"):
            assert drop_poll_access(self._rec(path=path)) is False, path

    def test_errors_and_writes_always_log(self):
        from core.logging_setup import drop_poll_access
        assert drop_poll_access(self._rec(status=500)) is True
        assert drop_poll_access(self._rec(method="POST")) is True

    def test_other_paths_log(self):
        from core.logging_setup import drop_poll_access
        for path in ("/api/tasks/task-abc", "/", "/api/mr/mr-1/view", "/api/tasksXlist"):
            assert drop_poll_access(self._rec(path=path)) is True, path

    def test_unexpected_record_shape_is_kept(self):
        import logging
        from core.logging_setup import drop_poll_access
        rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 0, "plain", None, None)
        assert drop_poll_access(rec) is True
