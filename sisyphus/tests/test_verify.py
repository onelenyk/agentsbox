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

    def test_the_verdict_records_which_commit_it_ran_against(self, repo, db_path,
                                                             monkeypatch):
        """A verdict without a commit can be stretched to cover work it never
        saw. get_mr_diff reads this back to tell the reviewer whether the
        recorded check still applies to the diff in front of it."""
        import subprocess as sp
        from core import mergerequests
        mergerequests.init_mr_table()
        mr = mergerequests.create_mr(task_id="t1", repo="r", source_branch="feature",
                                     target_branch="main", title="", bare_path=str(repo),
                                     base_branch="main", source_path=str(repo))
        monkeypatch.setattr(verify, "check_command", lambda name: "true")
        verify.run_and_record(mr)

        head = sp.run(["git", "-C", str(repo), "rev-parse", "feature"],
                      capture_output=True, text=True).stdout.strip()
        assert mergerequests.get_mr(mr["id"])["check_head"] == head

    def test_unconfigured_repo_records_not_run(self, repo, db_path, monkeypatch):
        from core import mergerequests
        mergerequests.init_mr_table()
        mr = mergerequests.create_mr(task_id="t1", repo="r", source_branch="feature",
                                     target_branch="main", title="", bare_path=str(repo),
                                     base_branch="main", source_path=str(repo))
        monkeypatch.setattr(verify, "check_command", lambda name: None)
        verify.run_and_record(mr)
        assert mergerequests.get_mr(mr["id"])["check_status"] == "not_run"


class TestDiffBaseline:
    """Agents that ran their own git reached for HEAD~1 and missed changes made
    earlier on the same branch. The baseline is now computed and handed over,
    so there is nothing left for them to derive."""

    def _branchy_repo(self, tmp_path):
        import subprocess as sp
        src = tmp_path / "b"; src.mkdir()
        run = lambda *a, cwd=str(src): sp.run(["git", "-C", cwd, *a], capture_output=True, text=True)
        sp.run(["git", "init", "-b", "main", str(src)], capture_output=True)
        run("config", "user.email", "t@t"); run("config", "user.name", "t")
        (src / "a.txt").write_text("1\n"); run("add", "-A"); run("commit", "-m", "base")
        base = run("rev-parse", "HEAD").stdout.strip()
        run("checkout", "-b", "feature")
        (src / "a.txt").write_text("2\n"); run("add", "-A"); run("commit", "-m", "first")
        (src / "b.txt").write_text("3\n"); run("add", "-A"); run("commit", "-m", "second")
        head = run("rev-parse", "HEAD").stdout.strip()
        prev = run("rev-parse", "HEAD~1").stdout.strip()
        return src, base, head, prev

    def test_base_is_the_merge_base_not_the_previous_commit(self, tmp_path):
        from core import mergerequests
        src, base, head, prev = self._branchy_repo(tmp_path)
        rng = mergerequests.diff_range({"bare_path": str(src), "target_branch": "main",
                                        "source_branch": "feature"})
        assert rng["base"] == base
        assert rng["head"] == head
        assert rng["base"] != prev, "HEAD~1 would hide the branch's first commit"

    def test_unresolvable_returns_blank_rather_than_a_plausible_wrong_value(self):
        from core import mergerequests
        assert mergerequests.diff_range({}) == {"base": "", "head": ""}
        rng = mergerequests.diff_range({"bare_path": "/nonexistent",
                                        "target_branch": "main", "source_branch": "x"})
        assert rng["base"] == "" and rng["head"] == ""
