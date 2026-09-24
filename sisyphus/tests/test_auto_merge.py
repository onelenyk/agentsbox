"""Tests for auto-merge — the per-repo opt-in that merges an MR once it has
cleared the gate, without a human pressing Merge.

The rules under test are the ones the feature rests on: auto-merge must never
land something the manual Merge button would refuse, and a block must be
announced exactly once per (head, reason) no matter how often the sweep runs.
"""
import subprocess

import pytest

from core import git_ops, mergerequests, store


def _run(*a):
    return subprocess.run(list(a), capture_output=True, text=True)


def _setup(tmp_path, dirty=False):
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

    if dirty:
        # A tracked-but-modified working tree: merge_branch refuses a dirty
        # checkout of the target, and `git checkout -- .` clears it again.
        (real / "README.md").write_text("hi\nmodified\n")
    return str(real), str(bare)


def _auto_mr(tmp_path, monkeypatch, auto_merge=True, dirty=False):
    """An open MR whose repo has (or lacks) the auto_merge opt-in."""
    real, bare = _setup(tmp_path, dirty=dirty)
    task = store.create_task("do it", repo="acme")
    mr = mergerequests.create_mr(
        task_id=task["id"], repo="acme", source_branch="sisyphus/task-x",
        target_branch="main", bare_path=bare, base_branch="main",
        source_path=real)
    monkeypatch.setattr("core.repos.get_repo",
                        lambda name: {"name": name, "auto_merge": auto_merge})
    return mr, real


@pytest.fixture
def no_auto_trigger(monkeypatch):
    """Unit tests drive try_auto_merge directly; keep the record-point triggers
    (set_review_status / set_check_result) from merging during state setup."""
    monkeypatch.setattr(mergerequests, "schedule_auto_merge", lambda *a, **k: None)


def _waiting_notes(task_id):
    return [m["text"] for m in store.get_thread(task_id)
            if m["text"].startswith("auto-merge waiting:")]


def _approved_passed(mr_id):
    """The gate in the clear: approved, and a check that passed against the
    branch's real current head."""
    mr = mergerequests.get_mr(mr_id)
    mergerequests.set_review_status(mr_id, "approved")
    mergerequests.set_check_result(mr_id, "passed", output="1095 passed",
                                   command="pytest -q",
                                   head=mergerequests.current_head(mr))


class TestMergesWhenTheGateIsClear:
    def test_approved_passed_check_merges_with_auto_note(self, db_path, tmp_path, monkeypatch, no_auto_trigger):
        mr, real = _auto_mr(tmp_path, monkeypatch)
        _approved_passed(mr["id"])
        mr = mergerequests.get_mr(mr["id"])

        res = mergerequests.try_auto_merge(mr)

        assert res is not None
        got = mergerequests.get_mr(mr["id"])
        assert got["status"] == "merged"
        assert got["merge_sha"] == res
        notes = [m["text"] for m in store.get_thread(mr["task_id"])]
        assert any(n.startswith("Auto-merged") and "check passed (pytest -q)" in n
                   and "review approved" in n for n in notes), notes

    def test_auto_merge_off_does_nothing(self, db_path, tmp_path, monkeypatch, no_auto_trigger):
        mr, real = _auto_mr(tmp_path, monkeypatch, auto_merge=False)
        _approved_passed(mr["id"])

        res = mergerequests.try_auto_merge(mergerequests.get_mr(mr["id"]))

        assert res is None
        assert mergerequests.get_mr(mr["id"])["status"] == "open"
        assert _waiting_notes(mr["task_id"]) == []

    def test_a_closed_mr_is_never_auto_merged(self, db_path, tmp_path, monkeypatch, no_auto_trigger):
        mr, real = _auto_mr(tmp_path, monkeypatch)
        _approved_passed(mr["id"])
        mergerequests.set_status(mr["id"], "closed")

        assert mergerequests.try_auto_merge(mergerequests.get_mr(mr["id"])) is None
        assert mergerequests.get_mr(mr["id"])["status"] == "closed"


class TestBlocksAnnounceOnce:
    """Every gate refusal: no merge, exactly one 'auto-merge waiting' note, and
    a second sweep adds no duplicate."""

    def test_review_pending(self, db_path, tmp_path, monkeypatch, no_auto_trigger):
        mr, real = _auto_mr(tmp_path, monkeypatch)
        mergerequests.set_check_result(mr["id"], "passed", command="pytest -q",
                                       head=mergerequests.current_head(mr))

        assert mergerequests.try_auto_merge(mergerequests.get_mr(mr["id"])) is None
        assert mergerequests.get_mr(mr["id"])["status"] == "open"
        assert len(_waiting_notes(mr["task_id"])) == 1
        assert "no review verdict is recorded" in _waiting_notes(mr["task_id"])[0]

        mergerequests.try_auto_merge(mergerequests.get_mr(mr["id"]))   # second sweep
        assert len(_waiting_notes(mr["task_id"])) == 1

    def test_check_failed(self, db_path, tmp_path, monkeypatch, no_auto_trigger):
        mr, real = _auto_mr(tmp_path, monkeypatch)
        mergerequests.set_review_status(mr["id"], "approved")
        mergerequests.set_check_result(mr["id"], "failed", output="3 failed",
                                       command="pytest -q")

        assert mergerequests.try_auto_merge(mergerequests.get_mr(mr["id"])) is None
        assert len(_waiting_notes(mr["task_id"])) == 1
        assert "check FAILED" in _waiting_notes(mr["task_id"])[0]

        mergerequests.try_auto_merge(mergerequests.get_mr(mr["id"]))
        assert len(_waiting_notes(mr["task_id"])) == 1

    def test_check_stale(self, db_path, tmp_path, monkeypatch, no_auto_trigger):
        mr, real = _auto_mr(tmp_path, monkeypatch)
        mergerequests.set_review_status(mr["id"], "approved")
        # The pass is recorded for a commit the branch has since moved past.
        mergerequests.set_check_result(mr["id"], "passed", command="pytest -q",
                                       head="beef0000beef0000beef0000beef0000beef0000")

        assert mergerequests.try_auto_merge(mergerequests.get_mr(mr["id"])) is None
        assert len(_waiting_notes(mr["task_id"])) == 1
        assert "that verdict is history" in _waiting_notes(mr["task_id"])[0]

        mergerequests.try_auto_merge(mergerequests.get_mr(mr["id"]))
        assert len(_waiting_notes(mr["task_id"])) == 1

    def test_conflict(self, db_path, tmp_path, monkeypatch, no_auto_trigger):
        mr, real = _auto_mr(tmp_path, monkeypatch)
        _approved_passed(mr["id"])
        monkeypatch.setattr("core.mergerequests.check_mergeability",
                            lambda *a, **k: {"ok": True, "mergeable": False,
                                             "conflicts": ["feature.py"]})

        assert mergerequests.try_auto_merge(mergerequests.get_mr(mr["id"])) is None
        assert mergerequests.get_mr(mr["id"])["status"] == "open"
        assert len(_waiting_notes(mr["task_id"])) == 1
        assert "does not merge cleanly" in _waiting_notes(mr["task_id"])[0]

        mergerequests.try_auto_merge(mergerequests.get_mr(mr["id"]))
        assert len(_waiting_notes(mr["task_id"])) == 1

    def test_dirty_checkout_blocks_then_lands_once_clean(self, db_path, tmp_path, monkeypatch, no_auto_trigger):
        mr, real = _auto_mr(tmp_path, monkeypatch, dirty=True)
        _approved_passed(mr["id"])

        assert mergerequests.try_auto_merge(mergerequests.get_mr(mr["id"])) is None
        assert mergerequests.get_mr(mr["id"])["status"] == "open"
        assert len(_waiting_notes(mr["task_id"])) == 1
        assert "uncommitted changes" in _waiting_notes(mr["task_id"])[0]

        # The sweep finds the same block again → still one note.
        mergerequests.try_auto_merge(mergerequests.get_mr(mr["id"]))
        assert len(_waiting_notes(mr["task_id"])) == 1

        # Once the tree is clean the sweep lands it.
        _run("git", "-C", real, "checkout", "--", ".")
        assert mergerequests.try_auto_merge(mergerequests.get_mr(mr["id"]))
        assert mergerequests.get_mr(mr["id"])["status"] == "merged"


class TestRecordPointTriggers:
    """The daemon records a verdict or a check result and the MR should not
    wait up to the next sweep for its auto-merge attempt."""

    def test_approved_review_triggers_an_attempt(self, db_path, tmp_path, monkeypatch):
        mr, real = _auto_mr(tmp_path, monkeypatch)
        attempts = []
        monkeypatch.setattr(mergerequests, "try_auto_merge",
                            lambda m: attempts.append(m["id"]))
        mergerequests.set_review_status(mr["id"], "approved")
        assert attempts == [mr["id"]]

    def test_passed_check_triggers_an_attempt(self, db_path, tmp_path, monkeypatch):
        mr, real = _auto_mr(tmp_path, monkeypatch)
        attempts = []
        monkeypatch.setattr(mergerequests, "try_auto_merge",
                            lambda m: attempts.append(m["id"]))
        mergerequests.set_check_result(mr["id"], "passed", command="pytest -q",
                                       head=mergerequests.current_head(mr))
        assert attempts == [mr["id"]]
