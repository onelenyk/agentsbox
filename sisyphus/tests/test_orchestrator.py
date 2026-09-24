"""Tests for core/orchestrator.py — the (thread + event) -> decision loop.

decide_fn is always a scripted stub here: these tests prove the orchestrator's
own logic (dispatch, guards, action execution), never a real LLM call.
"""
import pytest
from unittest.mock import patch

from core import orchestrator, sessions, store
from core.orchestrator import Decision, handle_event, reconcile_on_boot


@pytest.fixture(autouse=True)
def _reset():
    orchestrator.reset_queue()
    sessions.clear()
    yield
    orchestrator.reset_queue()
    sessions.clear()


def _scripted(decisions):
    """A decide_fn that returns queued Decisions in order, ignoring input."""
    it = iter(decisions)

    async def _fn(task, thread_text):
        return next(it)

    return _fn


class TestFullLifecycle:
    @pytest.mark.asyncio
    async def test_spawn_review_request_changes_respawn_approve_done(self, db_path):
        task = store.create_task("fix the login bug", repo="sisyphus")
        task_id = task["id"]

        async def fake_spawn(tid, role, instruction, repo_name=None, **kw):
            store.post_message(tid, role, kind="result", text=f"{role} done: {instruction}")
            # Every real spawn path ends with worker_finished (agent_mcp's
            # _end_worker or spawn_worker's crash fallback) — without it the
            # queued/started marker would read as still in flight.
            store.post_message(tid, "system", kind="worker_finished", text=f"{role} finished")

        decisions = _scripted([
            Decision(action="spawn_worker", role="engineer", instruction="fix it", why="start with a fix"),
            Decision(action="spawn_worker", role="reviewer", instruction="review the fix", why="check quality"),
            Decision(action="spawn_worker", role="engineer", instruction="address feedback", why="changes requested"),
            Decision(action="spawn_worker", role="reviewer", instruction="re-review", why="check again"),
            Decision(action="done", why="approved"),
        ])

        # Disable obvious step rules by patching _obvious_step to always return None
        with patch("core.orchestrator._obvious_step", return_value=None):
            with patch("core.orchestrator.spawn_worker", side_effect=fake_spawn):
                decision, bg = await handle_event(task_id, "created", decide_fn=decisions)
                assert decision.action == "spawn_worker" and decision.role == "engineer"
                await bg
                assert store.get_task(task_id)["status"] == "active"

                decision, bg = await handle_event(task_id, "worker_finished", decide_fn=decisions)
                assert decision.role == "reviewer"
                await bg

                # Simulate the reviewer requesting changes (a real session's
                # request_changes() would post exactly this kind of result).
                store.post_message(task_id, "reviewer", kind="result", text="CHANGES REQUESTED: needs tests")

                decision, bg = await handle_event(task_id, "worker_finished", decide_fn=decisions)
                assert decision.role == "engineer"
                await bg

                decision, bg = await handle_event(task_id, "worker_finished", decide_fn=decisions)
                assert decision.role == "reviewer"
                await bg

                store.post_message(task_id, "reviewer", kind="result", text="APPROVED: looks good")

                decision, bg = await handle_event(task_id, "worker_finished", decide_fn=decisions)
                assert decision.action == "done"

        assert store.get_task(task_id)["status"] == "done"
        # The rationale trail is fully readable from the thread alone.
        tags = [m["text"].split("]")[0] + "]" for m in store.get_thread(task_id) if m["author"] == "orchestrator"]
        assert tags == ["[spawn_worker]"] * 4 + ["[done]"]


class TestHumanLoop:
    @pytest.mark.asyncio
    async def test_ask_human_then_resume_after_answer(self, db_path):
        task = store.create_task("decide something risky")
        task_id = task["id"]

        decisions = _scripted([
            Decision(action="ask_human", question="Should I proceed?", why="needs approval"),
            Decision(action="spawn_worker", role="explorer", instruction="go", why="human said yes"),
        ])

        async def fake_spawn(tid, role, instruction, repo_name=None, **kw):
            store.post_message(tid, role, kind="result", text="done")

        decision, bg = await handle_event(task_id, "created", decide_fn=decisions)
        assert decision.action == "ask_human"
        assert bg is None
        assert store.get_task(task_id)["status"] == "waiting"
        questions = [m for m in store.get_thread(task_id) if m["kind"] == "question"]
        assert questions[-1]["text"] == "Should I proceed?"

        # Human replies in the same thread — the web surface posts this message
        # and calls emit(task_id, "human_replied"); we simulate both. The reply
        # resumes the loop out of 'waiting'.
        store.post_message(task_id, "human", kind="answer", text="yes, go ahead")

        with patch("core.orchestrator.spawn_worker", side_effect=fake_spawn):
            decision, bg = await handle_event(task_id, "human_replied", decide_fn=decisions)
            if bg:
                await bg
        assert decision.action == "spawn_worker"
        assert store.get_task(task_id)["status"] == "active"


class TestTerminalAndMissing:
    @pytest.mark.asyncio
    async def test_unknown_task_is_noop(self, db_path):
        decision, bg = await handle_event("task-nope", "created")
        assert decision.action == "noop"
        assert bg is None

    @pytest.mark.asyncio
    async def test_terminal_task_is_noop(self, db_path):
        task = store.create_task("g")
        store.set_status(task["id"], "done")
        called = {"n": 0}

        async def _should_not_run(task_, thread_text):
            called["n"] += 1
            return Decision(action="note", why="x")

        decision, bg = await handle_event(task["id"], "worker_finished", decide_fn=_should_not_run)
        assert decision.action == "noop"
        assert called["n"] == 0  # never even asked for a decision


class TestGuards:
    @pytest.mark.asyncio
    async def test_circuit_breaker_trips_on_repeated_action(self, db_path):
        task = store.create_task("loop forever")

        async def _same(task_, thread_text):
            return Decision(action="noop", why="thinking")

        decision = None
        for _ in range(orchestrator.MAX_CONSECUTIVE_REPEATS + 3):
            decision, _ = await handle_event(task["id"], "tick", decide_fn=_same)
            if store.get_task(task["id"])["status"] == "failed":
                break

        assert store.get_task(task["id"])["status"] == "failed"
        assert decision.action == "fail"
        # tripped well before exhausting the much larger pass budget
        assert orchestrator._pass_count(task["id"]) <= orchestrator.MAX_CONSECUTIVE_REPEATS + 3

    @pytest.mark.asyncio
    async def test_pass_budget_exceeded_fails(self, db_path):
        task = store.create_task("many distinct passes")
        counter = {"n": 0}

        async def _alternating(task_, thread_text):
            # Alternate the action so the circuit breaker (which looks at
            # consecutive identical actions) never trips first — isolates
            # the pass-budget guard from the repeat guard.
            counter["n"] += 1
            action = "note" if counter["n"] % 2 else "noop"
            return Decision(action=action, why=f"pass {counter['n']}")

        decision = None
        for _ in range(orchestrator.MAX_PASSES + 1):
            decision, _ = await handle_event(task["id"], "tick", decide_fn=_alternating)

        assert store.get_task(task["id"])["status"] == "failed"
        assert decision.action == "fail"

    @pytest.mark.asyncio
    async def test_repeated_worker_failures_asks_human_instead_of_respawning_forever(self, db_path):
        """A broken runner/config (e.g. the plugin-hijack bug) makes every
        spawned worker crash with agent_no_result. The LLM varies its
        rationale each time it decides to retry, so this must be caught by
        watching what actually happened (worker_finished failures), not by
        the verbatim-repeat circuit breaker."""
        task = store.create_task("fix something")
        store.set_status(task["id"], "active")
        counter = {"n": 0}

        async def _keeps_retrying(task_, thread_text):
            counter["n"] += 1
            return Decision(action="spawn_worker", role="engineer", instruction="x",
                            why=f"retry attempt {counter['n']}")  # different every time

        async def fake_spawn(tid, role, instruction, repo_name=None, **kw):
            store.post_message(
                tid, "system", kind="worker_finished",
                text=f"{role} ended without reporting a result (status=failed): agent_no_result: ...",
            )

        with patch("core.orchestrator.spawn_worker", side_effect=fake_spawn):
            for _ in range(orchestrator.MAX_CONSECUTIVE_WORKER_FAILURES + 1):
                decision, bg = await handle_event(task["id"], "worker_finished", decide_fn=_keeps_retrying)
                if bg is not None:
                    await bg
                if store.get_task(task["id"])["status"] == "waiting":
                    break

        assert store.get_task(task["id"])["status"] == "waiting"
        assert decision.action == "ask_human"
        # never trips the (unrelated) pass budget to get there
        assert counter["n"] <= orchestrator.MAX_CONSECUTIVE_WORKER_FAILURES + 1
        questions = [m for m in store.get_thread(task["id"]) if m["kind"] == "question"]
        assert "agent_no_result" in questions[-1]["text"] or "broken" in questions[-1]["text"]

    @pytest.mark.asyncio
    async def test_worker_failures_reset_by_a_success(self, db_path):
        """A successful worker_finished in between failures resets the streak.
        Both success and failure post kind="worker_finished" (see
        web/agent_mcp.py::_end_worker) — only the text differs, so the guard
        must key on content, not just the message kind."""
        task = store.create_task("fix something")
        store.post_message(task["id"], "system", kind="worker_finished",
                           text="engineer ended without reporting a result: agent_no_result")
        store.post_message(task["id"], "system", kind="worker_finished",
                           text="reviewer ended without reporting a result: agent_no_result")
        store.post_message(task["id"], "system", kind="worker_finished", text="explorer finished")
        assert orchestrator._consecutive_worker_failures(task["id"]) == 0

    @pytest.mark.asyncio
    async def test_worker_failures_reset_by_a_human_answer(self, db_path):
        """A human answer after a failure streak resets it. The human has seen
        the failures and asked to continue (fixed the runner, pruned a stale
        worktree, ...) — without the reset, three stale failures re-trip the
        breaker on every reply and a parked task can never be resumed."""
        task = store.create_task("fix something")
        for _ in range(orchestrator.MAX_CONSECUTIVE_WORKER_FAILURES):
            store.post_message(task["id"], "system", kind="worker_finished",
                               text="engineer ended without reporting a result: agent_no_result")
        assert orchestrator._consecutive_worker_failures(task["id"]) >= \
            orchestrator.MAX_CONSECUTIVE_WORKER_FAILURES
        store.post_message(task["id"], "human", kind="answer",
                           text="Fixed the runner — try again.")
        assert orchestrator._consecutive_worker_failures(task["id"]) == 0


class TestRestartIsANoOp:
    @pytest.mark.asyncio
    async def test_reconcile_resumes_orphaned_active_task(self, db_path):
        task = store.create_task("do work")
        store.set_status(task["id"], "active")
        store.post_message(task["id"], "system", kind="worker_started", text="spawning engineer: do it")

        n = reconcile_on_boot()

        assert n == 1
        assert store.get_task(task["id"])["status"] == "active"  # NOT failed
        kinds = [m["kind"] for m in store.get_thread(task["id"])]
        assert kinds.count("worker_finished") == 1

    def test_reconcile_rescues_worker_that_posted_progress_notes(self, db_path):
        # The worker posted a progress note after worker_started, so the thread
        # TAIL is a note — but the worker itself died with the daemon. A bare
        # "continue" would deadlock: dispatch sees the unclosed worker_started
        # and waits forever for a report that will never come (live incident:
        # task-a1a96df8). Reconcile must close the orphaned worker instead.
        task = store.create_task("do work")
        store.set_status(task["id"], "active")
        store.post_message(task["id"], "system", kind="worker_started", text="spawning")
        store.post_message(task["id"], "explorer", kind="note", text="already working")

        with patch("core.orchestrator.emit") as mock_emit:
            n = reconcile_on_boot()

        assert n == 1
        mock_emit.assert_called_once_with(task["id"], "worker_finished")

        kinds = [m["kind"] for m in store.get_thread(task["id"])]
        assert kinds.count("worker_finished") == 1  # orphan closed on the thread

    @pytest.mark.asyncio
    async def test_reconcile_ignores_waiting_and_terminal_tasks(self, db_path):
        t1 = store.create_task("a")
        store.set_status(t1["id"], "waiting")
        t2 = store.create_task("b")
        store.set_status(t2["id"], "done")

        assert reconcile_on_boot() == 0

    @pytest.mark.asyncio
    async def test_reconcile_wakes_active_task_with_no_messages(self, db_path):
        # A task created just before a crash has no messages and no pending
        # event — reconcile must wake it, not skip it.
        store.create_task("brand new, orchestrator hasn't acted yet")
        assert reconcile_on_boot() == 1

    def test_reconcile_emits_continue_for_active_task_with_note(self, db_path):
        task = store.create_task("pending work")
        store.set_status(task["id"], "active")
        store.post_message(task["id"], "orchestrator", kind="note", text="just thinking")

        with patch("core.orchestrator.emit") as mock_emit:
            n = reconcile_on_boot()

        assert n == 1
        mock_emit.assert_called_once_with(task["id"], "continue")

    def test_reconcile_does_not_wake_waiting_task(self, db_path):
        task = store.create_task("parked")
        store.set_status(task["id"], "waiting")
        store.post_message(task["id"], "orchestrator", kind="question", text="approve?")

        with patch("core.orchestrator.emit") as mock_emit:
            n = reconcile_on_boot()

        assert n == 0
        mock_emit.assert_not_called()


class TestStallWatchdog:
    """reconcile_on_boot only runs at startup. A task created without an event
    used to wait for the next restart to move — one waited 2h13m. The watchdog
    catches the same condition while the daemon keeps running.
    """

    def _age(self, task_id, seconds):
        """Backdate updated_at, as if nothing had touched the task since."""
        import datetime as dt
        from core.db import get_conn
        old = (dt.datetime.now(dt.timezone.utc)
               - dt.timedelta(seconds=seconds)).isoformat()
        conn = get_conn()
        conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (old, task_id))
        conn.commit()
        conn.close()

    def test_rescues_a_task_created_without_an_event(self, db_path):
        task = store.create_task("auto-review that nobody dispatched")
        self._age(task["id"], 3600)

        with patch("core.orchestrator.emit") as mock_emit:
            n = orchestrator.rescue_stalled(stall_after=600)

        assert n == 1
        mock_emit.assert_called_once_with(task["id"], "continue")

    def test_leaves_a_recently_touched_task_alone(self, db_path):
        # freshly created: something may still be about to dispatch it
        store.create_task("just created")

        with patch("core.orchestrator.emit") as mock_emit:
            assert orchestrator.rescue_stalled(stall_after=600) == 0
        mock_emit.assert_not_called()

    def test_does_not_touch_a_task_whose_worker_is_running(self, db_path):
        """The important false positive: a worker can be quiet for a long time.
        Rescuing it would drive a second decision behind its back.

        A live worker means BOTH a thread marker and a session — the marker
        alone is what a killed worker leaves behind, which find_abandoned now
        (correctly) treats as dead.
        """
        sessions.clear()
        task = store.create_task("long build")
        store.post_message(task["id"], "system", kind="worker_started",
                           text="spawning engineer")
        sessions.mint(task["id"], "engineer")
        self._age(task["id"], 7200)

        with patch("core.orchestrator.emit") as mock_emit:
            assert orchestrator.rescue_stalled(stall_after=600) == 0
        mock_emit.assert_not_called()

    def test_rescues_once_the_worker_has_reported_back(self, db_path):
        task = store.create_task("worker done, then silence")
        store.post_message(task["id"], "system", kind="worker_started", text="go")
        store.post_message(task["id"], "system", kind="worker_finished", text="done")
        self._age(task["id"], 3600)

        with patch("core.orchestrator.emit") as mock_emit:
            assert orchestrator.rescue_stalled(stall_after=600) == 1
        mock_emit.assert_called_once_with(task["id"], "continue")

    def test_a_killed_worker_stops_blocking_its_task(self, db_path):
        """Killing the subprocess is the only way to cancel today, and it leaves
        worker_started with no worker_finished — so _worker_in_flight stays true
        and every later pass is a no-op. The task became unrecoverable without a
        daemon restart; this happened for real while cancelling one.
        """
        sessions.clear()
        task = store.create_task("was cancelled by hand")
        store.post_message(task["id"], "system", kind="worker_started", text="go")
        self._age(task["id"], 3600)          # no session was ever minted

        with patch("core.orchestrator.emit") as mock_emit:
            assert orchestrator.rescue_stalled(stall_after=600) == 1

        assert not orchestrator._worker_in_flight(task["id"])
        mock_emit.assert_called_once_with(task["id"], "worker_finished")

    def test_a_slow_but_live_worker_is_left_alone(self, db_path):
        """The false positive that matters: a Gradle build can be silent for far
        longer than the stall threshold. Elapsed time cannot tell it apart from
        a killed worker — the live session can."""
        sessions.clear()
        task = store.create_task("running a long android build")
        store.post_message(task["id"], "system", kind="worker_started", text="go")
        sessions.mint(task["id"], "engineer")
        self._age(task["id"], 7200)

        with patch("core.orchestrator.emit") as mock_emit:
            assert orchestrator.rescue_stalled(stall_after=600) == 0
        mock_emit.assert_not_called()
        assert orchestrator._worker_in_flight(task["id"])

    def test_a_human_reply_may_resume_a_parked_task(self, db_path):
        """task_reply emits `human_replied` WITHOUT setting the status back to
        active, so an event-driven pass must accept a `waiting` task. Blocking on
        status here would kill every human-answers-a-question resume — which a
        naive single-predicate refactor does.
        """
        task = store.create_task("parked on a question")
        store.post_message(task["id"], "orchestrator", kind="question", text="which branch?")
        store.set_status(task["id"], "waiting")
        store.post_message(task["id"], "human", kind="answer", text="main")

        assert orchestrator.pass_blocked_by(store.get_task(task["id"])) is None

    def test_a_scan_must_not_wake_a_parked_task_on_its_own(self, db_path):
        """The other half: parked means parked. Only an explicit event resumes."""
        task = store.create_task("parked on a question")
        store.set_status(task["id"], "waiting")

        blocked = orchestrator.pass_blocked_by(store.get_task(task["id"]),
                                               spontaneous=True)
        assert blocked and "parked" in blocked

    def test_a_parent_awaiting_children_is_not_stalled(self, db_path):
        """find_stalled used to skip the children check that handle_event had, so
        a parent mid-fan-out could be reported as a fault and logged as one."""
        parent = store.create_task("fan out across repos")
        store.create_task("child work", parent_id=parent["id"])
        self._age(parent["id"], 7200)

        with patch("core.orchestrator.emit") as mock_emit:
            assert orchestrator.rescue_stalled(stall_after=600) == 0
        mock_emit.assert_not_called()

    def test_ignores_waiting_and_terminal_tasks(self, db_path):
        for status in ("waiting", "done", "failed"):
            t = store.create_task(f"parked in {status}")
            store.set_status(t["id"], status)
            self._age(t["id"], 7200)

        with patch("core.orchestrator.emit") as mock_emit:
            assert orchestrator.rescue_stalled(stall_after=600) == 0
        mock_emit.assert_not_called()


class TestDecisionValidation:
    @pytest.mark.asyncio
    async def test_malformed_decision_fails_the_task_instead_of_crashing(self, db_path):
        task = store.create_task("g")

        async def _garbage(task_, thread_text):
            from pydantic import ValidationError
            try:
                return Decision(action="fly_to_the_moon", why="oops")
            except ValidationError as exc:
                return Decision(action="fail", reason=str(exc), why="malformed")

        decision, bg = await handle_event(task["id"], "created", decide_fn=_garbage)
        assert decision.action == "fail"
        assert store.get_task(task["id"])["status"] == "failed"


class TestWorkerInFlightGuard:
    @pytest.mark.asyncio
    async def test_event_during_active_worker_does_not_spawn_a_second(self, db_path):
        """A worker is mid-run (worker_started, no worker_finished yet). A
        human_replied event must NOT trigger a decision/second spawn."""
        task = store.create_task("x", repo="sisyphus")
        store.set_status(task["id"], "active")
        store.post_message(task["id"], "system", kind="worker_started", text="spawning engineer")
        store.post_message(task["id"], "human", kind="answer", text="one more thing")  # reply arrives mid-run

        called = {"n": 0}

        async def _decide(task_, thread_text):
            called["n"] += 1
            return Decision(action="spawn_worker", role="engineer", instruction="y", why="z")

        decision, bg = await handle_event(task["id"], "human_replied", decide_fn=_decide)
        assert decision.action == "noop"
        assert bg is None
        assert called["n"] == 0  # never even asked for a decision

    @pytest.mark.asyncio
    async def test_worker_finished_clears_in_flight_and_proceeds(self, db_path):
        task = store.create_task("x", repo="sisyphus")
        store.set_status(task["id"], "active")
        store.post_message(task["id"], "system", kind="worker_started", text="spawning explorer")
        store.post_message(task["id"], "explorer", kind="result", text="found it")
        store.post_message(task["id"], "system", kind="worker_finished", text="explorer finished")

        decisions = _scripted([Decision(action="ask_human", question="ok?", why="check")])
        decision, bg = await handle_event(task["id"], "worker_finished", decide_fn=decisions)
        assert decision.action == "ask_human"  # not blocked — worker is done


class TestDoneRequiresReview:
    @pytest.mark.asyncio
    async def test_done_without_approval_is_converted_to_reviewer(self, db_path):
        task = store.create_task("ship it", repo="sisyphus")
        store.set_status(task["id"], "active")
        store.post_message(task["id"], "engineer", kind="result", text="implemented")

        async def fake_spawn(tid, role, instruction, repo_name=None, **kw):
            store.post_message(tid, role, kind="worker_started", text="spawning")

        decisions = _scripted([Decision(action="done", why="looks done to me")])
        with patch("core.orchestrator.spawn_worker", side_effect=fake_spawn):
            decision, bg = await handle_event(task["id"], "worker_finished", decide_fn=decisions)
            if bg:
                await bg
        assert decision.action == "spawn_worker"
        assert decision.role == "reviewer"
        assert store.get_task(task["id"])["status"] == "active"  # NOT done

    @pytest.mark.asyncio
    async def test_done_with_approval_is_allowed(self, db_path):
        task = store.create_task("ship it", repo="sisyphus")
        store.set_status(task["id"], "active")
        store.post_message(task["id"], "reviewer", kind="result", text="APPROVED: all good")

        decisions = _scripted([Decision(action="done", why="reviewer approved")])
        decision, bg = await handle_event(task["id"], "worker_finished", decide_fn=decisions)
        assert decision.action == "done"
        assert store.get_task(task["id"])["status"] == "done"

    @pytest.mark.asyncio
    async def test_approved_with_commit_but_no_mr_spawns_engineer(self, db_path):
        # SIS-114 regression: approved + committed + no merge request must NOT
        # go done — the commits live only on the task branch, and a done hands
        # it to cleanup, which deletes that branch's clone when no MR is open.
        task = store.create_task("fix the login bug", repo="sisyphus")
        store.set_status(task["id"], "active")
        store.post_message(task["id"], "reviewer", kind="result", text="APPROVED: all good")
        store.post_message(task["id"], "engineer", kind="commit",
                           text="fix: the login bug (abc1234)")

        async def fake_spawn(tid, role, instruction, repo_name=None, **kw):
            store.post_message(tid, role, kind="worker_started", text="spawning")

        decisions = _scripted([Decision(action="done", why="reviewer approved")])
        with patch("core.orchestrator.spawn_worker", side_effect=fake_spawn):
            decision, bg = await handle_event(task["id"], "worker_finished", decide_fn=decisions)
            if bg:
                await bg
        assert decision.action == "spawn_worker"
        assert decision.role == "engineer"
        assert "open_mr" in decision.instruction
        assert "do not change the code" in decision.instruction
        assert store.get_task(task["id"])["status"] == "active"  # NOT done

    @pytest.mark.asyncio
    async def test_approved_with_commit_and_open_mr_is_done(self, db_path):
        # A committed task whose MR exists completes exactly as before — the
        # gate only fires when the merge request is missing.
        from core import mergerequests
        task = store.create_task("fix the login bug", repo="sisyphus")
        store.set_status(task["id"], "active")
        store.post_message(task["id"], "reviewer", kind="result", text="APPROVED: all good")
        store.post_message(task["id"], "engineer", kind="commit",
                           text="fix: the login bug (abc1234)")
        from core.git_ops import task_branch
        mergerequests.create_mr(
            task_id=task["id"], repo="sisyphus",
            source_branch=task_branch(task["id"]), target_branch="main",
            title="fix login bug",
        )

        decisions = _scripted([Decision(action="done", why="reviewer approved")])
        decision, bg = await handle_event(task["id"], "worker_finished", decide_fn=decisions)
        assert decision.action == "done"
        assert store.get_task(task["id"])["status"] == "done"

    @pytest.mark.asyncio
    async def test_approved_answer_with_no_commits_is_done(self, db_path):
        # An answer task commits nothing — no MR is required, and it completes
        # exactly as it did before the committed-work gate.
        task = store.create_task("answer a question", repo="sisyphus")
        store.set_status(task["id"], "active")
        store.post_message(task["id"], "reviewer", kind="result", text="APPROVED: the answer is right")

        decisions = _scripted([Decision(action="done", why="reviewer approved")])
        decision, bg = await handle_event(task["id"], "worker_finished", decide_fn=decisions)
        assert decision.action == "done"
        assert store.get_task(task["id"])["status"] == "done"

    @pytest.mark.asyncio
    async def test_done_with_mixed_case_approval_is_allowed(self, db_path):
        # Regression (task-ec2ef73b): a reviewer whose only result is the
        # mixed-case "Approved." must still unblock 'done' — never re-spawn a
        # reviewer behind an already-approved thread.
        task = store.create_task("ship it", repo="sisyphus")
        store.set_status(task["id"], "active")
        store.post_message(task["id"], "reviewer", kind="result", text="Approved.")

        decisions = _scripted([Decision(action="done", why="reviewer approved")])
        decision, bg = await handle_event(task["id"], "worker_finished", decide_fn=decisions)
        assert decision.action == "done"
        assert decision.role != "reviewer"
        assert store.get_task(task["id"])["status"] == "done"


class TestReviewerApprovedPredicate:
    def _post_review(self, text):
        task = store.create_task("review me", repo="sisyphus")
        store.post_message(task["id"], "reviewer", kind="result", text=text)
        return task["id"]

    @pytest.mark.parametrize("text", [
        "APPROVED: LGTM",
        "Approved. The work is correct",
        "approved — ship it",
        "  APPROVED",
    ])
    def test_approval_prefix_matches(self, db_path, text):
        assert orchestrator._reviewer_approved(self._post_review(text))

    @pytest.mark.parametrize("text", [
        "CHANGES REQUESTED: x",
        "NOT APPROVED",
        "I would have approved this",
    ])
    def test_non_approval_does_not_match(self, db_path, text):
        assert not orchestrator._reviewer_approved(self._post_review(text))


class TestNoStall:
    @pytest.mark.asyncio
    async def test_note_re_emits_a_continue_event(self, db_path):
        """A note leaves the task active — the loop must re-emit so it isn't
        silently stuck with nothing to wake it."""
        task = store.create_task("think", repo="sisyphus")
        store.set_status(task["id"], "active")

        decisions = _scripted([Decision(action="note", why="just thinking")])
        await handle_event(task["id"], "created", decide_fn=decisions)

        q = orchestrator._get_queue()
        assert q.qsize() == 1
        ev = q.get_nowait()
        assert ev.task_id == task["id"] and ev.type == "continue"

    @pytest.mark.asyncio
    async def test_park_on_error_moves_active_task_to_waiting(self, db_path):
        task = store.create_task("boom", repo="sisyphus")
        store.set_status(task["id"], "active")

        orchestrator._park_on_error(task["id"])

        assert store.get_task(task["id"])["status"] == "waiting"
        kinds = [m["kind"] for m in store.get_thread(task["id"])]
        assert "question" in kinds  # resumable by a human reply


class TestTerminalCleanup:
    @pytest.mark.asyncio
    async def test_done_reclaims_disk(self, db_path):
        task = store.create_task("x", repo="sisyphus")
        store.set_status(task["id"], "active")
        store.post_message(task["id"], "reviewer", kind="result", text="APPROVED: ok")

        decisions = _scripted([Decision(action="done", why="approved")])
        with patch("core.worker.cleanup_task") as mock_cleanup:
            await handle_event(task["id"], "worker_finished", decide_fn=decisions)
        mock_cleanup.assert_called_once_with(task["id"])


class TestFanout:
    @pytest.mark.asyncio
    async def test_spawn_task_creates_children_and_emits(self, db_path):
        from core.orchestrator import Subtask

        parent = store.create_task("ticket PROJ-123")  # repo-less coordinator
        decisions = _scripted([Decision(
            action="spawn_task",
            subtasks=[Subtask(repo="ios-app", goal="iOS impl"),
                      Subtask(repo="android-app", goal="Android impl")],
            why="fan out to both platforms",
        )])
        await handle_event(parent["id"], "worker_finished", decide_fn=decisions)

        kids = store.list_children(parent["id"])
        assert {c["repo"] for c in kids} == {"ios-app", "android-app"}
        assert all(c["parent_id"] == parent["id"] for c in kids)
        # a "created" event was queued for each child
        assert orchestrator._get_queue().qsize() == 2

    @pytest.mark.asyncio
    async def test_parent_waits_while_a_child_is_pending(self, db_path):
        parent = store.create_task("ticket")
        store.create_task("iOS", repo="ios-app", parent_id=parent["id"])   # active
        done_child = store.create_task("Android", repo="android-app", parent_id=parent["id"])
        store.set_status(done_child["id"], "done")

        async def _should_not_run(task_, thread_text):
            raise AssertionError("decide must not run while a child is pending")

        decision, bg = await handle_event(parent["id"], "child_finished", decide_fn=_should_not_run)
        assert decision.action == "noop"

    @pytest.mark.asyncio
    async def test_child_termination_rolls_up_to_parent(self, db_path):
        parent = store.create_task("ticket")
        child = store.create_task("iOS", repo="ios-app", parent_id=parent["id"])
        store.set_status(child["id"], "active")

        with patch("core.worker.cleanup_task"):
            await orchestrator._terminate(child["id"], "done")

        rolled = [m for m in store.get_thread(parent["id"]) if m["kind"] == "child_finished"]
        assert rolled and "[done]" in rolled[0]["text"]
        assert orchestrator._get_queue().qsize() == 1   # parent woken

    @pytest.mark.asyncio
    async def test_parent_done_when_all_children_done_without_reviewer(self, db_path):
        parent = store.create_task("ticket")
        for repo in ("ios-app", "android-app"):
            c = store.create_task(repo, repo=repo, parent_id=parent["id"])
            store.set_status(c["id"], "done")
        store.set_status(parent["id"], "active")

        decisions = _scripted([Decision(action="done", why="both platforms shipped")])
        with patch("core.worker.cleanup_task"):
            decision, bg = await handle_event(parent["id"], "child_finished", decide_fn=decisions)

        # done was NOT overridden into a reviewer spawn (children gate it)
        assert decision.action == "done"
        assert store.get_task(parent["id"])["status"] == "done"

    @pytest.mark.asyncio
    async def test_leaf_task_still_needs_reviewer_for_done(self, db_path):
        """A task with no children keeps the reviewer-approval gate."""
        task = store.create_task("code task", repo="sisyphus")
        store.set_status(task["id"], "active")

        decisions = _scripted([Decision(action="done", why="looks done to me")])
        decision, bg = await handle_event(task["id"], "worker_finished", decide_fn=decisions)
        # no reviewer approval on the thread → overridden into a reviewer spawn
        assert decision.action == "spawn_worker"
        assert decision.role == "reviewer"
        if bg:
            bg.cancel()


class TestDecideViaAgent:
    @pytest.mark.asyncio
    async def test_decide_translates_recorded_decision(self, db_path):
        from unittest.mock import AsyncMock
        from core.orchestrator import _decide
        task = store.create_task("g", repo="sisyphus")
        with patch("core.worker.spawn_decider",
                   AsyncMock(return_value={"action": "note", "why": "thinking"})):
            d = await _decide(task, "thread")
        assert d.action == "note" and d.why == "thinking"

    @pytest.mark.asyncio
    async def test_decide_falls_back_to_ask_human_when_no_decision(self, db_path):
        from unittest.mock import AsyncMock
        from core.orchestrator import DECIDER_ATTEMPTS, _decide
        task = store.create_task("g", repo="sisyphus")
        mock = AsyncMock(return_value=None)
        with patch("core.worker.spawn_decider", mock):
            d = await _decide(task, "thread")
        assert d.action == "ask_human"
        assert mock.await_count == DECIDER_ATTEMPTS, \
            "an empty decider run must be retried before parking the task"

    @pytest.mark.asyncio
    async def test_one_empty_run_is_retried_not_parked(self, db_path):
        """The failure this guards: a single flaky model response sent the task
        straight to waiting, where it sat for days until a human noticed."""
        from unittest.mock import AsyncMock
        from core.orchestrator import _decide
        task = store.create_task("g", repo="sisyphus")
        mock = AsyncMock(side_effect=[None, {"action": "note", "why": "recovered"}])
        with patch("core.worker.spawn_decider", mock):
            d = await _decide(task, "thread")
        assert d.action == "note" and d.why == "recovered"
        assert mock.await_count == 2

    @pytest.mark.asyncio
    async def test_malformed_then_valid_recovers(self, db_path):
        from unittest.mock import AsyncMock
        from core.orchestrator import _decide
        task = store.create_task("g", repo="sisyphus")
        mock = AsyncMock(side_effect=[{"action": "nonsense-action"},
                                      {"action": "note", "why": "ok now"}])
        with patch("core.worker.spawn_decider", mock):
            d = await _decide(task, "thread")
        assert d.action == "note" and d.why == "ok now"

    @pytest.mark.asyncio
    async def test_persistently_malformed_still_fails(self, db_path):
        from unittest.mock import AsyncMock
        from core.orchestrator import DECIDER_ATTEMPTS, _decide
        task = store.create_task("g", repo="sisyphus")
        mock = AsyncMock(return_value={"action": "nonsense-action"})
        with patch("core.worker.spawn_decider", mock):
            d = await _decide(task, "thread")
        assert d.action == "fail"
        assert mock.await_count == DECIDER_ATTEMPTS

    def test_no_raw_model_http_in_orchestrator(self):
        import inspect
        import core.orchestrator as o
        src = inspect.getsource(o)
        assert "urllib" not in src
        assert "chat/completions" not in src


class TestSingleAgentMode:
    @pytest.mark.asyncio
    async def test_pinned_agent_runs_without_orchestrator(self, db_path):
        task = store.create_task("do it solo", repo="sisyphus", agent="engineer")

        async def _decide_must_not_run(task_, thread_text):
            raise AssertionError("orchestrator _decide must not run for a pinned agent")

        async def fake_spawn(tid, agent, instruction, repo_name=None, **kw):
            store.post_message(tid, agent, kind="worker_started", text="spawning")

        with patch("core.orchestrator.spawn_worker", side_effect=fake_spawn):
            decision, bg = await handle_event(task["id"], "created", decide_fn=_decide_must_not_run)
            if bg:
                await bg
        assert decision.action == "spawn_worker"
        assert decision.role == "engineer"

    @pytest.mark.asyncio
    async def test_result_completes_the_task(self, db_path):
        task = store.create_task("solo", repo="sisyphus", agent="engineer")
        store.post_message(task["id"], "engineer", kind="worker_started", text="spawning")
        store.post_message(task["id"], "engineer", kind="result", text="did it")
        store.post_message(task["id"], "system", kind="worker_finished", text="engineer finished")

        with patch("core.worker.cleanup_task"):
            decision, bg = await handle_event(task["id"], "worker_finished")
        assert decision.action == "done"
        assert store.get_task(task["id"])["status"] == "done"

    @pytest.mark.asyncio
    async def test_no_result_costs_one_more_run_then_fails(self, db_path):
        """An empty run buys a retry, not a verdict — but only one. See
        tests/test_task_lifecycle.py for the ledger this came from."""
        task = store.create_task("solo", repo="sisyphus", agent="engineer")
        store.post_message(task["id"], "engineer", kind="worker_started", text="spawning")
        store.post_message(task["id"], "system", kind="worker_finished",
                           text="engineer ended without reporting a result")

        with patch("core.orchestrator._spawn_worker_safe", return_value=None):
            decision, bg = await handle_event(task["id"], "worker_finished")
        assert decision.action == "spawn_worker"
        assert store.get_task(task["id"])["status"] != "failed"

        store.post_message(task["id"], "system", kind="worker_finished",
                           text="engineer ended without reporting a result")
        with patch("core.worker.cleanup_task"):
            decision, bg = await handle_event(task["id"], "worker_finished")
        assert decision.action == "fail"
        assert store.get_task(task["id"])["status"] == "failed"

    @pytest.mark.asyncio
    async def test_review_of_mr_death_records_on_mr_and_tells_origin(self, db_path):
        """A review-of-MR task whose runs all come back empty must, when it
        finally fails, record the death on the MR row and tell the origin task
        — not leave the MR 'pending' forever (mr-f7913881)."""
        from core import mergerequests, reviews
        origin = store.create_task("do some work", repo="sisyphus", agent="engineer")
        mr = mergerequests.create_mr(task_id=origin["id"], repo="sisyphus",
                                     source_branch="feature/x", target_branch="main",
                                     title="Test MR")
        mergerequests.set_review_status(mr["id"], "pending")
        review = reviews.ensure_review_task(mr)
        assert review is not None
        assert review["agent"] == "mr-reviewer"

        for _ in range(orchestrator.SINGLE_AGENT_ATTEMPTS):
            store.post_message(review["id"], "mr-reviewer", kind="worker_started",
                               text="spawning")
            store.post_message(review["id"], "system", kind="worker_finished",
                               text="mr-reviewer ended without reporting a result")
            with patch("core.orchestrator._spawn_worker_safe", return_value=None), \
                    patch("core.worker.cleanup_task"):
                decision, bg = await handle_event(review["id"], "worker_finished")

        assert decision.action == "fail"
        assert store.get_task(review["id"])["status"] == "failed"
        got = mergerequests.get_mr(mr["id"])
        assert got["review_status"] == "died"
        assert mergerequests.merge_block_reason(got) is not None
        origin_msgs = store.get_thread(origin["id"])
        assert any("review" in m["text"] and "died" in m["text"] for m in origin_msgs)


class TestSpawnFailureNeverStalls:
    @pytest.mark.asyncio
    async def test_unknown_agent_wakes_task_instead_of_hanging(self, db_path):
        """The orchestrator hallucinating an agent name must NOT leave the task
        stalled 'active' forever (regression: 'unknown agent hephaestus')."""
        task = store.create_task("g", repo="sisyphus")
        store.set_status(task["id"], "active")
        decisions = _scripted([Decision(action="spawn_worker", role="hephaestus",
                                        instruction="x", why="bogus agent")])
        decision, bg = await handle_event(task["id"], "created", decide_fn=decisions)
        assert decision.action == "spawn_worker"
        await bg   # the safe wrapper runs the real spawn_worker → raises → recovers
        kinds_text = [(m["kind"], m["text"]) for m in store.get_thread(task["id"])]
        assert any(k == "worker_finished" and "failed to start" in t for k, t in kinds_text)
        assert orchestrator._get_queue().qsize() >= 1   # an event was queued to re-decide


class TestQueuedWorkerVisibility:
    def test_trailing_worker_queued_counts_as_in_flight(self, db_path):
        """A spawn waiting for a slot must block duplicate spawns — the
        invisible-queue failure: deciders saw 'not in flight' and piled up
        phantom spawn decisions while other tasks held every slot."""
        task = store.create_task("g")
        store.post_message(task["id"], "system", kind="worker_queued",
                           text="engineer queued — waiting for a free worker slot")
        assert orchestrator._worker_in_flight(task["id"]) is True
        store.post_message(task["id"], "system", kind="worker_finished", text="engineer finished")
        assert orchestrator._worker_in_flight(task["id"]) is False

    @pytest.mark.asyncio
    async def test_reconcile_rescues_trailing_worker_queued(self, db_path):
        task = store.create_task("g")
        store.set_status(task["id"], "active")
        store.post_message(task["id"], "system", kind="worker_queued",
                           text="engineer queued — waiting for a free worker slot")
        assert reconcile_on_boot() == 1
        assert store.get_thread(task["id"])[-1]["kind"] == "worker_finished"

    @pytest.mark.asyncio
    async def test_spawn_safe_posts_queued_marker_before_worker_runs(self, db_path):
        task = store.create_task("g")

        async def fake_spawn(tid, role, instruction, repo_name=None, **kw):
            store.post_message(tid, "system", kind="worker_started", text=f"spawning {role}: x")

        with patch("core.orchestrator.spawn_worker", side_effect=fake_spawn):
            await orchestrator._spawn_worker_safe(task["id"], "engineer", "x", None)

        kinds = [m["kind"] for m in store.get_thread(task["id"])]
        assert kinds == ["worker_queued", "worker_started"]


class TestOneReviewPerBranch:
    """A merge request gets its own review task (core.reviews) the moment it is
    opened. The orchestrator used to read none of that and spawned a second
    reviewer of its own — two agents on one branch, the second re-running the
    whole suite because its prompt carried no record of the daemon's check.
    Live cost on task-20504ac8: four minutes, most of them after the merge."""

    def _mr_task(self, approved=None):
        from core import mergerequests
        task = store.create_task("ship it", repo="sisyphus")
        store.set_status(task["id"], "active")
        mr = mergerequests.create_mr(task["id"], "sisyphus", "sisyphus/t", "main", "ship it")
        if approved:
            mergerequests.set_review_status(mr["id"], approved)
        return task, mr

    @pytest.mark.asyncio
    async def test_the_branchs_own_approval_counts_as_the_approval(self, db_path):
        task, mr = self._mr_task(approved="approved")
        decisions = _scripted([Decision(action="done", why="the review approved it")])
        decision, bg = await handle_event(task["id"], "external", decide_fn=decisions)
        assert decision.action == "done"
        assert store.get_task(task["id"])["status"] == "done"

    @pytest.mark.asyncio
    async def test_a_running_review_is_waited_for_not_doubled(self, db_path):
        from core import reviews
        task, mr = self._mr_task()
        review = reviews.ensure_review_task(mr)
        assert review is not None
        called = []

        async def never(*a, **kw):
            called.append(a)
            return Decision(action="done", why="should not be asked")

        decision, bg = await handle_event(task["id"], "worker_finished", decide_fn=never)
        assert decision.action == "noop" and not called      # no pass was paid for
        assert store.get_task(task["id"])["status"] == "active"

    @pytest.mark.asyncio
    async def test_a_verdict_wakes_the_task_that_made_the_branch(self, db_path):
        from unittest.mock import patch as _patch
        from web import agent_mcp
        task, mr = self._mr_task()
        review = agent_mcp.reviews.ensure_review_task(mr)
        session = {"task_id": review["id"], "role": "mr-reviewer"}
        with _patch("core.orchestrator.emit") as emit_:
            with _patch("web.agent_mcp._end_worker"):
                agent_mcp._approve(session, summary="looks right")
        assert (task["id"], "external") in [c.args for c in emit_.call_args_list]


class TestAQuestionIsAnswered:
    @pytest.mark.asyncio
    async def test_an_answer_finishes_the_task_without_a_reviewer(self, db_path):
        task = store.create_task("how do I add a new CLI runner?", repo="sisyphus")
        store.set_status(task["id"], "active")
        decisions = _scripted([Decision(action="answer", text="Add an entry to runner_profiles.py.",
                                        why="the goal asks a question")])
        decision, bg = await handle_event(task["id"], "worker_finished", decide_fn=decisions)
        assert decision.action == "answer"
        thread = store.get_thread(task["id"])
        assert thread[-1]["kind"] == "result" and "runner_profiles" in thread[-1]["text"]
        assert store.get_task(task["id"])["status"] == "done"

    @pytest.mark.asyncio
    async def test_work_that_shipped_is_reviewed_even_if_it_answers(self, db_path):
        # Committed work is never finished by answering — but with no MR open it
        # first goes back to the engineer to open one (SIS-114), then a
        # reviewer. Answering is not a way around the gate.
        task = store.create_task("how do I add a runner? write it down", repo="sisyphus")
        store.set_status(task["id"], "active")
        store.post_message(task["id"], "engineer", kind="commit", text="docs: a guide (abc1234)")

        async def fake_spawn(tid, role, instruction, repo_name=None, **kw):
            store.post_message(tid, role, kind="worker_started", text="spawning")

        decisions = _scripted([Decision(action="answer", text="See the new guide.", why="…")])
        with patch("core.orchestrator.spawn_worker", side_effect=fake_spawn):
            decision, bg = await handle_event(task["id"], "worker_finished", decide_fn=decisions)
            if bg:
                await bg
        assert decision.action == "spawn_worker" and decision.role == "engineer"
        assert "open_mr" in decision.instruction


class TestWorkNobodyNeedsIsStopped:
    @pytest.mark.asyncio
    async def test_a_worker_still_running_is_stopped_when_the_task_ends(self, db_path):
        from core import cancellation, worker
        cancellation.clear()
        task = store.create_task("ship it", repo="sisyphus")
        store.set_status(task["id"], "active")
        event = cancellation.register(task["id"], "reviewer")

        await orchestrator._terminate(task["id"], "done")

        assert event.is_set()
        assert "stopped 1 worker" in store.get_thread(task["id"])[-1]["text"]
        # …and the thread says who stopped it, not "a human".
        line = worker.worker_end_message("reviewer", type("R", (), {"status": "cancelled"})(), task["id"])
        assert "the task is done" in line and worker.STOPPED_MARKER not in line
        cancellation.clear()
