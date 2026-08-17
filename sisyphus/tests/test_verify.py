"""Tests for core/verify.py — the daemon's own check on a merge request.

The point of this module is that a review verdict cites a number the system
produced. The tests that matter most are therefore the ones keeping "no check
ran" distinct from "the check passed": conflating those is precisely the failure
recorded in docs/known-gaps.md, where a reviewer approved on the strength of
test failures that did not exist.
"""
import subprocess

import pytest

from core import verify


@pytest.fixture
def repo(tmp_path):
    """A git repo with one commit on a branch, ready to be checked."""
    src = tmp_path / "repo"
    src.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(src)], capture_output=True)
    subprocess.run(["git", "-C", str(src), "config", "user.email", "t@t"], capture_output=True)
    subprocess.run(["git", "-C", str(src), "config", "user.name", "t"], capture_output=True)
    (src / "marker.txt").write_text("hello\n")
    subprocess.run(["git", "-C", str(src), "add", "-A"], capture_output=True)
    subprocess.run(["git", "-C", str(src), "commit", "-m", "init"], capture_output=True)
    subprocess.run(["git", "-C", str(src), "branch", "feature"], capture_output=True)
    return src


def _mr(repo, **over):
    mr = {"id": "mr-1", "task_id": "", "repo": "r",
          "bare_path": str(repo), "source_branch": "feature"}
    mr.update(over)
    return mr


class TestNoCheckIsNotAPass:
    def test_unconfigured_repo_does_not_run(self, repo, monkeypatch):
        monkeypatch.setattr(verify, "check_command", lambda name: None)
        res = verify.run_check(_mr(repo))
        assert res["ran"] is False
        assert res["ok"] is None            # not False, and emphatically not True
        assert "no check command" in res["reason"]

    def test_summary_says_nothing_ran(self, repo, monkeypatch):
        monkeypatch.setattr(verify, "check_command", lambda name: None)
        line = verify.summarise(verify.run_check(_mr(repo)))
        assert "No automated check ran" in line
        assert "PASSED" not in line

    def test_missing_branch_does_not_run(self, repo, monkeypatch):
        monkeypatch.setattr(verify, "check_command", lambda name: "true")
        res = verify.run_check(_mr(repo, source_branch=""))
        assert res["ran"] is False and res["ok"] is None


class TestVerdicts:
    def test_passing_command(self, repo, monkeypatch):
        monkeypatch.setattr(verify, "check_command", lambda name: "test -f marker.txt")
        res = verify.run_check(_mr(repo))
        assert res["ran"] is True and res["ok"] is True and res["exit_code"] == 0
        assert "PASSED" in verify.summarise(res)

    def test_failing_command_reports_its_output(self, repo, monkeypatch):
        monkeypatch.setattr(verify, "check_command",
                            lambda name: "echo '2 failed, 1 passed'; exit 1")
        res = verify.run_check(_mr(repo))
        assert res["ok"] is False and res["exit_code"] == 1
        assert "2 failed" in res["output"]
        assert "FAILED" in verify.summarise(res)

    def test_timeout_is_a_failure_not_a_pass(self, repo, monkeypatch):
        monkeypatch.setattr(verify, "check_command", lambda name: "sleep 30")
        res = verify.run_check(_mr(repo), timeout=1)
        assert res["ran"] is True and res["ok"] is False
        assert "exceeded" in res["output"]

    def test_runs_against_the_branch_not_the_checkout(self, repo, monkeypatch):
        """The check must see the MR's branch. `main` is checked out in the repo,
        so a check that only ever saw the working tree would pass either way."""
        # Attached, so the commit actually advances `feature`.
        subprocess.run(["git", "-C", str(repo), "worktree", "add",
                        str(repo.parent / "tmpwt"), "feature"], capture_output=True)
        wt = repo.parent / "tmpwt"
        (wt / "only_on_feature.txt").write_text("x\n")
        subprocess.run(["git", "-C", str(wt), "add", "-A"], capture_output=True)
        subprocess.run(["git", "-C", str(wt), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-m", "feature only"], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)],
                       capture_output=True)

        monkeypatch.setattr(verify, "check_command", lambda name: "test -f only_on_feature.txt")
        assert verify.run_check(_mr(repo))["ok"] is True

    def test_leaves_no_worktree_behind(self, repo, monkeypatch):
        monkeypatch.setattr(verify, "check_command", lambda name: "true")
        verify.run_check(_mr(repo))
        listed = subprocess.run(["git", "-C", str(repo), "worktree", "list"],
                                capture_output=True, text=True).stdout
        assert "sisyphus-check-" not in listed


class TestRecording:
    def test_result_is_stored_on_the_mr(self, repo, db_path, monkeypatch):
        from core import mergerequests
        mergerequests.init_mr_table()
        mr = mergerequests.create_mr(task_id="t1", repo="r", source_branch="feature",
                                     target_branch="main", title="", bare_path=str(repo),
                                     base_branch="main", source_path=str(repo))
        monkeypatch.setattr(verify, "check_command", lambda name: "exit 3")
        verify.run_and_record(mr)
        row = mergerequests.get_mr(mr["id"])
        assert row["check_status"] == "failed"
        assert row["check_command"] == "exit 3"
        assert row["check_ran_at"]

    def test_unconfigured_repo_records_not_run(self, repo, db_path, monkeypatch):
        from core import mergerequests
        mergerequests.init_mr_table()
        mr = mergerequests.create_mr(task_id="t1", repo="r", source_branch="feature",
                                     target_branch="main", title="", bare_path=str(repo),
                                     base_branch="main", source_path=str(repo))
        monkeypatch.setattr(verify, "check_command", lambda name: None)
        verify.run_and_record(mr)
        assert mergerequests.get_mr(mr["id"])["check_status"] == "not_run"
