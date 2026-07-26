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
            Decision(action="done", why="human said yes"),
        ])

        decision, bg = await handle_event(task_id, "created", decide_fn=decisions)
        assert decision.action == "ask_human"
        assert bg is None
        assert store.get_task(task_id)["status"] == "waiting"
        questions = [m for m in store.get_thread(task_id) if m["kind"] == "question"]
        assert questions[-1]["text"] == "Should I proceed?"

        # Human replies in the same thread — the future web surface posts this
        # message and calls emit(task_id, "human_replied"); we simulate both.
        store.post_message(task_id, "human", kind="answer", text="yes, go ahead")

        decision, bg = await handle_event(task_id, "human_replied", decide_fn=decisions)
        assert decision.action == "done"
        assert store.get_task(task_id)["status"] == "done"


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
