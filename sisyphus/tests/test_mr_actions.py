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

    def test_merge_drops_the_now_merged_source_branch(self, db_path, tmp_path):
        """Merged sisyphus/task-* branches piled up in the user's repo and the
        morning mr-reviewer re-reviewed every one of them daily."""
        mr, real, bare = _mr(db_path, tmp_path, target="main")
        mergerequests.land_branch(mr)     # the branch exists in the real repo
        assert mergerequests.merge_branch(mr, "main")["ok"]
        branches = _run("git", "-C", real, "branch", "--list", "sisyphus/task-x").stdout
        assert "sisyphus/task-x" not in branches

    def test_merge_via_worktree_also_drops_the_source_branch(self, db_path, tmp_path):
        mr, real, bare = _mr(db_path, tmp_path, target="develop")
        assert mergerequests.merge_branch(mr, "develop")["ok"]
        branches = _run("git", "-C", real, "branch", "--list", "sisyphus/task-x").stdout
        assert "sisyphus/task-x" not in branches

    def test_an_unmerged_branch_is_never_deleted(self, db_path, tmp_path):
        """-d is the whole guard: a branch whose work the target does not
        contain must survive any stray call."""
        mr, real, bare = _mr(db_path, tmp_path, target="main")
        mergerequests.land_branch(mr)     # branch exists, but nothing merged it
        mergerequests._drop_merged_source(real, "sisyphus/task-x", "main")
        branches = _run("git", "-C", real, "branch", "--list", "sisyphus/task-x").stdout
        assert "sisyphus/task-x" in branches


class TestRoutes:
    def test_mr_view_renders(self, db_path, tmp_path):
        mr, real, bare = _mr(db_path, tmp_path)
        r = TestClient(app).get(f"/api/mr/{mr['id']}/view")
        assert r.status_code == 200
        assert "Merge request" in r.text
        assert "sisyphus/task-x" in r.text and "develop" in r.text   # branch options

    def test_mr_merge_route_marks_merged(self, db_path, tmp_path):
        mr, real, bare = _mr(db_path, tmp_path, target="develop")
        mergerequests.set_review_status(mr["id"], "approved")
        r = TestClient(app).post(f"/api/mr/{mr['id']}/merge")
        assert r.status_code == 200
        assert mergerequests.get_mr(mr["id"])["status"] == "merged"

    def test_the_merge_note_cites_what_it_rested_on(self, db_path, tmp_path):
        """A merge that leaves no record of its evidence is why twelve of the
        twenty-six merges in the ledger cannot say who approved them."""
        mr, real, bare = _mr(db_path, tmp_path, target="develop")
        mergerequests.set_review_status(mr["id"], "approved")
        # Against the branch's REAL head: the gate refuses a pass recorded for
        # some other commit, so a placeholder SHA here would be blocked rather
        # than merged, and this test would be asserting the wrong thing.
        mergerequests.set_check_result(mr["id"], "passed", output="1095 passed",
                                       command="pytest -q",
                                       head=mergerequests.current_head(mr))
        TestClient(app).post(f"/api/mr/{mr['id']}/merge")

        notes = [m["text"] for m in store.get_thread(mr["task_id"])]
        assert any("review approved" in n and "check passed (pytest -q)" in n
                   for n in notes), notes

    def test_an_override_says_so_on_the_record(self, db_path, tmp_path):
        """A merge that skipped the gate must not read like one that cleared
        it."""
        mr, real, bare = _mr(db_path, tmp_path, target="develop")
        TestClient(app).post(f"/api/mr/{mr['id']}/merge", data={"force": "1"})

        notes = [m["text"] for m in store.get_thread(mr["task_id"])]
        assert any("OVERRIDDEN by the human" in n for n in notes), notes

    def test_mr_close_route(self, db_path, tmp_path):
        mr, real, bare = _mr(db_path, tmp_path)
        TestClient(app).post(f"/api/mr/{mr['id']}/close")
        assert mergerequests.get_mr(mr["id"])["status"] == "closed"


class TestTheCardPredictsTheButton:
    """A board card said "reviewed" and nothing else. The day the merge gate
    started requiring a recorded check too, that badge stopped predicting the
    button: a card over a failing check looked ready, and a branch nobody had
    reviewed showed no badge at all — silence that reads as nothing to report.
    """

    def _card(self, db_path, tmp_path, **verdicts):
        mr, real, bare = _mr(db_path, tmp_path, target="develop")
        if "review" in verdicts:
            mergerequests.set_review_status(mr["id"], verdicts["review"])
        if "check" in verdicts:
            mergerequests.set_check_result(mr["id"], verdicts["check"], command="pytest -q")
        store.set_status(mr["task_id"], "done")
        return TestClient(app).get("/api/tasks/list").text

    def test_reviewed_and_checked_reads_as_ready(self, db_path, tmp_path):
        html = self._card(db_path, tmp_path, review="approved", check="passed")
        assert "waiting for your merge" in html

    def test_an_approval_over_a_failing_check_never_reads_as_ready(self, db_path, tmp_path):
        html = self._card(db_path, tmp_path, review="approved", check="failed")
        assert "recorded check failed" in html
        assert "waiting for your merge" not in html

    def test_a_branch_nobody_reviewed_says_so(self, db_path, tmp_path):
        html = self._card(db_path, tmp_path)
        assert "Nobody reviewed this branch" in html
        assert "waiting for your merge" not in html

    def test_a_review_that_died_says_so_not_silence(self, db_path, tmp_path):
        html = self._card(db_path, tmp_path, review="died", check="passed")
        assert "review died" in html.lower()
        assert "ready to merge" not in html

    def test_the_badge_and_the_gate_cannot_disagree(self, db_path, tmp_path):
        """gate_state asks merge_block_reason rather than re-deriving it."""
        for i, (review, check, ready) in enumerate((("approved", "passed", True),
                                                    ("approved", "failed", False),
                                                    ("pending", "passed", False),
                                                    (None, "passed", False))):
            # Row logic only — no git needed, and _mr's real repo would collide
            # with itself four times in one tmp_path.
            mr = mergerequests.create_mr(f"t{i}", "myrepo", f"b{i}", "main", "t")
            if review:
                mergerequests.set_review_status(mr["id"], review)
            mergerequests.set_check_result(mr["id"], check, command="pytest -q")
            row = mergerequests.get_mr(mr["id"])
            assert (mergerequests.gate_state(row) == "ready") is ready
            assert (mergerequests.merge_block_reason(row) is None) is ready


class TestAPassIsOnlyEvidenceForTheCommitItRanOn:
    """The gate read check_status but never check_head, so a check that passed
    on commit A kept the merge button open after the branch moved to B —
    gate_state said 'ready' while verdict_line printed 'against aaaaaaaa' in the
    same breath. web/agent_mcp.py had always told a reviewer agent the verdict
    was history; only enforcement was not looking."""

    def _row(self, ran_head, status="passed"):
        return {"id": "mr-x", "review_status": "approved", "check_status": status,
                "check_head": ran_head, "check_command": "pytest -q"}

    def test_a_pass_on_a_commit_the_branch_moved_past_is_blocked(self):
        blocked = mergerequests.merge_block_reason(self._row("aaaaaaaa"), head="bbbbbbbb")
        assert blocked and "aaaaaaaa" in blocked and "bbbbbbbb" in blocked
        assert mergerequests.gate_state(self._row("aaaaaaaa"), head="bbbbbbbb") == "check-stale"

    def test_a_pass_on_the_current_commit_still_opens_the_gate(self):
        assert mergerequests.merge_block_reason(self._row("aaaaaaaa"), head="aaaaaaaa") is None
        assert mergerequests.gate_state(self._row("aaaaaaaa"), head="aaaaaaaa") == "ready"

    def test_a_record_silent_about_its_commit_is_not_called_stale(self):
        """Every MR checked before the check_head column existed carries no
        commit. That is a gap in our bookkeeping, not evidence the branch moved,
        and the gate blocks on contradiction, never on silence."""
        assert mergerequests.merge_block_reason(self._row(""), head="bbbbbbbb") is None

    def test_an_unresolvable_head_does_not_invent_a_block(self):
        assert mergerequests.merge_block_reason(self._row("aaaaaaaa"), head="") is None

    def test_a_failing_check_still_outranks_staleness(self):
        blocked = mergerequests.merge_block_reason(self._row("aaaaaaaa", status="failed"),
                                                   head="bbbbbbbb")
        assert "FAILED" in blocked
        assert mergerequests.gate_state(self._row("aaaaaaaa", status="failed"),
                                        head="bbbbbbbb") == "check-failed"

    def test_the_gate_resolves_the_head_itself_when_not_told(self, db_path, tmp_path):
        """Correct by default: a call site that passes nothing must still get
        the strong gate, or the next one added will quietly get the weak one."""
        mr, real, bare = _mr(db_path, tmp_path)
        mergerequests.set_review_status(mr["id"], "approved")
        mergerequests.set_check_result(mr["id"], "passed", command="pytest -q",
                                       head="deadbeefcafe")   # not this branch
        row = mergerequests.get_mr(mr["id"])
        assert mergerequests.merge_block_reason(row) is not None

        mergerequests.set_check_result(mr["id"], "passed", command="pytest -q",
                                       head=mergerequests.current_head(row))
        assert mergerequests.merge_block_reason(mergerequests.get_mr(mr["id"])) is None

    def test_a_stale_pass_cannot_be_merged_through_the_route(self, db_path, tmp_path):
        mr, real, bare = _mr(db_path, tmp_path, target="develop")
        mergerequests.set_review_status(mr["id"], "approved")
        mergerequests.set_check_result(mr["id"], "passed", command="pytest -q",
                                       head="deadbeefcafe")
        TestClient(app).post(f"/api/mr/{mr['id']}/merge")
        assert mergerequests.get_mr(mr["id"])["status"] == "open"


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


class TestDiskIsReclaimedWhenTheLastMrCloses:
    """cleanup_task keeps the per-task bare clone while an MR is open, and runs
    when the *task* finishes — always before the human merges. So nothing ever
    reclaimed it afterwards: four finished tasks had left 26MB and two worktrees
    behind by the time anyone looked."""

    def _mr(self, db_path, task_id="t1", source_branch="b"):
        from core import mergerequests
        mergerequests.init_mr_table()
        return mergerequests.create_mr(
            task_id=task_id, repo="r", source_branch=source_branch, target_branch="main",
            title="", bare_path="/bare", base_branch="main", source_path="/src")

    def test_merging_the_last_mr_reclaims(self, db_path, monkeypatch):
        from core import mergerequests
        seen = []
        monkeypatch.setattr("core.worker.cleanup_task", lambda tid, deep=False: seen.append(tid))
        mr = self._mr(db_path)
        mergerequests.mark_merged(mr["id"], "abc123")
        assert seen == ["t1"]

    def test_closing_reclaims_too(self, db_path, monkeypatch):
        from core import mergerequests
        seen = []
        monkeypatch.setattr("core.worker.cleanup_task", lambda tid, deep=False: seen.append(tid))
        mr = self._mr(db_path)
        mergerequests.set_status(mr["id"], "closed")
        assert seen == ["t1"]

    def test_another_open_mr_holds_the_disk(self, db_path, monkeypatch):
        """The remaining MR still needs the bare clone to merge from."""
        from core import mergerequests
        seen = []
        monkeypatch.setattr("core.worker.cleanup_task", lambda tid, deep=False: seen.append(tid))
        first = self._mr(db_path)
        self._mr(db_path, source_branch="b2")
        mergerequests.mark_merged(first["id"], "abc123")
        assert seen == []

    def test_reopening_a_status_does_not_reclaim(self, db_path, monkeypatch):
        from core import mergerequests
        seen = []
        monkeypatch.setattr("core.worker.cleanup_task", lambda tid, deep=False: seen.append(tid))
        mr = self._mr(db_path)
        mergerequests.set_status(mr["id"], "open")
        assert seen == []

    def test_a_live_worker_holds_the_disk(self, db_path, monkeypatch):
        """Merging while the task's own reviewer is still running must not rip
        the worktree out from under it — cleanup runs again at terminal status."""
        from core import mergerequests, sessions
        seen = []
        monkeypatch.setattr("core.worker.cleanup_task", lambda tid, deep=False: seen.append(tid))
        sessions.clear()
        sessions.mint("t1", "reviewer")
        try:
            mr = self._mr(db_path)
            mergerequests.mark_merged(mr["id"], "abc123")
            assert seen == []
        finally:
            sessions.clear()

    def test_a_dead_worker_does_not_hold_the_disk(self, db_path, monkeypatch):
        """The guard is about *live* sessions — once revoked, reclaim proceeds."""
        from core import mergerequests, sessions
        seen = []
        monkeypatch.setattr("core.worker.cleanup_task", lambda tid, deep=False: seen.append(tid))
        sessions.clear()
        sessions.mint("t1", "reviewer")
        sessions.revoke_task("t1")
        mr = self._mr(db_path)
        mergerequests.mark_merged(mr["id"], "abc123")
        assert seen == ["t1"]

    def test_a_reclaim_failure_never_breaks_the_merge(self, db_path, monkeypatch):
        from core import mergerequests
        def boom(tid, deep=False):
            raise RuntimeError("disk gone")
        monkeypatch.setattr("core.worker.cleanup_task", boom)
        mr = self._mr(db_path)
        mergerequests.mark_merged(mr["id"], "abc123")
        assert mergerequests.get_mr(mr["id"])["status"] == "merged"


class TestAVerdictIsShownWhetherOrNotTheBranchLanded:
    """"Somebody asked for changes here" is worth seeing on a shipped task too
    — that is the record of a call made anyway. What must not happen is the
    two cases looking identical."""

    def _row(self, db_path, tmp_path, merged=False):
        mr, real, bare = _mr(db_path, tmp_path, target="develop")
        mergerequests.set_review_status(mr["id"], "changes_requested")
        if merged:
            mergerequests.mark_merged(mr["id"], "abc1234")
        store.set_status(mr["task_id"], "done")
        html = TestClient(app).get("/api/tasks/list").text
        return html.split("changes requested")[0].rsplit("<span", 1)[1]

    def test_an_open_verdict_blocks_and_says_so(self, db_path, tmp_path):
        tag = self._row(db_path, tmp_path)
        assert "is-settled" not in tag
        assert "cannot land" in tag

    def test_a_merged_one_is_shown_quieter_and_explains_itself(self, db_path, tmp_path):
        tag = self._row(db_path, tmp_path, merged=True)
        assert "is-settled" in tag
        assert "merged anyway" in tag
