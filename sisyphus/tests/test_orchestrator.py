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

        decisions = _scripted([
            Decision(action="spawn_worker", role="engineer", instruction="fix it", why="start with a fix"),
            Decision(action="spawn_worker", role="reviewer", instruction="review the fix", why="check quality"),
            Decision(action="spawn_worker", role="engineer", instruction="address feedback", why="changes requested"),
            Decision(action="spawn_worker", role="reviewer", instruction="re-review", why="check again"),
            Decision(action="done", why="approved"),
        ])

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

    @pytest.mark.asyncio
    async def test_reconcile_ignores_tasks_with_progress_since(self, db_path):
        task = store.create_task("do work")
        store.set_status(task["id"], "active")
        store.post_message(task["id"], "system", kind="worker_started", text="spawning")
        store.post_message(task["id"], "explorer", kind="note", text="already working")

        assert reconcile_on_boot() == 0

    @pytest.mark.asyncio
    async def test_reconcile_ignores_waiting_and_terminal_tasks(self, db_path):
        t1 = store.create_task("a")
        store.set_status(t1["id"], "waiting")
        t2 = store.create_task("b")
        store.set_status(t2["id"], "done")

        assert reconcile_on_boot() == 0

    @pytest.mark.asyncio
    async def test_reconcile_ignores_active_task_with_no_messages(self, db_path):
        store.create_task("brand new, orchestrator hasn't acted yet")
        assert reconcile_on_boot() == 0


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
            orchestrator._terminate(child["id"], "done")

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
        from core.orchestrator import _decide
        task = store.create_task("g", repo="sisyphus")
        with patch("core.worker.spawn_decider", AsyncMock(return_value=None)):
            d = await _decide(task, "thread")
        assert d.action == "ask_human"

    def test_no_raw_model_http_in_orchestrator(self):
        import inspect
        import core.orchestrator as o
        src = inspect.getsource(o)
        assert "urllib" not in src
        assert "chat/completions" not in src


class TestSingleAgentMode:
    @pytest.mark.asyncio
    async def test_pinned_agent_runs_without_orchestrator(self, db_path):
        from unittest.mock import AsyncMock
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
    async def test_no_result_fails_the_task(self, db_path):
        task = store.create_task("solo", repo="sisyphus", agent="engineer")
        store.post_message(task["id"], "engineer", kind="worker_started", text="spawning")
        store.post_message(task["id"], "system", kind="worker_finished",
                           text="engineer ended without reporting a result")

        with patch("core.worker.cleanup_task"):
            decision, bg = await handle_event(task["id"], "worker_finished")
        assert decision.action == "fail"
        assert store.get_task(task["id"])["status"] == "failed"


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
