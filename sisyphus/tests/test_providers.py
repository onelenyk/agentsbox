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


class TestRepoSlug:
    def test_https(self):
        assert providers._repo_slug("https://github.com/acme/widgets.git") == "acme/widgets"

    def test_ssh(self):
        assert providers._repo_slug("git@github.com:acme/widgets.git") == "acme/widgets"

    def test_nested_gitlab(self):
        assert providers._repo_slug("https://gitlab.com/grp/sub/proj.git") == "grp/sub/proj"


class TestResolveForgeUrl:
    def test_url_source_unchanged(self):
        assert providers._resolve_forge_url("https://github.com/acme/widgets.git") == \
            "https://github.com/acme/widgets.git"
        assert providers._resolve_forge_url("git@github.com:acme/widgets.git") == \
            "git@github.com:acme/widgets.git"

    def test_path_source_returns_origin(self):
        remote = "git@sln-gtl.vanongo.com:sln-apps/android-driver.git"
        with patch("subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, remote + "\n", "")):
            assert providers._resolve_forge_url("/Users/me/android-driver") == remote

    def test_path_source_no_origin_returns_none(self):
        with patch("subprocess.run",
                   return_value=subprocess.CompletedProcess([], 1, "", "no origin")):
            assert providers._resolve_forge_url("/Users/me/android-driver") is None


class TestForgeProviders:
    def test_gitlab_pushes_and_opens_mr(self):
        with patch("core.config.get_config", return_value={"token": "t", "base_url": "https://gl.example.com"}), \
             patch("core.providers._push") as push, \
             patch("core.providers._post_json", return_value={"web_url": "https://gl/mr/1"}) as post:
            r = providers._publish_gitlab("/bare", "https://gl.example.com/grp/proj.git",
                                          "sisyphus/task-1", "main", "Title", "Desc")
        assert r["ok"] and r["platform"] == "gitlab" and r["url"] == "https://gl/mr/1"
        push.assert_called_once()
        # project path is URL-encoded (slash → %2F)
        assert "grp%2Fproj/merge_requests" in post.call_args.args[0]
        assert post.call_args.args[2]["source_branch"] == "sisyphus/task-1"

    def test_github_pushes_and_opens_pr(self):
        with patch("core.config.get_config", return_value={"token": "t"}), \
             patch("core.providers._push") as push, \
             patch("core.providers._post_json", return_value={"html_url": "https://gh/pr/1"}) as post:
            r = providers._publish_github("/bare", "https://github.com/acme/widgets.git",
                                          "sisyphus/task-1", "main", "Title", "Desc")
        assert r["ok"] and r["platform"] == "github" and r["url"] == "https://gh/pr/1"
        push.assert_called_once()
        assert post.call_args.args[0].endswith("/repos/acme/widgets/pulls")
        assert post.call_args.args[2] == {"head": "sisyphus/task-1", "base": "main", "title": "Title", "body": "Desc"}

    def test_missing_token_errors(self):
        with patch("core.config.get_config", return_value={"token": "${GITHUB_TOKEN}"}):
            r = providers._publish_github("/bare", "https://github.com/a/b.git", "sisyphus/x", "main", "T", "D")
        assert "error" in r and "token" in r["error"].lower()

    def test_http_error_surfaced(self):
        import io
        import urllib.error
        err = urllib.error.HTTPError("u", 422, "Unprocessable", {}, io.BytesIO(b'{"message":"exists"}'))
        with patch("core.config.get_config", return_value={"token": "t"}), \
             patch("core.providers._push"), \
             patch("core.providers._post_json", return_value=err):
            r = providers._publish_github("/bare", "https://github.com/a/b.git", "sisyphus/x", "main", "T", "D")
        assert "error" in r and "422" in r["error"]

    def test_gitlab_path_source_resolves_origin_and_pushes(self):
        remote = "git@sln-gtl.vanongo.com:sln-apps/android-driver.git"
        source = "/path/to/android-driver"

        def fake_run(cmd, *a, **kw):
            if "get-url" in cmd:
                return subprocess.CompletedProcess(cmd, 0, remote + "\n", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch("core.config.get_config", return_value={"token": "t", "base_url": "https://gl.example.com"}), \
             patch("subprocess.run", side_effect=fake_run) as run, \
             patch("core.providers._post_json", return_value={"web_url": "https://gl/mr/1"}) as post:
            r = providers._publish_gitlab("/bare", source, "sisyphus/task-1", "main", "T", "D")

        assert r["ok"] and r["platform"] == "gitlab"
        assert "sln-apps%2Fandroid-driver/merge_requests" in post.call_args.args[0]
        push_calls = [c for c in run.call_args_list if "push" in c.args[0]]
        assert push_calls, "expected a git push"
        push_cmd = push_calls[0].args[0]
        assert remote in push_cmd
        assert push_cmd[-1] == "sisyphus/task-1:sisyphus/task-1"

    def test_gitlab_path_source_no_origin_errors(self):
        with patch("core.config.get_config", return_value={"token": "t"}), \
             patch("subprocess.run",
                   return_value=subprocess.CompletedProcess([], 1, "", "No such remote 'origin'")):
            r = providers._publish_gitlab("/bare", "/some/path", "sisyphus/x", "main", "T", "D")
        assert "error" in r
        assert "no origin remote" in r["error"]

    def test_publish_change_routes_by_platform(self, db_path):
        task = store.create_task("x", repo="r")
        session = {"task_id": task["id"], "role": "engineer", "worktree": ""}
        with patch("core.repos.get_repo", return_value={"source": "https://github.com/a/b.git", "branch": "main"}), \
             patch("core.providers._publish_github", return_value={"ok": True, "platform": "github"}) as gh:
            r = providers.publish_change(session, "t", "d")
        assert r["platform"] == "github"
        gh.assert_called_once()
