"""Tests for forge MR safety — preventing local merges on forge-hosted MRs.

Forge-hosted MRs (GitHub/GitLab) must never be merged locally by Sisyphus.
Merging belongs to the forge (its CI, branch protection, human review all live there).
Locally merging a forge MR would land code behind the forge's back.
"""
import subprocess
import threading

import pytest

from core import git_ops, mergerequests, store


def _run(*a):
    return subprocess.run(list(a), capture_output=True, text=True)


def _setup(tmp_path):
    """A real repo on `main`, a bare clone, and a task branch with a commit."""
    real = tmp_path / "real"; real.mkdir()
    for a in (["init", "-b", "main"], ["config", "user.email", "t@t"],
              ["config", "user.name", "T"]):
        _run("git", "-C", str(real), *a)
    (real / "README.md").write_text("hi\n")
    _run("git", "-C", str(real), "add", "-A")
    _run("git", "-C", str(real), "commit", "-m", "init")

    bare = tmp_path / "bare"
    _run("git", "clone", "--bare", str(real), str(bare))
    git_ops.ensure_task_branch(str(bare), "task-x", "main")
    wt = tmp_path / "wt"
    _run("git", "-C", str(bare), "worktree", "add", str(wt), "sisyphus/task-x")
    (wt / "feature.py").write_text("print('x')\n")
    git_ops.commit_all(str(wt), "add feature")

    return str(real), str(bare)


def _local_mr(tmp_path, monkeypatch):
    """An open local MR (no forge_url)."""
    real, bare = _setup(tmp_path)
    task = store.create_task("do it", repo="acme")
    monkeypatch.setattr("core.repos.get_repo",
                        lambda name: {"name": name, "source": "", "platform": "local", "auto_merge": False})
    mr = mergerequests.create_mr(
        task_id=task["id"], repo="acme", source_branch="sisyphus/task-x",
        target_branch="main", bare_path=bare, base_branch="main",
        source_path=real)
    return mr, real


def _github_mr(tmp_path, monkeypatch):
    """An open MR whose repo is on GitHub."""
    real, bare = _setup(tmp_path)
    task = store.create_task("do it", repo="acme")
    monkeypatch.setattr("core.repos.get_repo",
                        lambda name: {"name": name, "source": "https://github.com/user/repo.git",
                                     "platform": "", "auto_merge": False})
    mr = mergerequests.create_mr(
        task_id=task["id"], repo="acme", source_branch="sisyphus/task-x",
        target_branch="main", bare_path=bare, base_branch="main",
        source_path=real)
    return mr, real


def _github_mr_with_forge_url(tmp_path, monkeypatch):
    """An open MR with an explicit forge_url."""
    real, bare = _setup(tmp_path)
    task = store.create_task("do it", repo="acme")
    monkeypatch.setattr("core.repos.get_repo",
                        lambda name: {"name": name, "auto_merge": False})
    mr = mergerequests.create_mr(
        task_id=task["id"], repo="acme", source_branch="sisyphus/task-x",
        target_branch="main", bare_path=bare, base_branch="main",
        source_path=real, forge_url="https://github.com/user/repo/pull/123")
    return mr, real


@pytest.fixture
def no_auto_trigger(monkeypatch):
    """Unit tests drive try_auto_merge directly; keep the record-point triggers
    (set_review_status / set_check_result) from merging during state setup."""
    monkeypatch.setattr(mergerequests, "schedule_auto_merge", lambda *a, **k: None)


class TestForgeMRDetection:
    """The is_forge_mr function correctly identifies forge-hosted MRs."""

    def test_detects_forge_url(self, db_path, tmp_path, monkeypatch):
        mr, _ = _github_mr_with_forge_url(tmp_path, monkeypatch)
        assert mergerequests.is_forge_mr(mr)

    def test_detects_github_platform(self, db_path, tmp_path, monkeypatch):
        mr, _ = _github_mr(tmp_path, monkeypatch)
        assert mergerequests.is_forge_mr(mr)

    def test_local_repo_is_not_forge(self, db_path, tmp_path, monkeypatch):
        mr, _ = _local_mr(tmp_path, monkeypatch)
        assert not mergerequests.is_forge_mr(mr)


class TestAutoMergeRefusesForge:
    """Auto-merge never touches a forge MR, even when approved with a passing check."""

    def test_github_mr_never_auto_merges(self, db_path, tmp_path, monkeypatch, no_auto_trigger):
        mr, real = _github_mr(tmp_path, monkeypatch)
        merge_called = []
        monkeypatch.setattr(mergerequests, "merge_branch",
                            lambda *a, **k: merge_called.append(True) or {"ok": False})

        mergerequests.set_review_status(mr["id"], "approved")
        mergerequests.set_check_result(mr["id"], "passed", command="pytest -q",
                                       head=mergerequests.current_head(mr))

        res = mergerequests.try_auto_merge(mergerequests.get_mr(mr["id"]))

        assert res is None
        assert merge_called == []

    def test_forge_url_mr_never_auto_merges(self, db_path, tmp_path, monkeypatch, no_auto_trigger):
        mr, real = _github_mr_with_forge_url(tmp_path, monkeypatch)
        merge_called = []
        monkeypatch.setattr(mergerequests, "merge_branch",
                            lambda *a, **k: merge_called.append(True) or {"ok": False})

        mergerequests.set_review_status(mr["id"], "approved")
        mergerequests.set_check_result(mr["id"], "passed", command="pytest -q",
                                       head=mergerequests.current_head(mr))

        res = mergerequests.try_auto_merge(mergerequests.get_mr(mr["id"]))

        assert res is None
        assert merge_called == []

    def test_local_mr_still_auto_merges(self, db_path, tmp_path, monkeypatch, no_auto_trigger):
        mr, real = _local_mr(tmp_path, monkeypatch)
        monkeypatch.setattr("core.repos.get_repo",
                            lambda name: {"name": name, "source": "", "platform": "local", "auto_merge": True})

        mergerequests.set_review_status(mr["id"], "approved")
        mergerequests.set_check_result(mr["id"], "passed", command="pytest -q",
                                       head=mergerequests.current_head(mr))

        res = mergerequests.try_auto_merge(mergerequests.get_mr(mr["id"]))

        assert res is not None
        assert mergerequests.get_mr(mr["id"])["status"] == "merged"


class TestConcurrentAutoMergeLocking:
    """Two concurrent auto-merge attempts on the same MR produce exactly one merge_branch call."""

    def test_concurrent_attempts_produce_one_merge(self, db_path, tmp_path, monkeypatch, no_auto_trigger):
        mr, real = _local_mr(tmp_path, monkeypatch)
        monkeypatch.setattr("core.repos.get_repo",
                            lambda name: {"name": name, "source": "", "platform": "local", "auto_merge": True})

        mergerequests.set_review_status(mr["id"], "approved")
        mergerequests.set_check_result(mr["id"], "passed", command="pytest -q",
                                       head=mergerequests.current_head(mr))

        merge_calls = []
        original_merge_branch = mergerequests.merge_branch

        def slow_merge_branch(mr_dict, target):
            """Sleep briefly to simulate concurrent attempts."""
            merge_calls.append(mr_dict["id"])
            import time
            time.sleep(0.1)
            return original_merge_branch(mr_dict, target)

        monkeypatch.setattr(mergerequests, "merge_branch", slow_merge_branch)

        results = [None, None]
        errors = [None, None]

        def attempt(idx):
            try:
                results[idx] = mergerequests.try_auto_merge(mergerequests.get_mr(mr["id"]))
            except Exception as e:
                errors[idx] = e

        # Start two concurrent attempts
        t1 = threading.Thread(target=attempt, args=(0,))
        t2 = threading.Thread(target=attempt, args=(1,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Verify no errors occurred
        assert errors == [None, None]

        # Verify exactly one merge_branch was called
        assert len(merge_calls) == 1

        # Verify MR is merged
        assert mergerequests.get_mr(mr["id"])["status"] == "merged"

        # Verify one result is the sha, the other is None
        results_set = {r for r in results if r is not None}
        assert len(results_set) == 1
