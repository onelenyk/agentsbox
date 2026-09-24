"""Tests for obvious step rules — mechanical decisions without model calls.

These tests verify that the orchestrator avoids calling the LLM for unambiguous
thread states: spawning a reviewer after an engineer delivers an MR, marking done
when a reviewer approves and the check passes, and spawning an engineer when a
reviewer requests changes.
"""
import pytest
from unittest.mock import patch

from core import orchestrator, sessions, store, mergerequests
from core.orchestrator import Decision, handle_event


@pytest.fixture(autouse=True)
def _reset():
    orchestrator.reset_queue()
    sessions.clear()
    yield
    orchestrator.reset_queue()
    sessions.clear()


def _make_failing_decider():
    """A decide_fn that fails the test if called — proof that the model was not asked."""
    async def _fn(task, thread_text):
        raise AssertionError("Model should not have been called (obvious rule should have fired)")
    return _fn


class TestEngineerResultNeedsReview:
    """Case A: spawn_worker(role="reviewer") when engineer delivered MR, no review since."""

    @pytest.mark.asyncio
    async def test_engineer_result_with_mr_spawns_reviewer(self, db_path):
        """Engineer result + MR exists + no reviewer since → spawn reviewer."""
        task = store.create_task("fix something", repo="sisyphus")
        task_id = task["id"]

        # Engineer posts a result.
        store.post_message(task_id, "engineer", kind="result", text="done: added fix")

        # Create an MR for this task.
        mr = mergerequests.create_mr(
            task_id=task_id,
            repo="sisyphus",
            source_branch="sisyphus/test",
            target_branch="main",
            source_path="/tmp/repo",
            bare_path="/tmp/bare"
        )
        assert mr is not None

        # The pass should spawn a reviewer without calling the model.
        decision, _ = await handle_event(task_id, "worker_finished", decide_fn=_make_failing_decider())

        assert decision.action == "spawn_worker"
        assert decision.role == "reviewer"
        assert "rule:" in decision.why
        assert "engineer delivered an MR" in decision.why

    @pytest.mark.asyncio
    async def test_engineer_result_without_mr_asks_model(self, db_path):
        """Engineer result but no MR → ask the model."""
        task = store.create_task("fix something", repo="sisyphus")
        task_id = task["id"]

        # Engineer posts a result but no MR exists.
        store.post_message(task_id, "engineer", kind="result", text="done: added fix")

        # The pass should ask the model (no MR to review).
        called = []
        async def tracking_decider(t, thread_text):
            called.append(True)
            return Decision(action="done", why="model decided")

        decision, _ = await handle_event(task_id, "worker_finished", decide_fn=tracking_decider)
        assert called, "Model should have been called"

    @pytest.mark.asyncio
    async def test_reviewer_already_ran_asks_model(self, db_path):
        """Engineer result + MR + reviewer already ran → ask the model."""
        task = store.create_task("fix something", repo="sisyphus")
        task_id = task["id"]

        # Engineer posts a result.
        store.post_message(task_id, "engineer", kind="result", text="done: added fix")

        # Create an MR.
        mr = mergerequests.create_mr(
            task_id=task_id,
            repo="sisyphus",
            source_branch="sisyphus/test",
            target_branch="main",
            source_path="/tmp/repo",
            bare_path="/tmp/bare"
        )

        # Reviewer already posted a result.
        store.post_message(task_id, "reviewer", kind="result", text="APPROVED")

        # The pass should ask the model (reviewer already ran).
        called = []
        async def tracking_decider(t, thread_text):
            called.append(True)
            return Decision(action="done", why="model decided")

        decision, _ = await handle_event(task_id, "worker_finished", decide_fn=tracking_decider)
        assert called, "Model should have been called"


class TestReviewerApproved:
    """Case B: done when reviewer approved and MR check passed, no newer engineer commit."""

    @pytest.mark.asyncio
    async def test_reviewer_approved_and_check_passed_goes_done(self, db_path):
        """Reviewer approved + MR check passed + no newer commit → done."""
        task = store.create_task("fix something", repo="sisyphus")
        task_id = task["id"]

        # Create an MR with check passed.
        mr = mergerequests.create_mr(
            task_id=task_id,
            repo="sisyphus",
            source_branch="sisyphus/test",
            target_branch="main",
            source_path="/tmp/repo",
            bare_path="/tmp/bare"
        )

        # Set the MR as reviewed and checked.
        mergerequests.set_check_result(mr["id"], status="passed")
        mergerequests.set_review_status(mr["id"], review_status="approved")

        # Post a reviewer approval.
        store.post_message(task_id, "reviewer", kind="result", text="APPROVED: looks good")

        # The pass should mark done without calling the model.
        decision, _ = await handle_event(task_id, "worker_finished", decide_fn=_make_failing_decider())

        assert decision.action == "done"
        assert "rule:" in decision.why
        assert "reviewer approved and the check passed" in decision.why

    @pytest.mark.asyncio
    async def test_reviewer_approved_but_check_failed_asks_model(self, db_path):
        """Reviewer approved but check failed → ask the model."""
        task = store.create_task("fix something", repo="sisyphus")
        task_id = task["id"]

        # Create an MR with check failed.
        mr = mergerequests.create_mr(
            task_id=task_id,
            repo="sisyphus",
            source_branch="sisyphus/test",
            target_branch="main",
            source_path="/tmp/repo",
            bare_path="/tmp/bare"
        )

        # Set the MR as reviewed but check failed.
        mergerequests.set_check_result(mr["id"], status="failed")
        mergerequests.set_review_status(mr["id"], review_status="approved")

        # Post a reviewer approval.
        store.post_message(task_id, "reviewer", kind="result", text="APPROVED: looks good")

        # The pass should ask the model (check failed).
        called = []
        async def tracking_decider(t, thread_text):
            called.append(True)
            return Decision(action="done", why="model decided")

        decision, _ = await handle_event(task_id, "worker_finished", decide_fn=tracking_decider)
        assert called, "Model should have been called"

    @pytest.mark.asyncio
    async def test_newer_engineer_commit_asks_model(self, db_path):
        """Reviewer approved and check passed but newer engineer commit exists → ask model."""
        task = store.create_task("fix something", repo="sisyphus")
        task_id = task["id"]

        # Create an MR.
        mr = mergerequests.create_mr(
            task_id=task_id,
            repo="sisyphus",
            source_branch="sisyphus/test",
            target_branch="main",
            source_path="/tmp/repo",
            bare_path="/tmp/bare"
        )

        # Set the MR as reviewed and checked.
        mergerequests.set_check_result(mr["id"], status="passed")
        mergerequests.set_review_status(mr["id"], review_status="approved")

        # Post a reviewer approval.
        store.post_message(task_id, "reviewer", kind="result", text="APPROVED: looks good")

        # Post a newer engineer commit.
        store.post_message(task_id, "engineer", kind="commit", text="added more fixes")

        # The pass should ask the model (newer commit after approval).
        called = []
        async def tracking_decider(t, thread_text):
            called.append(True)
            return Decision(action="done", why="model decided")

        decision, _ = await handle_event(task_id, "worker_finished", decide_fn=tracking_decider)
        assert called, "Model should have been called"


class TestReviewerRequestedChanges:
    """Case C: spawn_worker(role="engineer") when reviewer requests changes, no engineer since."""

    @pytest.mark.asyncio
    async def test_reviewer_requested_changes_spawns_engineer(self, db_path):
        """Reviewer requested changes + no engineer since → spawn engineer."""
        task = store.create_task("fix something", repo="sisyphus")
        task_id = task["id"]

        # Post a reviewer request.
        store.post_message(task_id, "reviewer", kind="result", text="CHANGES REQUESTED: needs tests")

        # The pass should spawn an engineer without calling the model.
        decision, _ = await handle_event(task_id, "worker_finished", decide_fn=_make_failing_decider())

        assert decision.action == "spawn_worker"
        assert decision.role == "engineer"
        assert "rule:" in decision.why
        assert "reviewer requested changes" in decision.why

    @pytest.mark.asyncio
    async def test_engineer_already_ran_asks_model(self, db_path):
        """Reviewer requested changes but engineer already ran → ask the model."""
        task = store.create_task("fix something", repo="sisyphus")
        task_id = task["id"]

        # Post a reviewer request.
        store.post_message(task_id, "reviewer", kind="result", text="CHANGES REQUESTED: needs tests")

        # Engineer already ran (worker activity).
        store.post_message(task_id, "system", kind="worker_started", text="engineer started")
        store.post_message(task_id, "system", kind="worker_finished", text="engineer finished")

        # The pass should ask the model (engineer already ran).
        called = []
        async def tracking_decider(t, thread_text):
            called.append(True)
            return Decision(action="done", why="model decided")

        decision, _ = await handle_event(task_id, "worker_finished", decide_fn=tracking_decider)
        assert called, "Model should have been called"


class TestHumanAnswerBlocksRules:
    """Never apply rules if a human answer came after the last worker result."""

    @pytest.mark.asyncio
    async def test_human_answer_after_engineer_result_asks_model(self, db_path):
        """Human answer after engineer result → ask the model, not the rules."""
        task = store.create_task("fix something", repo="sisyphus")
        task_id = task["id"]

        # Engineer posts a result.
        store.post_message(task_id, "engineer", kind="result", text="done: added fix")

        # Create an MR (would normally trigger the rule).
        mr = mergerequests.create_mr(
            task_id=task_id,
            repo="sisyphus",
            source_branch="sisyphus/test",
            target_branch="main",
            source_path="/tmp/repo",
            bare_path="/tmp/bare"
        )

        # Human posts an answer (a message the model must read).
        store.post_message(task_id, "human", kind="answer", text="Looks good, proceed")

        # The pass should ask the model (human message after engineer result).
        called = []
        async def tracking_decider(t, thread_text):
            called.append(True)
            return Decision(action="done", why="model decided")

        decision, _ = await handle_event(task_id, "worker_finished", decide_fn=tracking_decider)
        assert called, "Model should have been called"


class TestConfigDisablesRules:
    """Config orchestrator.rules can turn off the rules entirely."""

    @pytest.mark.asyncio
    async def test_rules_disabled_asks_model(self, db_path):
        """With rules disabled, even obvious cases ask the model."""
        task = store.create_task("fix something", repo="sisyphus")
        task_id = task["id"]

        # Engineer posts a result.
        store.post_message(task_id, "engineer", kind="result", text="done: added fix")

        # Create an MR (would normally trigger the rule).
        mr = mergerequests.create_mr(
            task_id=task_id,
            repo="sisyphus",
            source_branch="sisyphus/test",
            target_branch="main",
            source_path="/tmp/repo",
            bare_path="/tmp/bare"
        )

        # Patch config to disable rules.
        with patch("core.orchestrator._obvious_step") as mock_obvious:
            # Make _obvious_step always return None (simulating disabled rules).
            mock_obvious.return_value = None

            # The pass should ask the model.
            called = []
            async def tracking_decider(t, thread_text):
                called.append(True)
                return Decision(action="spawn_worker", role="reviewer", why="model decided")

            decision, _ = await handle_event(task_id, "worker_finished", decide_fn=tracking_decider)
            assert called, "Model should have been called"


class TestPinnedAgentBlocksRules:
    """Never apply rules on pinned-agent (single-agent) tasks."""

    @pytest.mark.asyncio
    async def test_pinned_agent_asks_model(self, db_path):
        """Task with pinned agent → ask the model, not the rules."""
        task = store.create_task("fix something", repo="sisyphus", agent="explorer")
        task_id = task["id"]

        # Engineer posts a result (hypothetically).
        store.post_message(task_id, "engineer", kind="result", text="done: added fix")

        # Create an MR.
        mr = mergerequests.create_mr(
            task_id=task_id,
            repo="sisyphus",
            source_branch="sisyphus/test",
            target_branch="main",
            source_path="/tmp/repo",
            bare_path="/tmp/bare"
        )

        # The pass should use single-agent path, not the rules.
        # _obvious_step should return None for pinned-agent tasks.
        decision = orchestrator._obvious_step(task, store.get_thread(task_id))
        assert decision is None


class TestChildTasksBlockRules:
    """Never apply rules on parent tasks with children."""

    def test_task_with_children_blocks_rules(self, db_path):
        """Task with children → rules should not fire."""
        parent = store.create_task("parent goal", repo="sisyphus")
        parent_id = parent["id"]

        # Create a child task.
        child = store.create_task("child goal", repo="sisyphus", parent_id=parent_id)

        # Engineer posts a result (hypothetically).
        store.post_message(parent_id, "engineer", kind="result", text="done: added fix")

        # _obvious_step should return None for tasks with children.
        decision = orchestrator._obvious_step(parent, store.get_thread(parent_id))
        assert decision is None
