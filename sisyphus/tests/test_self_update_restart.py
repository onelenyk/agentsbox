"""Self-update restart logic tests — verify safe restart when workers are idle.

The real restart is never run: ``perform_restart`` is stubbed everywhere so a
test cannot restart the live daemon. These tests exercise the decision logic —
the pending flag, the loop pass, and the /api/daemon/restart endpoint — and the
pre-existing ``daemon_ctl.restart`` guard it delegates to.
"""
import subprocess
import pathlib
import tempfile

from core import daemon_ctl, self_update


def _git(path, *args):
    return subprocess.run(list(args), cwd=str(path), capture_output=True,
                          text=True, check=True)


def _init_git_repo(path) -> str:
    """Init a repo with one commit; returns HEAD short SHA."""
    path = pathlib.Path(path)
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "git", "init")
    _git(path, "git", "config", "user.email", "test@test.com")
    _git(path, "git", "config", "user.name", "Test")
    (path / "file.txt").write_text("initial")
    _git(path, "git", "add", ".")
    _git(path, "git", "commit", "-m", "initial")
    return _git(path, "git", "rev-parse", "--short", "HEAD").stdout.strip()


def _add_commits(path, count):
    path = pathlib.Path(path)
    for i in range(count):
        (path / f"file_{i}.txt").write_text(f"content {i}")
        _git(path, "git", "add", ".")
        _git(path, "git", "commit", "-m", f"commit {i}")


# ── daemon_ctl.restart's own guard (pre-existing, exercised directly) ──────

def test_daemon_ctl_restart_refuses_while_workers_running(monkeypatch):
    """daemon_ctl.restart(force=False) returns {blocked: [worker]} when busy."""
    workers_list = [{"task_id": "t1", "role": "engineer", "elapsed_mins": 5}]
    monkeypatch.setattr(daemon_ctl, "running_workers", lambda: workers_list)

    result = daemon_ctl.restart(force=False)

    # Should refuse restart while workers are running
    assert result.get("blocked") == workers_list


def test_daemon_ctl_running_workers_queries_health(monkeypatch):
    """running_workers() returns worker list from health endpoint."""
    workers_list = [
        {"task_id": "t1", "role": "engineer", "elapsed_mins": 3},
        {"task_id": "t2", "role": "reviewer", "elapsed_mins": 1},
    ]
    monkeypatch.setattr(daemon_ctl, "health", lambda: {"workers": workers_list})

    result = daemon_ctl.running_workers()

    assert result == workers_list


def test_daemon_ctl_running_workers_empty_when_no_health(monkeypatch):
    """running_workers() returns [] when daemon is unreachable."""
    monkeypatch.setattr(daemon_ctl, "health", lambda: None)

    result = daemon_ctl.running_workers()

    assert result == []


# ── the pending-flag / deferred restart, which the reviewer found was dead ─

def test_request_restart_sets_pending_flag(monkeypatch):
    """request_restart() records a restart that the loop must perform later."""
    self_update.reset()
    assert not self_update.pending_restart()
    self_update.request_restart()
    assert self_update.pending_restart()


def test_restart_pass_waits_while_worker_runs(monkeypatch):
    """A worker running → no restart is triggered and the pending flag stays."""
    with tempfile.TemporaryDirectory() as tmpdir:
        start_sha = _init_git_repo(tmpdir)
        _add_commits(tmpdir, 2)

        self_update.reset()
        self_update.request_restart()
        monkeypatch.setattr(self_update, "perform_restart",
                            lambda: (_ for _ in ()).throw(AssertionError("must not restart")))

        # A worker is mid-run
        monkeypatch.setattr("core.sessions.running", lambda: [{"task_id": "t1"}])
        monkeypatch.setattr("core.config.get_config", lambda key, default=None: default)

        self_update._restart_pass(start_sha, tmpdir)

        # No restart happened and the request is still pending
        assert self_update.pending_restart()


def test_restart_pass_restarts_once_idle(monkeypatch):
    """Last worker finished → the loop performs the restart and clears pending."""
    with tempfile.TemporaryDirectory() as tmpdir:
        start_sha = _init_git_repo(tmpdir)
        _add_commits(tmpdir, 2)

        self_update.reset()
        self_update.request_restart()

        spawned = []
        monkeypatch.setattr(self_update, "perform_restart",
                            lambda: spawned.append(True) or {"spawned": True})
        monkeypatch.setattr("core.sessions.running", lambda: [])
        monkeypatch.setattr("core.config.get_config", lambda key, default=None: default)

        self_update._restart_pass(start_sha, tmpdir)

        assert spawned == [True]
        assert not self_update.pending_restart()


def test_auto_restart_when_idle_is_consumed(monkeypatch):
    """auto_restart_when_idle=true + behind + idle → restart happens."""
    with tempfile.TemporaryDirectory() as tmpdir:
        start_sha = _init_git_repo(tmpdir)
        _add_commits(tmpdir, 2)

        self_update.reset()
        spawned = []
        monkeypatch.setattr(self_update, "perform_restart",
                            lambda: spawned.append(True) or {"spawned": True})
        monkeypatch.setattr("core.sessions.running", lambda: [])
        monkeypatch.setattr("core.config.get_config",
                            lambda key, default=None: True if key == "self_update.auto_restart_when_idle" else default)

        self_update._restart_pass(start_sha, tmpdir)

        assert spawned == [True]


def test_no_auto_restart_without_config(monkeypatch):
    """auto_restart_when_idle=false (default) + behind + idle → no restart."""
    with tempfile.TemporaryDirectory() as tmpdir:
        start_sha = _init_git_repo(tmpdir)
        _add_commits(tmpdir, 2)

        self_update.reset()
        monkeypatch.setattr(self_update, "perform_restart",
                            lambda: (_ for _ in ()).throw(AssertionError("must not restart")))
        monkeypatch.setattr("core.sessions.running", lambda: [])
        monkeypatch.setattr("core.config.get_config",
                            lambda key, default=None: False if key == "self_update.auto_restart_when_idle" else default)

        self_update._restart_pass(start_sha, tmpdir)


def test_no_auto_restart_when_up_to_date(monkeypatch):
    """auto_restart only applies when behind > 0 — up to date is nothing to do."""
    with tempfile.TemporaryDirectory() as tmpdir:
        start_sha = _init_git_repo(tmpdir)

        self_update.reset()
        monkeypatch.setattr(self_update, "perform_restart",
                            lambda: (_ for _ in ()).throw(AssertionError("must not restart")))
        monkeypatch.setattr("core.sessions.running", lambda: [])
        monkeypatch.setattr("core.config.get_config",
                            lambda key, default=None: True if key == "self_update.auto_restart_when_idle" else default)

        self_update._restart_pass(start_sha, tmpdir)


def test_perform_restart_spawns_detached_process(monkeypatch):
    """perform_restart() spawns the existing restart path as a detached
    subprocess — the process that stops the daemon is never the daemon."""
    popped = {}
    monkeypatch.setattr(self_update.subprocess, "Popen",
                        lambda *a, **k: popped.update(cmd=a[0], kwargs=k))
    monkeypatch.setattr(self_update.sys, "executable", "/venv/bin/python")

    result = self_update.perform_restart()

    assert result["spawned"] is True
    assert popped["cmd"][0] == "/venv/bin/python"
    assert "-c" in popped["cmd"]
    assert popped["kwargs"]["start_new_session"] is True


def test_perform_restart_returns_false_on_failure(monkeypatch):
    """A spawn failure is reported, never raised."""
    monkeypatch.setattr(self_update.subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))

    result = self_update.perform_restart()

    assert result["spawned"] is False


# ── the daemon's own checkout path comparison (reviewer #8) ────────────────

def test_is_daemon_checkout_matches_git_toplevel(monkeypatch):
    """A repo registered at the git toplevel (one level above the source dir)
    is recognised as the daemon's own checkout."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Simulate: src = <tmp>/sisyphus, git root = <tmp>
        src = pathlib.Path(tmpdir) / "sisyphus"
        src.mkdir()
        _git(tmpdir, "git", "init")
        _git(tmpdir, "git", "config", "user.email", "t@t.com")
        _git(tmpdir, "git", "config", "user.name", "T")
        (pathlib.Path(tmpdir) / "f.txt").write_text("x")
        _git(tmpdir, "git", "add", ".")
        _git(tmpdir, "git", "commit", "-m", "i")

        monkeypatch.setattr(self_update, "daemon_repo_root",
                            lambda: pathlib.Path(tmpdir).resolve())
        assert self_update.is_daemon_checkout(tmpdir)
        assert not self_update.is_daemon_checkout("/nonexistent")


# ── the endpoint: pending when busy, HTML fragment, never restarts live ────

def test_endpoint_restart_waits_while_worker_runs(db_path):
    """POST /api/daemon/restart with a worker running → pending flag set, no
    restart triggered, and an HTML fragment (not JSON) comes back."""
    from unittest.mock import patch
    from web.app import app
    from fastapi.testclient import TestClient

    with tempfile.TemporaryDirectory() as tmpdir:
        _init_git_repo(tmpdir)
        _add_commits(tmpdir, 2)

        client = TestClient(app)
        with patch.object(self_update, "get_cached_status",
                          return_value={"behind": 2, "behind_count": 2,
                                        "behind_message": "Running X — main is Y, 2 commits behind"}):
            with patch("core.sessions.running", return_value=[{"task_id": "t1"}]):
                with patch.object(self_update, "perform_restart") as restart:
                    self_update.reset()
                    resp = client.post("/api/daemon/restart")

        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert "Restart scheduled" in resp.text
        assert "will restart when the last worker finishes" in resp.text
        restart.assert_not_called()
        assert self_update.pending_restart()


def test_endpoint_restart_spawns_when_idle(db_path):
    """POST /api/daemon/restart with no worker → the detached restart is
    spawned and the endpoint answers with HTML, not JSON."""
    from unittest.mock import patch
    from web.app import app
    from fastapi.testclient import TestClient

    with tempfile.TemporaryDirectory() as tmpdir:
        _init_git_repo(tmpdir)
        _add_commits(tmpdir, 2)

        client = TestClient(app)
        with patch.object(self_update, "get_cached_status",
                          return_value={"behind": 2, "behind_count": 2,
                                        "behind_message": "Running X — main is Y, 2 commits behind"}):
            with patch("core.sessions.running", return_value=[]):
                with patch.object(self_update, "perform_restart",
                                  return_value={"spawned": True}) as restart:
                    self_update.reset()
                    resp = client.post("/api/daemon/restart")

        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert "Restarting daemon" in resp.text
        restart.assert_called_once()


def test_endpoint_refuses_when_up_to_date(db_path):
    """POST /api/daemon/restart when up to date → no restart, HTML answer."""
    from unittest.mock import patch
    from web.app import app
    from fastapi.testclient import TestClient

    with tempfile.TemporaryDirectory() as tmpdir:
        _init_git_repo(tmpdir)

        client = TestClient(app)
        with patch.object(self_update, "get_cached_status",
                          return_value={"behind": 0, "behind_message": None}):
            with patch("core.sessions.running", return_value=[]):
                with patch.object(self_update, "perform_restart") as restart:
                    self_update.reset()
                    resp = client.post("/api/daemon/restart")

        assert resp.status_code == 200
        assert "up to date" in resp.text
        restart.assert_not_called()
        assert not self_update.pending_restart()
