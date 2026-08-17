"""Tests for core/worktree.py — the isolation that keeps agents out of the real repo.

171 lines carrying three incidents its own comments record (a stale worktree
registration that "bit us three times in production", a reviewer re-reading the
previous round's files forever, and set_access silently no-opping on the level
core.worker actually passes) and, until now, no tests at all. These pin the
behaviour those comments describe.
"""
import os
import subprocess
import stat

import pytest

from core import worktree


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


@pytest.fixture
def bare(tmp_path, monkeypatch):
    """A bare clone with `main` and a `feature` branch, plus REPOS_DIR pointed
    at the temp dir. REPOS_DIR is a module-level constant read from the
    environment at import time, so it has to be patched on the module."""
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(src)], capture_output=True)
    _git(src, "config", "user.email", "t@t")
    _git(src, "config", "user.name", "t")
    (src / "file.txt").write_text("one\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-m", "init")
    _git(src, "branch", "feature")

    bare_path = tmp_path / "bare.git"
    subprocess.run(["git", "clone", "--bare", str(src), str(bare_path)], capture_output=True)
    monkeypatch.setattr(worktree, "REPOS_DIR", tmp_path / "repos")
    return bare_path, src


class TestRepoNameFromUrl:
    @pytest.mark.parametrize("url,expected", [
        ("https://github.com/owner/repo.git", "repo"),
        ("https://github.com/owner/repo", "repo"),
        ("git@github.com:owner/repo.git", "repo"),
        ("/local/path/to/myrepo/", "myrepo"),
        ("/local/path/to/myrepo", "myrepo"),
    ])
    def test_extracts_the_name(self, url, expected):
        assert worktree.get_repo_name_from_url(url) == expected

    def test_only_a_trailing_dot_git_is_stripped(self):
        # A repo legitimately named "git" must survive.
        assert worktree.get_repo_name_from_url("/x/git") == "git"


class TestInitBareClone:
    def test_creates_a_bare_clone(self, tmp_path):
        src = tmp_path / "s"
        src.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(src)], capture_output=True)
        _git(src, "config", "user.email", "t@t"); _git(src, "config", "user.name", "t")
        (src / "a").write_text("x"); _git(src, "add", "-A"); _git(src, "commit", "-m", "i")
        out = worktree.init_bare_clone(str(src), str(tmp_path / "b.git"))
        assert os.path.exists(os.path.join(out, "HEAD"))

    def test_is_idempotent(self, tmp_path):
        src = tmp_path / "s"
        src.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(src)], capture_output=True)
        _git(src, "config", "user.email", "t@t"); _git(src, "config", "user.name", "t")
        (src / "a").write_text("x"); _git(src, "add", "-A"); _git(src, "commit", "-m", "i")
        first = worktree.init_bare_clone(str(src), str(tmp_path / "b.git"))
        assert worktree.init_bare_clone(str(src), str(tmp_path / "b.git")) == first

    def test_unreachable_source_raises(self, tmp_path):
        with pytest.raises(RuntimeError):
            worktree.init_bare_clone("/nonexistent/repo", str(tmp_path / "b.git"))


class TestCreateWorktree:
    def test_attached_holds_the_branch(self, bare):
        bare_path, _ = bare
        wt = worktree.create_worktree(str(bare_path), "task-1", "engineer", "feature")
        head = _git(wt, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
        assert head == "feature", "the engineer's commits must advance the branch"

    def test_detached_does_not_hold_the_branch(self, bare):
        """Only one worktree may hold a branch attached. Read-only roles check
        out detached so they cannot block the engineer from committing."""
        bare_path, _ = bare
        wt = worktree.create_worktree(str(bare_path), "task-1", "reviewer", "feature",
                                      detach=True)
        assert _git(wt, "symbolic-ref", "--quiet", "--short", "HEAD").returncode != 0

    def test_engineer_and_reviewer_can_coexist(self, bare):
        bare_path, _ = bare
        worktree.create_worktree(str(bare_path), "task-1", "engineer", "feature")
        wt2 = worktree.create_worktree(str(bare_path), "task-1", "reviewer", "feature",
                                       detach=True)
        assert os.path.isdir(wt2)

    def test_attached_falls_back_to_detached_when_the_branch_is_taken(self, bare):
        """Rather than failing the run outright."""
        bare_path, _ = bare
        worktree.create_worktree(str(bare_path), "task-1", "engineer", "feature")
        wt2 = worktree.create_worktree(str(bare_path), "task-2", "engineer", "feature")
        assert os.path.isdir(wt2)
        assert _git(wt2, "symbolic-ref", "--quiet", "--short", "HEAD").returncode != 0

    def test_a_reused_detached_worktree_is_refreshed(self, bare):
        """A re-spawned reviewer must see the current tip. Left frozen, it would
        re-review the previous round's files forever after CHANGES REQUESTED."""
        bare_path, src = bare
        wt = worktree.create_worktree(str(bare_path), "task-1", "reviewer", "feature",
                                      detach=True)
        assert (open(os.path.join(wt, "file.txt")).read()) == "one\n"

        # Advance `feature` in the bare clone, the way an engineer's commit would.
        eng = worktree.create_worktree(str(bare_path), "task-1", "engineer", "feature")
        with open(os.path.join(eng, "file.txt"), "w") as fh:
            fh.write("two\n")
        _git(eng, "add", "-A")
        _git(eng, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "round two")

        again = worktree.create_worktree(str(bare_path), "task-1", "reviewer", "feature",
                                         detach=True)
        assert again == wt
        assert open(os.path.join(again, "file.txt")).read() == "two\n"

    def test_a_stale_registration_self_heals(self, bare):
        """A worktree directory deleted without `git worktree prune` leaves a
        registration that blocks re-adding the same path — the failure its own
        comment says bit production three times."""
        import shutil
        bare_path, _ = bare
        wt = worktree.create_worktree(str(bare_path), "task-1", "engineer", "feature")
        shutil.rmtree(wt)                       # deliberately not pruned
        again = worktree.create_worktree(str(bare_path), "task-1", "engineer", "feature")
        assert os.path.isdir(again)

    def test_an_unknown_branch_raises_rather_than_returning_a_bad_path(self, bare):
        bare_path, _ = bare
        with pytest.raises(RuntimeError):
            worktree.create_worktree(str(bare_path), "task-1", "engineer", "no-such-branch")


class TestRemoveWorktree:
    def test_removes_and_deregisters(self, bare):
        bare_path, _ = bare
        wt = worktree.create_worktree(str(bare_path), "task-1", "engineer", "feature")
        assert worktree.remove_worktree(str(bare_path), "task-1", "engineer") is True
        assert not os.path.exists(wt)
        assert "task-1_engineer" not in _git(bare_path, "worktree", "list").stdout

    def test_absent_worktree_is_not_an_error(self, bare):
        bare_path, _ = bare
        assert worktree.remove_worktree(str(bare_path), "task-1", "never-made") is True


class TestSetAccess:
    def _tree(self, tmp_path):
        d = tmp_path / "wt"
        (d / "sub").mkdir(parents=True)
        (d / "sub" / "f.txt").write_text("x")
        return d

    def test_write_is_an_alias_for_edit(self, tmp_path):
        """core.worker calls set_access(path, "write"). An unknown level returns
        silently, so without the alias every worktree kept whatever modes it
        happened to have — a no-op that looked like a policy."""
        d = self._tree(tmp_path)
        (d / "sub" / "f.txt").chmod(0o444)
        worktree.set_access(str(d), "write")
        assert os.stat(d / "sub" / "f.txt").st_mode & stat.S_IWUSR

    def test_read_makes_files_read_only(self, tmp_path):
        d = self._tree(tmp_path)
        worktree.set_access(str(d), "read")
        assert not (os.stat(d / "sub" / "f.txt").st_mode & stat.S_IWUSR)

    def test_applies_recursively_including_the_root(self, tmp_path):
        d = self._tree(tmp_path)
        worktree.set_access(str(d), "read")
        assert not (os.stat(d).st_mode & stat.S_IWUSR)
        assert not (os.stat(d / "sub").st_mode & stat.S_IWUSR)

    def test_unknown_level_changes_nothing(self, tmp_path):
        d = self._tree(tmp_path)
        (d / "sub" / "f.txt").chmod(0o600)
        worktree.set_access(str(d), "banana")
        assert os.stat(d / "sub" / "f.txt").st_mode & 0o777 == 0o600

    def test_missing_path_is_not_an_error(self, tmp_path):
        worktree.set_access(str(tmp_path / "nope"), "write")
