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
