"""Tests for core/git_ops.py — real git against throwaway repos in tmp."""
import subprocess

import pytest

from core import git_ops


def _git(args, cwd):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


@pytest.fixture
def repo(tmp_path):
    """A source repo with one commit + a bare clone of it. Returns the bare path."""
    src = tmp_path / "src"
    src.mkdir()
    _git(["init", "-b", "main"], src)
    _git(["config", "user.email", "t@t"], src)
    _git(["config", "user.name", "T"], src)
    (src / "README.md").write_text("hello\n")
    _git(["add", "-A"], src)
    _git(["commit", "-m", "init"], src)
    bare = tmp_path / "bare.git"
    _git(["clone", "--bare", str(src), str(bare)], tmp_path)
    return bare


def _attached_worktree(tmp_path, bare, branch):
    wt = tmp_path / "wt"
    _git(["-C", str(bare), "worktree", "add", str(wt), branch], tmp_path)
    return wt


def test_ensure_task_branch_creates_off_base_and_is_idempotent(repo):
    b1 = git_ops.ensure_task_branch(str(repo), "task-abc", "main")
    assert b1 == "sisyphus/task-abc"
    # exists in the bare now
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify",
                        "refs/heads/sisyphus/task-abc"], capture_output=True, text=True)
    assert r.returncode == 0
    # second call is a no-op, same name
    assert git_ops.ensure_task_branch(str(repo), "task-abc", "main") == b1


def test_commit_all_advances_the_task_branch(repo, tmp_path):
    branch = git_ops.ensure_task_branch(str(repo), "task-1", "main")
    wt = _attached_worktree(tmp_path, repo, branch)
    (wt / "feature.py").write_text("print('hi')\n")

    sha = git_ops.commit_all(str(wt), "add feature")
    assert sha and len(sha) == 40

    patch = git_ops.diff_patch(str(repo), "main", branch)
    assert "feature.py" in patch
    assert "+print('hi')" in patch
    stat = git_ops.diff_stat(str(repo), "main", branch)
    assert "feature.py" in stat


def test_commit_all_returns_none_when_nothing_to_commit(repo, tmp_path):
    branch = git_ops.ensure_task_branch(str(repo), "task-2", "main")
    wt = _attached_worktree(tmp_path, repo, branch)
    assert git_ops.commit_all(str(wt), "noop") is None


def test_commit_all_excludes_daemon_files(repo, tmp_path):
    branch = git_ops.ensure_task_branch(str(repo), "task-3", "main")
    wt = _attached_worktree(tmp_path, repo, branch)
    (wt / "real.py").write_text("x = 1\n")
    (wt / "opencode.json").write_text("{}")
    (wt / ".opencode").mkdir()
    (wt / ".opencode" / "state.json").write_text("{}")

    sha = git_ops.commit_all(str(wt), "work")
    assert sha is not None
    patch = git_ops.diff_patch(str(repo), "main", branch)
    assert "real.py" in patch
    assert "opencode.json" not in patch
    assert ".opencode" not in patch


def test_commit_all_excludes_build_junk(repo, tmp_path):
    """An agent that runs the code leaves __pycache__/*.pyc — keep it out of the
    commit even when the target repo has no .gitignore."""
    branch = git_ops.ensure_task_branch(str(repo), "task-junk", "main")
    wt = _attached_worktree(tmp_path, repo, branch)
    (wt / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (wt / "__pycache__").mkdir()
    (wt / "__pycache__" / "calc.cpython-314.pyc").write_bytes(b"\x00\x01junk")
    (wt / ".DS_Store").write_bytes(b"\x00")

    git_ops.commit_all(str(wt), "work")
    patch = git_ops.diff_patch(str(repo), "main", branch)
    assert "calc.py" in patch
    assert "__pycache__" not in patch
    assert ".pyc" not in patch
    assert ".DS_Store" not in patch
