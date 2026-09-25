"""Tests for tools/goal_report.py — the scoreboard for docs/GOAL.md.

The git metric is exercised against a real repository with a real Sisyphus-style
merge, because the whole point of the metric is that the trunk's own history is
the evidence: a version that counted the wrong thing would still print a number.
"""
import subprocess

from core import mergerequests as mrs
from core import store
from core.db import get_conn
from tools.goal_report import (autonomy, evidence, open_gaps, readiness, reliability,
                               render, self_build, _rate)


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


def test_readiness_wasted_runs_counts_workers_without_results(db_path):
    # Good run that produced a result
    good = store.create_task("worked", repo="sisyphus")
    store.post_message(good["id"], "orchestrator", kind="worker_finished",
                      text="worker returned a diff")

    # Bad runs that wasted a worker slot
    wasted1 = store.create_task("nothing came back", repo="sisyphus")
    store.post_message(wasted1["id"], "orchestrator", kind="worker_finished",
                      text="agent_no_result")

    wasted2 = store.create_task("vanished", repo="sisyphus")
    store.post_message(wasted2["id"], "orchestrator", kind="worker_finished",
                      text="worker vanished mid-run")

    wasted3 = store.create_task("never started", repo="sisyphus")
    store.post_message(wasted3["id"], "orchestrator", kind="worker_finished",
                      text="failed to start: too many workers")

    r = readiness(get_conn(), days=30)
    assert r["all_finished"] == 4
    assert r["wasted_finished"] == 3
    assert r["wasted_rate"] == 75.0


def test_readiness_wasted_runs_zero_when_no_finished(db_path):
    store.create_task("still running", repo="sisyphus")
    r = readiness(get_conn(), days=30)
    assert r["all_finished"] == 0
    assert r["wasted_finished"] == 0
    assert r["wasted_rate"] == 0.0


def test_readiness_reports_7_day_wasted_rate_next_to_30_day(db_path):
    """The 30-day wasted figure keeps carrying history the recent fixes already
    ended; the 7-day figure next to it is the one that shows a live improvement.
    Both are counted, and the report renders them side by side."""
    from datetime import datetime, timedelta, timezone
    conn = get_conn()

    def _backdate(task_id, days_ago):
        at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        conn.execute("UPDATE messages SET created_at=? WHERE task_id=?", (at, task_id))
        conn.commit()

    # 20 days ago: a wasted run — counts in 30d, not in 7d
    old = store.create_task("old wasted run", repo="sisyphus")
    store.post_message(old["id"], "orchestrator", kind="worker_finished",
                      text="agent_no_result")
    _backdate(old["id"], 20)

    # 3 days ago: a good run and a wasted run — both count in 30d and 7d
    recent = store.create_task("recent good run", repo="sisyphus")
    store.post_message(recent["id"], "orchestrator", kind="worker_finished",
                       text="worker returned a diff")
    recent_wasted = store.create_task("recent wasted run", repo="sisyphus")
    store.post_message(recent_wasted["id"], "orchestrator", kind="worker_finished",
                       text="worker vanished mid-run")
    _backdate(recent["id"], 3)
    _backdate(recent_wasted["id"], 3)

    r = readiness(get_conn(), days=30)
    assert r["all_finished"] == 3 and r["wasted_finished"] == 2
    assert r["wasted_rate"] == 66.7                       # 2 of 3 over 30 days
    assert r["all_finished_7d"] == 2 and r["wasted_finished_7d"] == 1
    assert r["wasted_rate_7d"] == 50.0                    # 1 of 2 over 7 days

    rendered = render({"days": 30, "target": 50,
                       "self_build": {"rate": 0.0, "delivered": 0, "total": 0,
                                      "merges": 0},
                       "autonomy": {"rate": 0.0, "clean": 0, "delivering": 0,
                                    "questions": 0},
                       "evidence": {"rate": 0.0, "backed": 0, "merged": 0,
                                    "approved": 0, "legacy": 0},
                       "reliability": {"rate": 0.0, "failed": 0, "terminal": 0,
                                       "silent": 0},
                       "gaps": {"open": 0, "closed": 0, "titles": []},
                       "readiness": r})
    assert "66.7%" in rendered and "2 of 3 worker runs ended without a result" in rendered
    assert "50.0%" in rendered and "1 of 2 worker runs ended without a result" in rendered
    assert "wasted runs 7d" in rendered


def test_readiness_provider_faults_counts_cooldowns(db_path):
    """Counted per provider, from the sentence core.worker._set_aside_note writes."""
    note = ("engineer ended without reporting a result. The {} runner is set aside "
            "until 14:05 UTC: its run never reached the model.")
    for provider in ("claude", "claude", "opencode-go"):
        t = store.create_task(f"{provider} fault", repo="sisyphus")
        store.post_message(t["id"], "system", kind="worker_finished", text=note.format(provider))

    r = readiness(get_conn(), days=30)
    assert r["provider_faults"] == {"claude": 2, "opencode-go": 1}


def test_readiness_provider_faults_empty_when_no_cooldowns(db_path):
    store.create_task("fine run", repo="sisyphus")
    store.post_message(store.create_task("ok", repo="sisyphus")["id"], "orchestrator",
                      kind="worker_finished", text="completed normally")
    r = readiness(get_conn(), days=30)
    assert r["provider_faults"] == {}


def test_readiness_duplicate_workers_finds_concurrent_starts(db_path):
    task = store.create_task("concurrent worker issue", repo="sisyphus")

    # First worker starts
    store.post_message(task["id"], "orchestrator", kind="worker_started",
                      text="worker A starting")
    # Second worker starts before first finishes (duplicate)
    store.post_message(task["id"], "orchestrator", kind="worker_started",
                      text="worker B starting")
    # First finishes
    store.post_message(task["id"], "orchestrator", kind="worker_finished",
                      text="worker A done")

    r = readiness(get_conn(), days=30)
    assert r["duplicates"] == 1


def test_readiness_duplicate_workers_zero_when_sequential(db_path):
    task = store.create_task("sequential runs", repo="sisyphus")

    store.post_message(task["id"], "orchestrator", kind="worker_started",
                      text="worker 1 starting")
    store.post_message(task["id"], "orchestrator", kind="worker_finished",
                      text="worker 1 done")
    store.post_message(task["id"], "orchestrator", kind="worker_started",
                      text="worker 2 starting")
    store.post_message(task["id"], "orchestrator", kind="worker_finished",
                      text="worker 2 done")

    r = readiness(get_conn(), days=30)
    assert r["duplicates"] == 0


def test_readiness_duplicate_workers_empty_when_no_workers(db_path):
    store.create_task("no workers", repo="sisyphus")
    r = readiness(get_conn(), days=30)
    assert r["duplicates"] == 0


def test_readiness_lost_work_finds_commits_without_mr(db_path):
    mrs.init_mr_table()

    # Task with commit and MR (not lost)
    has_mr = store.create_task("with mr", repo="sisyphus")
    store.set_status(has_mr["id"], "done")
    store.post_message(has_mr["id"], "orchestrator", kind="commit",
                      text="commit abc123")
    mr = mrs.create_mr(has_mr["id"], "sisyphus", "sisyphus/task-x", "main")
    get_conn().execute("UPDATE merge_requests SET status='merged' WHERE id=?",
                      (mr["id"],)).connection.commit()

    # Task with commit but no MR (lost work)
    no_mr = store.create_task("lost work", repo="sisyphus")
    store.set_status(no_mr["id"], "done")
    store.post_message(no_mr["id"], "orchestrator", kind="commit",
                      text="commit def456")

    # Active task with commit (not terminal yet, not lost)
    active = store.create_task("still running", repo="sisyphus")
    store.post_message(active["id"], "orchestrator", kind="commit",
                      text="commit ghi789")

    r = readiness(get_conn(), days=30)
    assert r["lost_work"] == 1


def test_readiness_lost_work_zero_when_all_have_mrs(db_path):
    mrs.init_mr_table()
    t = store.create_task("all good", repo="sisyphus")
    store.set_status(t["id"], "done")
    store.post_message(t["id"], "orchestrator", kind="commit", text="commit xyz")
    mr = mrs.create_mr(t["id"], "sisyphus", "sisyphus/task-x", "main")
    get_conn().execute("UPDATE merge_requests SET status='merged' WHERE id=?",
                      (mr["id"],)).connection.commit()
    r = readiness(get_conn(), days=30)
    assert r["lost_work"] == 0


def test_readiness_merge_latency_calculates_median_hours(db_path):
    mrs.init_mr_table()
    t = store.create_task("latency test", repo="sisyphus")

    # MR 1: 1 hour latency
    mr1 = mrs.create_mr(t["id"], "sisyphus", "sisyphus/task-1", "main")
    get_conn().execute(
        "UPDATE merge_requests SET created_at=?, merged_at=? WHERE id=?",
        ("2026-09-24T10:00:00+00:00", "2026-09-24T11:00:00+00:00", mr1["id"])
    ).connection.commit()
    get_conn().execute("UPDATE merge_requests SET status='merged' WHERE id=?",
                      (mr1["id"],)).connection.commit()

    # MR 2: 3 hours latency
    mr2 = mrs.create_mr(t["id"], "sisyphus", "sisyphus/task-2", "main")
    get_conn().execute(
        "UPDATE merge_requests SET created_at=?, merged_at=? WHERE id=?",
        ("2026-09-24T10:00:00+00:00", "2026-09-24T13:00:00+00:00", mr2["id"])
    ).connection.commit()
    get_conn().execute("UPDATE merge_requests SET status='merged' WHERE id=?",
                      (mr2["id"],)).connection.commit()

    # MR 3: 5 hours latency
    mr3 = mrs.create_mr(t["id"], "sisyphus", "sisyphus/task-3", "main")
    get_conn().execute(
        "UPDATE merge_requests SET created_at=?, merged_at=? WHERE id=?",
        ("2026-09-24T10:00:00+00:00", "2026-09-24T15:00:00+00:00", mr3["id"])
    ).connection.commit()
    get_conn().execute("UPDATE merge_requests SET status='merged' WHERE id=?",
                      (mr3["id"],)).connection.commit()

    r = readiness(get_conn(), days=30)
    assert r["median_latency_hours"] == 3.0  # median of [1, 3, 5]
    assert r["merged_count"] == 3


def test_readiness_merge_latency_zero_when_no_merges(db_path):
    mrs.init_mr_table()
    r = readiness(get_conn(), days=30)
    assert r["median_latency_hours"] == 0.0
    assert r["merged_count"] == 0


def test_readiness_merge_latency_handles_two_merges(db_path):
    mrs.init_mr_table()
    t = store.create_task("two merges", repo="sisyphus")

    # MR 1: 2 hours
    mr1 = mrs.create_mr(t["id"], "sisyphus", "sisyphus/task-1", "main")
    get_conn().execute(
        "UPDATE merge_requests SET created_at=?, merged_at=? WHERE id=?",
        ("2026-09-24T10:00:00+00:00", "2026-09-24T12:00:00+00:00", mr1["id"])
    ).connection.commit()
    get_conn().execute("UPDATE merge_requests SET status='merged' WHERE id=?",
                      (mr1["id"],)).connection.commit()

    # MR 2: 4 hours
    mr2 = mrs.create_mr(t["id"], "sisyphus", "sisyphus/task-2", "main")
    get_conn().execute(
        "UPDATE merge_requests SET created_at=?, merged_at=? WHERE id=?",
        ("2026-09-24T10:00:00+00:00", "2026-09-24T14:00:00+00:00", mr2["id"])
    ).connection.commit()
    get_conn().execute("UPDATE merge_requests SET status='merged' WHERE id=?",
                      (mr2["id"],)).connection.commit()

    r = readiness(get_conn(), days=30)
    assert r["median_latency_hours"] == 3.0  # (2 + 4) / 2


def test_readiness_stale_check_blocks_counts_unresolved_notes(db_path):
    """The metric counts only genuine stale-check block notes — the one text
    mergerequests._note_auto_merge_waiting writes for a check-stale — and only
    those whose block was never resolved automatically. A block resolved by a
    re-check on the current head or by an auto-merge is history, not a live
    problem; an 'auto-merge waiting' note about review or a dirty checkout was
    never about the check at all."""
    mrs.init_mr_table()
    stale_reason = ("auto-merge waiting: the check passed against aaaa1111, but "
                    "the branch is now at bbbb2222 — that verdict is history, re-run it")

    # Never resolved: the note was posted and nothing cleared the block.
    stuck = store.create_task("stuck on a stale check", repo="sisyphus")
    stuck_note = store.post_message(stuck["id"], "system", kind="note", text=stale_reason)
    conn = get_conn()
    conn.execute("UPDATE messages SET created_at=? WHERE id=?",
                 ("2026-09-01T10:00:00+00:00", stuck_note["id"])).connection.commit()

    # Resolved by an auto-merge: the daemon re-checked the head and landed the MR.
    landed = store.create_task("auto-merged away", repo="sisyphus")
    landed_note = store.post_message(landed["id"], "system", kind="note", text=stale_reason)
    conn.execute("UPDATE messages SET created_at=? WHERE id=?",
                 ("2026-09-02T10:00:00+00:00", landed_note["id"])).connection.commit()
    store.post_message(landed["id"], "system", kind="note",
                       text="Auto-merged sisyphus/task-landed into main (bbbb2222) — review approved")

    # Resolved by a re-check on the current head: check_ran_at after the note.
    rechecked = store.create_task("re-checked head", repo="sisyphus")
    rc_mr = mrs.create_mr(rechecked["id"], "sisyphus", "sisyphus/task-rechecked", "main")
    rc_note = store.post_message(rechecked["id"], "system", kind="note", text=stale_reason)
    conn.execute("UPDATE messages SET created_at=? WHERE id=?",
                 ("2026-09-03T10:00:00+00:00", rc_note["id"])).connection.commit()
    conn.execute("UPDATE merge_requests SET check_ran_at=? WHERE id=?",
                 ("2026-09-03T11:00:00+00:00", rc_mr["id"])).connection.commit()

    # Never about the check: a review block, and prose that mentions the words.
    review_blocked = store.create_task("review wait", repo="sisyphus")
    store.post_message(review_blocked["id"], "system", kind="note",
                       text="auto-merge waiting: review is pending")
    chatter = store.create_task("chatter", repo="sisyphus")
    store.post_message(chatter["id"], "engineer", kind="note",
                       text="so a new commit no longer leaves the check stale/merge blocked")

    r = readiness(get_conn(), days=30)
    assert r["stale_blocks"] == 1


def test_readiness_stale_check_blocks_zero_when_none(db_path):
    t = store.create_task("no blocks", repo="sisyphus")
    store.post_message(t["id"], "orchestrator", kind="note",
                       text="[status] all clear")
    r = readiness(get_conn(), days=30)
    assert r["stale_blocks"] == 0


def test_readiness_stale_check_blocks_renders_the_unresolved_count(db_path):
    """The human-facing line shows the corrected meaning: only blocks no
    automatic path cleared are reported as live problems."""
    mrs.init_mr_table()
    stale_reason = ("auto-merge waiting: the check passed against aaaa1111, but "
                    "the branch is now at bbbb2222 — that verdict is history, re-run it")
    stuck = store.create_task("stuck on a stale check", repo="sisyphus")
    note = store.post_message(stuck["id"], "system", kind="note", text=stale_reason)
    conn = get_conn()
    conn.execute("UPDATE messages SET created_at=? WHERE id=?",
                 ("2026-09-01T10:00:00+00:00", note["id"])).connection.commit()

    rd = readiness(conn, days=30)
    rendered = render({"days": 30, "target": 50,
                       "self_build": {"rate": 0.0, "delivered": 0, "total": 0,
                                      "merges": 0},
                       "autonomy": {"rate": 0.0, "clean": 0, "delivering": 0,
                                    "questions": 0},
                       "evidence": {"rate": 0.0, "backed": 0, "merged": 0,
                                    "approved": 0, "legacy": 0},
                       "reliability": {"rate": 0.0, "failed": 0, "terminal": 0,
                                       "silent": 0},
                       "gaps": {"open": 0, "closed": 0, "titles": []},
                       "readiness": rd})
    assert "stale-check blocks 1 notes waiting on check status, never auto-resolved" in rendered


def test_readiness_human_turns_per_delivered_task(db_path):
    mrs.init_mr_table()

    # Task 1: delivered, no human answers
    t1 = store.create_task("auto delivery", repo="sisyphus")
    mrs.create_mr(t1["id"], "sisyphus", "sisyphus/task-1", "main")

    # Task 2: delivered, 1 human answer
    t2 = store.create_task("one question", repo="sisyphus")
    store.post_message(t2["id"], "orchestrator", kind="question", text="ok?")
    store.post_message(t2["id"], "human", kind="answer", text="yes")
    mrs.create_mr(t2["id"], "sisyphus", "sisyphus/task-2", "main")

    # Task 3: delivered, 2 human answers
    t3 = store.create_task("two questions", repo="sisyphus")
    store.post_message(t3["id"], "orchestrator", kind="question", text="which one?")
    store.post_message(t3["id"], "human", kind="answer", text="left")
    store.post_message(t3["id"], "orchestrator", kind="question", text="really?")
    store.post_message(t3["id"], "human", kind="answer", text="yes")
    mrs.create_mr(t3["id"], "sisyphus", "sisyphus/task-3", "main")

    # Task 4: not delivered (no MR)
    t4 = store.create_task("not delivered", repo="sisyphus")
    store.post_message(t4["id"], "human", kind="answer", text="ignored")

    r = readiness(get_conn(), days=30)
    # (0 + 1 + 2) / 3 = 1.0 turn per delivered task
    assert r["human_per_delivered"] == 1.0


def test_readiness_human_turns_zero_when_no_delivered(db_path):
    store.create_task("not delivered", repo="sisyphus")
    r = readiness(get_conn(), days=30)
    assert r["human_per_delivered"] == 0.0


def test_readiness_human_turns_zero_when_all_clean_deliveries(db_path):
    mrs.init_mr_table()
    t1 = store.create_task("clean 1", repo="sisyphus")
    t2 = store.create_task("clean 2", repo="sisyphus")
    mrs.create_mr(t1["id"], "sisyphus", "sisyphus/task-1", "main")
    mrs.create_mr(t2["id"], "sisyphus", "sisyphus/task-2", "main")
    r = readiness(get_conn(), days=30)
    assert r["human_per_delivered"] == 0.0
