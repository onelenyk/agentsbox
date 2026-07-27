"""Tests for core/providers.py — platform detection + the local diff publisher."""
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from core import git_ops, providers, store


def _git(args, cwd):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


class TestDetectPlatform:
    def test_local_path(self):
        assert providers.detect_platform("/Users/me/proj") == "local"

    def test_github(self):
        assert providers.detect_platform("https://github.com/acme/widgets.git") == "github"
        assert providers.detect_platform("git@github.com:acme/widgets.git") == "github"

    def test_gitlab(self):
        assert providers.detect_platform("https://gitlab.com/acme/widgets.git") == "gitlab"

    def test_unknown_remote(self):
        assert providers.detect_platform("https://example.com/x.git") == "unknown"


class TestPublishLocal:
    def test_local_publish_writes_patch_and_stat(self, db_path, tmp_path):
        ws = os.environ["WORKSPACE_ROOT"]
        source = str(tmp_path / "src")

        # source repo with a base commit
        Path(source).mkdir()
        _git(["init", "-b", "main"], source)
        _git(["config", "user.email", "t@t"], source)
        _git(["config", "user.name", "T"], source)
        (Path(source) / "README.md").write_text("hi\n")
        _git(["add", "-A"], source)
        _git(["commit", "-m", "init"], source)

        # a task, and a bare clone at the exact path providers._bare_path expects
        task = store.create_task("do it", repo="myrepo")
        bare = providers._bare_path(task["id"], source)
        Path(bare).parent.mkdir(parents=True, exist_ok=True)
        _git(["clone", "--bare", source, bare], tmp_path)

        # a real change committed on the task branch
        branch = git_ops.ensure_task_branch(bare, task["id"], "main")
        wt = tmp_path / "wt"
        _git(["-C", bare, "worktree", "add", str(wt), branch], tmp_path)
        (wt / "feature.py").write_text("print('x')\n")
        git_ops.commit_all(str(wt), "add feature")

        session = {"task_id": task["id"], "role": "engineer", "worktree": str(wt)}
        with patch("core.repos.get_repo", return_value={"source": source, "branch": "main"}):
            result = providers.publish_change(session, "My change", "does a thing")

        assert result["ok"] is True
        assert result["platform"] == "local"
        assert result["branch"] == branch
        assert "feature.py" in result["stat"]
        patch_path = Path(ws) / "tasks" / task["id"] / "attachments" / "change.patch"
        assert patch_path.exists()
        assert "feature.py" in patch_path.read_text()

    def test_local_publish_with_no_changes_errors(self, db_path, tmp_path):
        source = str(tmp_path / "src")
        Path(source).mkdir()
        _git(["init", "-b", "main"], source)
        _git(["config", "user.email", "t@t"], source)
        _git(["config", "user.name", "T"], source)
        (Path(source) / "README.md").write_text("hi\n")
        _git(["add", "-A"], source)
        _git(["commit", "-m", "init"], source)

        task = store.create_task("do it", repo="myrepo")
        bare = providers._bare_path(task["id"], source)
        Path(bare).parent.mkdir(parents=True, exist_ok=True)
        _git(["clone", "--bare", source, bare], tmp_path)
        git_ops.ensure_task_branch(bare, task["id"], "main")  # branch, but no commits on it

        session = {"task_id": task["id"], "role": "engineer", "worktree": ""}
        with patch("core.repos.get_repo", return_value={"source": source, "branch": "main"}):
            result = providers.publish_change(session, "empty", "")
        assert "error" in result
