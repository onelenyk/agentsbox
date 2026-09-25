"""Self-update detection tests — compare daemon startup commit to current HEAD."""
import tempfile
import subprocess
import pathlib

from core import self_update


def _init_git_repo(path: str, initial_files=None) -> str:
    """Initialize a git repo with optional initial commits. Returns HEAD short SHA."""
    path = pathlib.Path(path)
    path.mkdir(parents=True, exist_ok=True)

    # Init and configure
    subprocess.run(["git", "init"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"],
                   cwd=str(path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), capture_output=True, check=True)

    # Create initial commit if requested
    if initial_files:
        for name, content in initial_files.items():
            (path / name).write_text(content)
        subprocess.run(["git", "add", "."], cwd=str(path), capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=str(path),
                       capture_output=True, check=True)

    # Get HEAD short SHA
    r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(path),
                       capture_output=True, text=True, check=True)
    return r.stdout.strip()


def _add_commits(path: str, count: int) -> list:
    """Add N commits and return list of short SHAs."""
    path = pathlib.Path(path)
    shas = []
    for i in range(count):
        (path / f"file_{i}.txt").write_text(f"content {i}")
        subprocess.run(["git", "add", "."], cwd=str(path), capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", f"commit {i}"],
                       cwd=str(path), capture_output=True, check=True)
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(path),
                           capture_output=True, text=True, check=True)
        shas.append(r.stdout.strip())
    return shas


def test_check_behind_up_to_date():
    """Same commit → behind=0, no message."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sha = _init_git_repo(tmpdir, {"file.txt": "initial"})
        self_update.clear_cache()
        result = self_update.check_behind(sha, tmpdir)
        assert result["behind"] == 0
        assert result["message"] == "up to date"
        assert result["behind_message"] is None


def test_check_behind_has_new_commits():
    """Start commit is an ancestor → behind=N, message shows count."""
    with tempfile.TemporaryDirectory() as tmpdir:
        start_sha = _init_git_repo(tmpdir, {"file.txt": "initial"})
        new_shas = _add_commits(tmpdir, 3)

        self_update.clear_cache()
        result = self_update.check_behind(start_sha, tmpdir)

        assert result["behind"] == 3
        assert result["behind_count"] == 3
        assert result["current_commit"] == new_shas[-1]
        assert result["branch"] == "master" or result["branch"] == "main"
        assert "3 commits behind" in result["behind_message"]
        assert start_sha in result["behind_message"]
        assert new_shas[-1] in result["behind_message"]


def test_check_behind_unknown_start_commit():
    """Unknown start commit → behind=None, no message."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _init_git_repo(tmpdir, {"file.txt": "initial"})
        self_update.clear_cache()
        result = self_update.check_behind("badbadbad", tmpdir)
        assert result["behind"] is None
        assert result["message"] is None


def test_check_behind_not_a_git_repo():
    """Not a git repo → behind=None, no message."""
    with tempfile.TemporaryDirectory() as tmpdir:
        self_update.clear_cache()
        result = self_update.check_behind("abc123", tmpdir)
        assert result["behind"] is None
        assert result["message"] is None


def test_check_behind_missing_paths():
    """Empty start commit or checkout root → behind=None."""
    self_update.clear_cache()
    result = self_update.check_behind("", "/nonexistent")
    assert result["behind"] is None

    self_update.clear_cache()
    result = self_update.check_behind("abc123", "")
    assert result["behind"] is None


def test_get_cached_status_refreshes_after_timeout(monkeypatch):
    """Cached result is reused within CACHE_SECONDS, then refreshed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        start_sha = _init_git_repo(tmpdir, {"file.txt": "initial"})
        self_update.clear_cache()

        # First call — caches
        result1 = self_update.get_cached_status(start_sha, tmpdir)
        assert result1["behind"] == 0

        # Add commits (would change the real result)
        _add_commits(tmpdir, 2)

        # Second call within timeout — returns cached old result
        result2 = self_update.get_cached_status(start_sha, tmpdir)
        assert result2["behind"] == 0  # Still cached

        # Simulate cache expiry
        monkeypatch.setattr(self_update, "_cached_at", None)

        # Third call — cache expired, fetches new status
        result3 = self_update.get_cached_status(start_sha, tmpdir)
        assert result3["behind"] == 2  # Now reflects new commits


def test_check_behind_counts_correctly_with_branch():
    """Check that the branch name is correctly detected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        start_sha = _init_git_repo(tmpdir, {"file.txt": "initial"})

        # Create main branch if not default
        subprocess.run(["git", "checkout", "-b", "main"], cwd=str(tmpdir),
                       capture_output=True)

        # Add commits on main
        _add_commits(tmpdir, 2)

        self_update.clear_cache()
        result = self_update.check_behind(start_sha, tmpdir)

        assert result["behind"] == 2
        assert "main" in result["branch"]
        assert "2 commits behind" in result["behind_message"]
