"""Unit tests for pure worktree utility functions (no git required)."""
import os
import stat

import pytest

from core.worktree import get_repo_name_from_url, set_access


class TestGetRepoNameFromUrl:
    def test_https_url_with_dot_git(self):
        assert get_repo_name_from_url("https://github.com/user/my-repo.git") == "my-repo"

    def test_https_url_without_dot_git(self):
        assert get_repo_name_from_url("https://github.com/user/my-repo") == "my-repo"

    def test_ssh_url(self):
        assert get_repo_name_from_url("git@github.com:user/my-repo.git") == "my-repo"

    def test_trailing_slash(self):
        assert get_repo_name_from_url("https://github.com/user/repo.git/") == "repo"

    def test_simple_name(self):
        assert get_repo_name_from_url("repo") == "repo"

    def test_deep_path(self):
        assert get_repo_name_from_url("https://gitlab.com/group/subgroup/project.git") == "project"

    def test_empty_string(self):
        assert get_repo_name_from_url("") == ""


class TestSetAccess:
    def test_read_level(self, tmp_path):
        path = tmp_path / "read-dir"
        path.mkdir()
        set_access(str(path), "read")
        perms = stat.S_IMODE(os.stat(str(path)).st_mode)
        assert perms == 0o555

    def test_analyze_level(self, tmp_path):
        path = tmp_path / "analyze-dir"
        path.mkdir()
        set_access(str(path), "analyze")
        perms = stat.S_IMODE(os.stat(str(path)).st_mode)
        assert perms == 0o755

    def test_edit_level(self, tmp_path):
        path = tmp_path / "edit-dir"
        path.mkdir()
        set_access(str(path), "edit")
        perms = stat.S_IMODE(os.stat(str(path)).st_mode)
        assert perms == 0o755

    def test_unknown_level_does_nothing(self, tmp_path):
        path = tmp_path / "unknown-dir"
        path.mkdir()
        before = os.stat(str(path)).st_mode
        set_access(str(path), "nonexistent")
        after = os.stat(str(path)).st_mode
        assert before == after

    def test_nonexistent_path_does_nothing(self, tmp_path):
        path = tmp_path / "does-not-exist"
        # Should not raise
        set_access(str(path), "read")


class TestStaleRegistrationSelfHeal:
    """create_worktree must survive a worktree dir that was removed without
    `git worktree prune` (stale registration) — the recurring production bug."""

    def _make_bare(self, tmp_path):
        import subprocess
        src = tmp_path / "src"
        src.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=src, check=True, capture_output=True)
        (src / "f.txt").write_text("hi")
        subprocess.run(["git", "add", "."], cwd=src, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-m", "init"], cwd=src, check=True, capture_output=True)
        bare = tmp_path / "bare.git"
        subprocess.run(["git", "clone", "--bare", str(src), str(bare)],
                       check=True, capture_output=True)
        return str(bare)

    def test_readd_after_dir_removed_without_prune(self, tmp_path, monkeypatch):
        import shutil
        from core import worktree as wt

        monkeypatch.setattr(wt, "REPOS_DIR", tmp_path / "repos")
        bare = self._make_bare(tmp_path)
        path = wt.create_worktree(bare, "task-x", "engineer", branch="main")
        shutil.rmtree(path)                      # removed WITHOUT prune
        path2 = wt.create_worktree(bare, "task-x", "engineer", branch="main")
        assert os.path.isdir(path2)
