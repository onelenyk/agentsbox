"""Tests for tools/goal_report.py — the scoreboard for docs/GOAL.md.

The git metric is exercised against a real repository with a real Sisyphus-style
merge, because the whole point of the metric is that the trunk's own history is
the evidence: a version that counted the wrong thing would still print a number.
"""
import subprocess

from core import mergerequests as mrs
from core import store
from core.db import get_conn
from tools.goal_report import (autonomy, evidence, open_gaps, reliability,
                               self_build, _rate)


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _commit(repo, name):
    (repo / name).write_text(name)
    _git(repo, "add", name)
    _git(repo, "commit", "-m", name)


def _repo(tmp_path):
    repo = tmp_path / "trunk"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _commit(repo, "root")
    return repo


def test_self_build_counts_commits_not_merges(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "by-hand")                      # a human commit on the trunk

    _git(repo, "checkout", "-b", "sisyphus/task-aaaa1111")
    _commit(repo, "agent-one")
    _commit(repo, "agent-two")                    # one merge, two commits
    _git(repo, "checkout", "main")
    _git(repo, "merge", "--no-ff", "sisyphus/task-aaaa1111",
         "-m", "Merge sisyphus/task-aaaa1111 into main (Sisyphus MR mr-dead)")

    r = self_build(str(repo), days=30)
    assert r["merges"] == 1
    assert r["delivered"] == 2                    # commits, not the merge itself
    assert r["total"] == 4                        # root + by-hand + the two
    assert r["rate"] == 50.0


def test_self_build_ignores_merges_that_are_not_ours(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-b", "feature/manual")
    _commit(repo, "hand-written")
    _git(repo, "checkout", "main")
    _git(repo, "merge", "--no-ff", "feature/manual", "-m", "Merge feature/manual")

    r = self_build(str(repo), days=30)
    assert r["merges"] == 0 and r["delivered"] == 0
    assert r["total"] == 2                        # the work still counts against us


def test_autonomy_counts_only_the_tasks_that_delivered(db_path):
    quiet = store.create_task("shipped alone", repo="sisyphus")
    store.post_message(quiet["id"], "engineer", kind="mr", text="opened mr-1")

    asked = store.create_task("needed a nudge", repo="sisyphus")
    store.post_message(asked["id"], "engineer", kind="mr", text="opened mr-2")
    store.post_message(asked["id"], "orchestrator", kind="question", text="which one?")
    store.post_message(asked["id"], "human", kind="answer", text="that one")

    scan = store.create_task("autopilot scan", repo="sisyphus")   # never delivered
    store.post_message(scan["id"], "orchestrator", kind="note", text="nothing open")

    a = autonomy(get_conn(), days=30)
    assert a["delivering"] == 2        # the scan is not a delivery
    assert a["clean"] == 1 and a["questions"] == 1
    assert a["rate"] == 50.0


def test_evidence_needs_both_the_approval_and_the_check(db_path):
    mrs.init_mr_table()
    t = store.create_task("work", repo="sisyphus")

    def _merged(review, check):
        mr = mrs.create_mr(t["id"], "sisyphus", "sisyphus/task-x", "main")
        if review:
            mrs.set_review_status(mr["id"], review)
        if check:
            mrs.set_check_result(mr["id"], check, output="1030 passed", command="pytest")
        get_conn().execute("UPDATE merge_requests SET status='merged' WHERE id=?",
                           (mr["id"],)).connection.commit()

    _merged("approved", "passed")
    _merged("approved", None)          # approved on an agent's word alone
    _merged(None, "passed")            # a green check nobody signed off on

    e = evidence(get_conn(), days=30)
    assert e["merged"] == 3 and e["approved"] == 2
    assert e["backed"] == 1 and e["rate"] == 33.3


def test_reliability_separates_silent_failures(db_path):
    ok = store.create_task("fine", repo="sisyphus"); store.set_status(ok["id"], "done")

    silent = store.create_task("nothing came back", repo="sisyphus")
    store.post_message(silent["id"], "orchestrator", kind="note",
                       text="[fail] mr-reviewer ended without a result")
    store.set_status(silent["id"], "failed")

    loud = store.create_task("honest failure", repo="sisyphus")
    store.post_message(loud["id"], "orchestrator", kind="note", text="[fail] merge conflict")
    store.set_status(loud["id"], "failed")

    store.create_task("still going", repo="sisyphus")   # active — not terminal yet

    r = reliability(get_conn(), days=30)
    assert r["terminal"] == 3 and r["failed"] == 2 and r["silent"] == 1


def test_open_gaps_reads_the_ledger(tmp_path):
    ledger = tmp_path / "known-gaps.md"
    ledger.write_text(
        "# Known gaps\n\nPreamble that mentions ### nothing.\n\n"
        "### A gap that is closed\n**Status:** CLOSED 2026-08-22 — fixed.\n\n"
        "### A gap that is open\n**Status:** open, found 2026-08-23.\n\n"
        "### A gap that forgot to say\nNo status line at all.\n")
    g = open_gaps(str(ledger))
    assert g["closed"] == 1
    assert g["open"] == 2                       # silence counts as open
    assert "A gap that is open" in g["titles"]


def test_open_gaps_survives_a_missing_ledger(tmp_path):
    assert open_gaps(str(tmp_path / "nope.md")) == {"open": 0, "closed": 0, "titles": []}


def test_a_rate_of_nothing_is_zero():
    assert _rate(0, 0) == 0.0 and _rate(1, 3) == 33.3
