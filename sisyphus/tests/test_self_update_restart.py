"""Self-update restart logic tests — verify safe restart when workers are idle."""
import tempfile

from core import daemon_ctl, self_update


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


def test_self_update_detects_behind_status(monkeypatch):
    """Self-update correctly identifies when code is behind."""
    with tempfile.TemporaryDirectory() as tmpdir:
        import subprocess
        import pathlib

        # Init a git repo with one commit
        path = pathlib.Path(tmpdir)
        subprocess.run(["git", "init"], cwd=str(path), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"],
                       cwd=str(path), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test"],
                       cwd=str(path), capture_output=True, check=True)
        (path / "file.txt").write_text("initial")
        subprocess.run(["git", "add", "."], cwd=str(path), capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "initial"],
                       cwd=str(path), capture_output=True, check=True)

        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                           cwd=str(path), capture_output=True, text=True, check=True)
        start_sha = r.stdout.strip()

        # Add more commits
        for i in range(2):
            (path / f"file{i}.txt").write_text(f"content {i}")
            subprocess.run(["git", "add", "."], cwd=str(path), capture_output=True, check=True)
            subprocess.run(["git", "commit", "-m", f"commit {i}"],
                           cwd=str(path), capture_output=True, check=True)

        self_update.clear_cache()
        result = self_update.check_behind(start_sha, str(path))

        assert result["behind"] == 2
        assert result["behind_count"] == 2
        assert "2 commits behind" in result.get("behind_message", "")


def test_self_update_shows_nothing_when_up_to_date(monkeypatch):
    """Self-update shows nothing when daemon code is current."""
    with tempfile.TemporaryDirectory() as tmpdir:
        import subprocess
        import pathlib

        # Init a git repo
        path = pathlib.Path(tmpdir)
        subprocess.run(["git", "init"], cwd=str(path), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"],
                       cwd=str(path), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test"],
                       cwd=str(path), capture_output=True, check=True)
        (path / "file.txt").write_text("initial")
        subprocess.run(["git", "add", "."], cwd=str(path), capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "initial"],
                       cwd=str(path), capture_output=True, check=True)

        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                           cwd=str(path), capture_output=True, text=True, check=True)
        sha = r.stdout.strip()

        self_update.clear_cache()
        result = self_update.check_behind(sha, str(path))

        assert result["behind"] == 0
        assert result["message"] == "up to date"
        assert result["behind_message"] is None
