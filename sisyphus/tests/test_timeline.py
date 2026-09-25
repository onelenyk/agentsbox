"""Reading a task's thread as what happened — core/timeline.py.

Synthetic threads in the exact shapes the daemon writes, so each rule is
pinned to the message that triggers it.
"""
from core import timeline as tl


def _m(seq, kind, author, text, at=None, **kw):
    return {"seq": seq, "kind": kind, "author": author, "text": text,
            "created_at": at or f"2026-09-04T10:{seq:02d}:00+00:00", "attachment": kw.get("attachment")}


TASK = {"id": "task-x", "status": "done", "repo": "sisyphus",
        "created_at": "2026-09-04T09:00:00+00:00", "updated_at": "2026-09-04T11:00:00+00:00"}


class TestTheOutcomeIsTheAnswerNotTheVerdictOnIt:
    def test_a_workers_result_beats_a_later_verdict(self):
        msgs = [_m(1, "result", "engineer", "Built the thing."),
                _m(2, "result", "reviewer", "APPROVED: looks right")]
        o = tl.outcome(TASK, msgs)
        assert o["kind"] == "answer" and o["author"] == "engineer" and o["text"] == "Built the thing."

    def test_a_reviewer_alone_is_its_own_answer(self):
        o = tl.outcome(TASK, [_m(1, "result", "mr-reviewer", "No open merge requests.")])
        assert o["author"] == "mr-reviewer"

    def test_a_scan_loses_the_verdict_word_it_had_no_business_saying(self):
        scan = {**TASK, "repo": ""}
        o = tl.outcome(scan, [_m(1, "result", "reviewer", "APPROVED: Scan complete, report delivered")])
        assert o["text"] == "Scan complete, report delivered"

    def test_a_failure_explains_itself(self):
        failed = {**TASK, "status": "failed"}
        msgs = [_m(1, "worker_finished", "system",
                   "mr-reviewer ended without reporting a result (status=timeout): Agent exceeded 300s"),
                _m(2, "note", "orchestrator", "[fail] mr-reviewer ended without a result (2 attempts)")]
        o = tl.outcome(failed, msgs)
        assert o["kind"] == "failure"
        assert o["text"] == "mr-reviewer ended without a result (2 attempts)"
        assert "exceeded 300s" in o["detail"]

    def test_a_waiting_task_shows_its_question(self):
        o = tl.outcome({**TASK, "status": "waiting"}, [_m(1, "question", "engineer", "Which database?")])
        assert o["kind"] == "question" and o["text"] == "Which database?"


class TestStepsAreRunsNotMessages:
    def test_a_run_folds_its_start_end_commits_and_reason(self):
        msgs = [_m(1, "note", "orchestrator", "[spawn_worker] the goal needs code"),
                _m(2, "worker_started", "system", "spawning engineer: implement it"),
                _m(3, "commit", "engineer", "feat: it (abc1234)"),
                _m(9, "worker_finished", "system", "engineer finished")]
        (run,) = tl.steps(msgs)
        assert run["type"] == "run" and run["role"] == "engineer" and run["how"] == "ok"
        assert run["mins"] == 7 and run["commits"] == ["feat: it (abc1234)"]
        assert run["why"] == "the goal needs code" and run["instruction"] == "implement it"

    def test_dead_attempts_of_one_role_collapse_into_one_step(self):
        msgs = [_m(1, "worker_started", "system", "spawning engineer: a"),
                _m(2, "worker_finished", "system",
                   "engineer ended without reporting a result (status=timeout): no output"),
                _m(3, "worker_finished", "system", "worker vanished without reporting (no live session) — resuming"),
                _m(4, "worker_finished", "system", "worker vanished without reporting (no live session) — resuming")]
        (dead,) = tl.steps(msgs)
        assert dead["type"] == "dead-runs" and dead["role"] == "engineer"
        assert dead["hows"] == {"timeout": 1, "vanished": 2}

    def test_two_runs_of_one_role_at_once_each_get_their_own_end(self):
        """The orchestrator re-spawned an engineer a minute after the first;
        a map keyed by role alone left the first 'running' forever."""
        msgs = [_m(1, "worker_started", "system", "spawning engineer: first"),
                _m(2, "worker_started", "system", "spawning engineer: second"),
                _m(5, "worker_finished", "system", "engineer finished"),
                _m(8, "worker_finished", "system", "engineer finished")]
        runs = [s for s in tl.steps(msgs) if s["type"] == "run"]
        assert [r["instruction"] for r in runs] == ["first", "second"]
        assert all(r["how"] == "ok" for r in runs)
        assert [r["mins"] for r in runs] == [4, 6]

    def test_a_delivered_run_closes_as_delivered_not_running(self):
        """A run that posted its deliverable but never called the finishing tool
        should close with how='delivered', not stay 'running'."""
        msgs = [_m(1, "worker_started", "system", "spawning reviewer: review it"),
                _m(5, "worker_finished", "system",
                   "reviewer delivered its verdict (APPROVED) but ended without calling its finishing tool (status=completed): the work reached the thread")]
        (run,) = tl.steps(msgs)
        assert run["type"] == "run" and run["role"] == "reviewer"
        assert run["how"] == "delivered"
        assert run["ended"] is not None

    def test_ended_without_reporting_still_closes_with_status(self):
        """The old message pattern 'ended without reporting a result' should still
        close the run with its status (e.g., 'completed', 'timeout')."""
        msgs = [_m(1, "worker_started", "system", "spawning engineer: build it"),
                _m(3, "worker_finished", "system",
                   "engineer ended without reporting a result (status=completed): no output")]
        steps = tl.steps(msgs)
        assert len(steps) == 1
        step = steps[0]
        # A run with no commits and a status is a dead-run
        assert step["type"] == "dead-runs"
        assert step["role"] == "engineer"
        assert step["hows"] == {"completed": 1}
        assert step["ended"] is not None

    def test_checks_verdicts_decisions_and_the_merge_are_steps(self):
        msgs = [_m(1, "note", "system", "Automated check FAILED (exit 1, 475s): `pytest -q`\n"
                                        "FAILED tests/test_x.py::test_y - assert\n1 failed, 1301 passed"),
                _m(2, "note", "system", "Automated check PASSED (exit 0, 495s): `pytest -q` 1302 passed"),
                _m(3, "note", "system", "review verdict for mr-95dbed9b: APPROVED — looks right"),
                _m(4, "note", "orchestrator", "[done] Reviewer APPROVED and the check PASSED"),
                _m(5, "note", "system", "Merged sisyphus/task-x into main (1a0dd7b9) in /repo — review approved")]
        kinds = [s["type"] for s in tl.steps(msgs)]
        assert kinds == ["check", "check", "verdict", "decision", "merge"]
        failed, passed, verdict, decision, merge = tl.steps(msgs)
        assert failed["broke"] == "tests/test_x.py::test_y" and not failed["passed"]
        assert passed["passed"] and passed["tests"] == 1302
        assert verdict["approved"] and decision["done"] and merge["sha"] == "1a0dd7b9"

    def test_service_traffic_is_not_a_step(self):
        msgs = [_m(1, "worker_queued", "system", "engineer queued"),
                _m(2, "note", "system", "[sandbox] this checkout carries its own opencode config")]
        assert tl.steps(msgs) == []


class TestHistoryHasCausesAndPauses:
    def test_rebuilt_from_the_thread_when_nothing_was_recorded(self):
        msgs = [_m(1, "question", "orchestrator", "Check the runtime?", at="2026-09-03T20:51:00+00:00"),
                _m(2, "answer", "human", "fixed", at="2026-09-04T17:53:00+00:00"),
                _m(3, "note", "orchestrator", "[done] approved", at="2026-09-04T19:43:00+00:00")]
        task = {**TASK, "created_at": "2026-09-03T20:48:00+00:00"}
        h = tl.history(task, msgs, None, [])
        assert [e["status"] for e in h] == ["created", "waiting", "active", "done"]
        assert h[2]["after"] == "21h" and h[2]["why"] == "you answered"
        assert all(e["inferred"] for e in h[1:])

    def test_follow_up_work_after_done_does_not_reopen_it(self):
        msgs = [_m(1, "note", "orchestrator", "[done] approved"),
                _m(2, "worker_started", "system", "spawning reviewer: re-review")]
        assert [e["status"] for e in tl.history(TASK, msgs, None, [])] == ["created", "done"]

    def test_recorded_events_win_over_reconstruction(self):
        events = [{"at": "2026-09-04T09:05:00+00:00", "from_status": "active", "to_status": "waiting",
                   "cause": "stopped by you"}]
        h = tl.history({**TASK, "status": "waiting"}, [_m(1, "note", "orchestrator", "[done] x")], None, events)
        assert [e["status"] for e in h] == ["created", "waiting"]
        assert h[1]["why"] == "stopped by you" and not h[1]["inferred"]

    def test_a_merge_ships_it(self):
        mr = {"status": "merged", "merged_at": "2026-09-19T18:23:00+00:00", "target_branch": "main"}
        h = tl.history(TASK, [_m(1, "note", "orchestrator", "[done] ok")], mr, [])
        assert h[-1]["status"] == "shipped" and h[-1]["why"] == "merged into main"
        assert h[-1]["after"] == "15d"


class TestFilesListTheDirectoryNotJustWhatWasLinked:
    def test_groups_what_a_task_left_behind(self, tmp_path):
        adir = tmp_path / "tasks" / "task-x" / "attachments"
        adir.mkdir(parents=True)
        (adir / "change.patch").write_text("diff")
        (adir / "mr-scan-report.md").write_text("# report")
        (adir / "salvaged-task-x_engineer.patch").write_text("wip")
        ldir = tmp_path / "runs" / "task-x" / "logs"
        ldir.mkdir(parents=True)
        (ldir / "task-x-engineer.log").write_text("log")

        msgs = [_m(1, "note", "reviewer", "report attached", attachment="mr-scan-report.md")]
        f = tl.files("task-x", msgs, workspace=str(tmp_path))
        assert [(x["name"], x["kind"]) for x in f["produced"]] == [("change.patch", "patch"),
                                                                    ("mr-scan-report.md", "report")]
        assert f["produced"][1]["by"] == "reviewer"                   # from the message that posted it
        assert [(x["kind"], x["by"]) for x in f["recovered"]] == [("salvaged", "engineer")]
        assert [(x["name"], x["by"]) for x in f["logs"]] == [("task-x-engineer.log", "engineer")]
        assert f["count"] == 4

    def test_a_task_with_nothing_on_disk_has_nothing(self, tmp_path):
        assert tl.files("task-none", [], workspace=str(tmp_path))["count"] == 0


def test_spans_read_the_way_people_say_them():
    assert [tl.span(m) for m in (None, 7, 60, 125, 60 * 24 * 15)] == ["", "7m", "1h", "2h05m", "15d"]
    assert tl.human_size(107) == "107 B" and tl.human_size(5256) == "5.1 KB"
