"""Tests for the local MR git actions — mergeability, land, merge — on real
temp repos, plus a route smoke test."""
import subprocess

from fastapi.testclient import TestClient
from web.app import app

from core import git_ops, mergerequests, store


def _run(*a):
    return subprocess.run(list(a), capture_output=True, text=True)


def _setup(tmp_path):
    real = tmp_path / "real"; real.mkdir()
    for a in (["init", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "T"]):
        _run("git", "-C", str(real), *a)
    (real / "README.md").write_text("hi\n")
    _run("git", "-C", str(real), "add", "-A")
    _run("git", "-C", str(real), "commit", "-m", "init")
    _run("git", "-C", str(real), "branch", "develop")     # a second target

    bare = tmp_path / "bare"
    _run("git", "clone", "--bare", str(real), str(bare))
    git_ops.ensure_task_branch(str(bare), "task-x", "main")
    wt = tmp_path / "wt"
    _run("git", "-C", str(bare), "worktree", "add", str(wt), "sisyphus/task-x")
    (wt / "feature.py").write_text("print('x')\n")
    git_ops.commit_all(str(wt), "add feature")
    return str(real), str(bare)


def _mr(db_path, tmp_path, target="main"):
    real, bare = _setup(tmp_path)
    task = store.create_task("do it", repo="myrepo")
    from core import attachments
    attachments.save_text(task["id"], "change.patch",
                          git_ops.diff_patch(bare, "main", "sisyphus/task-x"))
    return mergerequests.create_mr(
        task_id=task["id"], repo="myrepo", source_branch="sisyphus/task-x",
        target_branch=target, bare_path=bare, base_branch="main", source_path=real), real, bare


class TestGitActions:
    def test_list_branches(self, db_path, tmp_path):
        mr, real, bare = _mr(db_path, tmp_path)
        assert set(mergerequests.list_branches(real)) >= {"main", "develop"}

    def test_mergeability_clean(self, db_path, tmp_path):
        mr, real, bare = _mr(db_path, tmp_path)
        res = mergerequests.check_mergeability(bare, real, "sisyphus/task-x", "main")
        assert res["ok"] and res["mergeable"] and res["conflicts"] == []

    def test_land_adds_branch_to_real_repo(self, db_path, tmp_path):
        mr, real, bare = _mr(db_path, tmp_path)
        assert mergerequests.land_branch(mr)["ok"]
        branches = _run("git", "-C", real, "branch", "--list", "sisyphus/task-x").stdout
        assert "sisyphus/task-x" in branches

    def test_merge_into_non_current_branch_via_worktree(self, db_path, tmp_path):
        # real repo is on 'main'; merge into 'develop' (not checked out)
        mr, real, bare = _mr(db_path, tmp_path, target="develop")
        res = mergerequests.merge_branch(mr, "develop")
        assert res["ok"], res
        # develop now contains the feature; main working tree untouched
        assert "feature.py" in _run("git", "-C", real, "ls-tree", "-r", "--name-only", "develop").stdout
        assert not (tmp_path / "real" / "feature.py").exists()   # main's tree untouched

    def test_merge_into_current_clean_branch(self, db_path, tmp_path):
        mr, real, bare = _mr(db_path, tmp_path, target="main")
        res = mergerequests.merge_branch(mr, "main")
        assert res["ok"]
        assert (tmp_path / "real" / "feature.py").exists()   # merged into checked-out main


class TestRoutes:
    def test_mr_view_renders(self, db_path, tmp_path):
        mr, real, bare = _mr(db_path, tmp_path)
        r = TestClient(app).get(f"/api/mr/{mr['id']}/view")
        assert r.status_code == 200
        assert "Merge request" in r.text
        assert "sisyphus/task-x" in r.text and "develop" in r.text   # branch options

    def test_mr_merge_route_marks_merged(self, db_path, tmp_path):
        mr, real, bare = _mr(db_path, tmp_path, target="develop")
        r = TestClient(app).post(f"/api/mr/{mr['id']}/merge")
        assert r.status_code == 200
        assert mergerequests.get_mr(mr["id"])["status"] == "merged"

    def test_mr_close_route(self, db_path, tmp_path):
        mr, real, bare = _mr(db_path, tmp_path)
        TestClient(app).post(f"/api/mr/{mr['id']}/close")
        assert mergerequests.get_mr(mr["id"])["status"] == "closed"


class TestBarePreservedForOpenMR:
    def test_cleanup_keeps_bare_while_mr_open_then_reclaims(self, db_path, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        from core.worker import cleanup_task
        task = store.create_task("t", repo="r")
        bare = tmp_path / "repos" / f"{task['id']}_r"
        bare.mkdir(parents=True)
        m = mergerequests.create_mr(task_id=task["id"], repo="r", source_branch="s",
                                    target_branch="main", bare_path=str(bare))
        cleanup_task(task["id"])            # open MR → bare must survive
        assert bare.exists()
        mergerequests.set_status(m["id"], "closed")
        cleanup_task(task["id"])            # resolved → bare reclaimed
        assert not bare.exists()
