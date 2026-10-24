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
        mergerequests.create_mr(
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
    async def test_reviewer_requested_changes_from_verdict_note(self, db_path):
        """Independent review task (mr-reviewer) posts a verdict note, not a result.

        This tests the regression for task-788f0e41: when mr-reviewer finishes with
        CHANGES REQUESTED, it posts a note to the origin task, not a result message.
        The rule must detect the verdict from the note (kind="note", author="system",
        text matching "review verdict for mr-...").
        """
        task = store.create_task("fix something", repo="sisyphus")
        task_id = task["id"]

        # Create an MR so we can reference it in the verdict note.
        mr = mergerequests.create_mr(
            task_id=task_id,
            repo="sisyphus",
            source_branch="sisyphus/test",
            target_branch="main",
            source_path="/tmp/repo",
            bare_path="/tmp/bare"
        )

        # Post the verdict note from an independent review task (mr-reviewer).
        # This is what agent_mcp.py posts when a review task finishes.
        store.post_message(task_id, "system", kind="note",
                          text=f"review verdict for {mr['id']}: CHANGES REQUESTED — needs tests")

        # The pass should spawn an engineer without calling the model.
        decision, _ = await handle_event(task_id, "external", decide_fn=_make_failing_decider())

        assert decision.action == "spawn_worker"
        assert decision.role == "engineer"
        assert "rule:" in decision.why
        assert "reviewer requested changes" in decision.why

    @pytest.mark.asyncio
    async def test_reviewer_result_then_worker_finished_queues_engineer_under_running_mr_review(self, db_path):
        """Reviewer result (CHANGES REQUESTED) + worker_finished → engineer queued
        in the same pass, even while the branch's own independent review is running.

        Regression for task-788f0e41: the `_mr_review running` guard used to swallow
        the embedded reviewer's verdict + worker_finished, so the task sat dead for
        ~30 minutes (task-fb481456 ran 20:46:41→21:11:42) until the independent
        review finished. The verdict is already on the thread when worker_finished
        arrives, so the pass must reach the rules and act on it immediately.
        """
        task = store.create_task("fix something", repo="sisyphus")
        task_id = task["id"]

        # Create an MR so the independent review (_mr_review) has something to track.
        mergerequests.create_mr(
            task_id=task_id,
            repo="sisyphus",
            source_branch="sisyphus/test",
            target_branch="main",
            source_path="/tmp/repo",
            bare_path="/tmp/bare"
        )

        # The embedded reviewer already posted its verdict on the thread.
        store.post_message(task_id, "reviewer", kind="result", text="CHANGES REQUESTED: needs tests")

        # The branch's own independent review is still running — the exact
        # task-788f0e41 condition that previously swallowed the verdict.
        with patch("core.orchestrator._mr_review", return_value={"verdict": "", "running": True}):
            decision, _ = await handle_event(task_id, "worker_finished", decide_fn=_make_failing_decider())

        assert decision.action == "spawn_worker"
        assert decision.role == "engineer"
        assert "rule:" in decision.why
        assert "reviewer requested changes" in decision.why
        # The engineer must be queued in the same pass.
        queued = [m for m in store.get_thread(task_id)
                  if m["kind"] == "worker_queued" and "engineer" in (m["text"] or "")]
        assert queued, "engineer should be queued in the same pass"

    @pytest.mark.asyncio
    async def test_verdict_note_external_queues_engineer_under_running_mr_review(self, db_path):
        """Verdict note + external event → engineer queued even if the independent
        review task is still marked running (the note races its done transition).

        In task-788f0e41 the verdict note arrived at 21:11:42 while the independent
        review task was still on its feet; the `_mr_review running` guard would have
        swallowed the external event too. A verdict already on the thread must always
        be acted on.
        """
        task = store.create_task("fix something", repo="sisyphus")
        task_id = task["id"]

        mr = mergerequests.create_mr(
            task_id=task_id,
            repo="sisyphus",
            source_branch="sisyphus/test",
            target_branch="main",
            source_path="/tmp/repo",
            bare_path="/tmp/bare"
        )

        store.post_message(task_id, "system", kind="note",
                          text=f"review verdict for {mr['id']}: CHANGES REQUESTED — needs tests")

        with patch("core.orchestrator._mr_review", return_value={"verdict": "", "running": True}):
            decision, _ = await handle_event(task_id, "external", decide_fn=_make_failing_decider())

        assert decision.action == "spawn_worker"
        assert decision.role == "engineer"
        assert "reviewer requested changes" in decision.why

    @pytest.mark.asyncio
    async def test_still_waits_when_no_verdict_on_thread(self, db_path):
        """Without a verdict on the thread, the pass still waits for the branch's
        own independent review (the original guard behaviour — no wasted passes,
        no second reviewer beside it)."""
        task = store.create_task("fix something", repo="sisyphus")
        task_id = task["id"]

        # Engineer delivered an MR; no reviewer verdict on the thread yet.
        store.post_message(task_id, "engineer", kind="result", text="done")
        mergerequests.create_mr(
            task_id=task_id,
            repo="sisyphus",
            source_branch="sisyphus/test",
            target_branch="main",
            source_path="/tmp/repo",
            bare_path="/tmp/bare"
        )

        with patch("core.orchestrator._mr_review", return_value={"verdict": "", "running": True}):
            decision, _ = await handle_event(task_id, "worker_finished", decide_fn=_make_failing_decider())

        assert decision.action == "noop"
        assert "review is still running" in decision.why

    @pytest.mark.asyncio
    async def test_later_verdict_note_is_harmless(self, db_path):
        """A verdict note arriving after the engineer already addressed the
        reviewer's result must not spawn a second engineer.

        In task-788f0e41 the independent review's note landed ~11 minutes after the
        reviewer's own result. If the engineer was queued on the result (as it now
        is), the note is a duplicate of the same review round — the rule must find
        the reviewer's result first and see that the engineer already ran.
        """
        task = store.create_task("fix something", repo="sisyphus")
        task_id = task["id"]

        mr = mergerequests.create_mr(
            task_id=task_id,
            repo="sisyphus",
            source_branch="sisyphus/test",
            target_branch="main",
            source_path="/tmp/repo",
            bare_path="/tmp/bare"
        )

        # Reviewer requested changes; the engineer addressed them.
        store.post_message(task_id, "reviewer", kind="result", text="CHANGES REQUESTED: needs tests")
        store.post_message(task_id, "system", kind="worker_queued", text="engineer queued")
        store.post_message(task_id, "system", kind="worker_started", text="engineer started")
        store.post_message(task_id, "system", kind="worker_finished", text="engineer finished")

        # The independent review's verdict note arrives later (its own done
        # transition emits the external event).
        store.post_message(task_id, "system", kind="note",
                          text=f"review verdict for {mr['id']}: CHANGES REQUESTED — needs tests")

        # With the independent review still running, the pass must NOT spawn a
        # second engineer; it waits with the guarded noop (no model call).
        with patch("core.orchestrator._mr_review", return_value={"verdict": "", "running": True}):
            decision, _ = await handle_event(task_id, "external", decide_fn=_make_failing_decider())

        assert decision.action == "noop"
        assert "review is still running" in decision.why
        queued = [m for m in store.get_thread(task_id)
                  if m["kind"] == "worker_queued" and "engineer" in (m["text"] or "")]
        assert len(queued) == 1, "a second engineer must not be queued"

    @pytest.mark.asyncio
    async def test_continue_after_engineer_addressed_does_not_loop_to_model(self, db_path):
        """Once the engineer has addressed the reviewer's changes, a `continue`
        must fall back to the guarded noop (one deduped note), not call the model
        — so a watchdog pass can never loop through the decider."""
        task = store.create_task("fix something", repo="sisyphus")
        task_id = task["id"]

        # Reviewer requested changes; the engineer already ran.
        store.post_message(task_id, "reviewer", kind="result", text="CHANGES REQUESTED: needs tests")
        store.post_message(task_id, "system", kind="worker_started", text="engineer started")
        store.post_message(task_id, "system", kind="worker_finished", text="engineer finished")

        # A continue while the branch's own review is still running: the guarded
        # noop fires (the engineer has addressed the verdict), and the model is
        # never asked.
        with patch("core.orchestrator._mr_review", return_value={"verdict": "", "running": True}):
            decision, _ = await handle_event(task_id, "continue", decide_fn=_make_failing_decider())

        assert decision.action == "noop"
        assert "review is still running" in decision.why

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
        mergerequests.create_mr(
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
        mergerequests.create_mr(
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
        mergerequests.create_mr(
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
        store.create_task("child goal", repo="sisyphus", parent_id=parent_id)

        # Engineer posts a result (hypothetically).
        store.post_message(parent_id, "engineer", kind="result", text="done: added fix")

        # _obvious_step should return None for tasks with children.
        decision = orchestrator._obvious_step(parent, store.get_thread(parent_id))
        assert decision is None


class TestTheBranchsOwnReviewCounts:
    """open_mr spawns an independent mr-reviewer task for the branch. Its
    verdict reaches this thread as a system note, and the pass already treats
    it as the approval — the rules must not spawn a second reviewer beside it,
    and must accept its approval as done."""

    def _mr(self, task_id, **cols):
        mr = mergerequests.create_mr(task_id=task_id, repo="sisyphus", source_branch="sisyphus/t",
                                     target_branch="main", source_path="/tmp/repo", bare_path="/tmp/bare")
        return mr

    @pytest.mark.asyncio
    async def test_no_second_reviewer_while_the_branch_review_runs(self, db_path):
        task = store.create_task("fix", repo="sisyphus")
        store.post_message(task["id"], "engineer", kind="result", text="done")
        self._mr(task["id"])
        with patch("core.orchestrator._mr_review", return_value={"verdict": "", "running": True}):
            assert orchestrator._obvious_step(task, store.get_thread(task["id"])) is None

    @pytest.mark.asyncio
    async def test_no_second_reviewer_while_the_review_is_only_pending(self, db_path):
        # open_mr records "pending" before the hook spends ~30s on the check and
        # creates the mr-reviewer task; in that window nothing is running yet.
        task = store.create_task("fix", repo="sisyphus")
        store.post_message(task["id"], "engineer", kind="result", text="done")
        self._mr(task["id"])
        with patch("core.orchestrator._mr_review", return_value={"verdict": "pending", "running": False}):
            assert orchestrator._obvious_step(task, store.get_thread(task["id"])) is None

    @pytest.mark.asyncio
    async def test_the_branch_reviews_approval_is_done(self, db_path):
        task = store.create_task("fix", repo="sisyphus")
        store.post_message(task["id"], "engineer", kind="result", text="done")
        mr = self._mr(task["id"])
        store.post_message(task["id"], "system", kind="note",
                           text=f"review verdict for {mr['id']}: APPROVED — fine")
        with patch("core.orchestrator._mr_review", return_value={"verdict": "approved", "running": False}), \
                patch("core.mergerequests.merge_block_reason", return_value=None):
            d = orchestrator._obvious_step(task, store.get_thread(task["id"]))
        assert d is not None and d.action == "done"


class TestConflictingVerdicts:
    """Rule 1: an unanswered CHANGES REQUESTED on the current head blocks done,
    whichever reviewer wrote it — APPROVED counts only when no reviewer requested
    changes on that head."""

    @pytest.mark.asyncio
    async def test_changes_requested_then_mr_reviewer_approved_is_not_done(self, db_path):
        """Task reviewer CHANGES REQUESTED, then mr-reviewer APPROVED the SAME
        head → not done; the engineer addresses the request next.

        Regression for task-df5a3fcc (SIS-155): the MR row's review_status was
        overwritten to 'approved' by the second verdict, so the pass read an
        approval and marked done while the first reviewer's CHANGES REQUESTED
        stood unanswered on the same head (no new commit between the two)."""
        task = store.create_task("fix something", repo="sisyphus")
        task_id = task["id"]

        mr = mergerequests.create_mr(
            task_id=task_id, repo="sisyphus", source_branch="sisyphus/test",
            target_branch="main", source_path="/tmp/repo", bare_path="/tmp/bare")
        mergerequests.set_check_result(mr["id"], status="passed")

        # The task's own reviewer asked for changes on the current head…
        store.post_message(task_id, "reviewer", kind="result",
                           text="CHANGES REQUESTED: needs tests")
        # …then the independent review approved the same head, and its verdict
        # clobbered the MR row's review_status to 'approved'.
        store.post_message(task_id, "system", kind="note",
                           text=f"review verdict for {mr['id']}: APPROVED — fine")
        mergerequests.set_review_status(mr["id"], review_status="approved")

        decision, _ = await handle_event(task_id, "external", decide_fn=_make_failing_decider())

        assert decision.action == "spawn_worker"
        assert decision.role == "engineer"
        assert "rule:" in decision.why
        assert "reviewer requested changes" in decision.why

    @pytest.mark.asyncio
    async def test_answered_changes_requested_allows_approval(self, db_path):
        """A CHANGES REQUESTED answered by a newer commit does not block an
        APPROVED that followed it → done."""
        task = store.create_task("fix something", repo="sisyphus")
        task_id = task["id"]

        mr = mergerequests.create_mr(
            task_id=task_id, repo="sisyphus", source_branch="sisyphus/test",
            target_branch="main", source_path="/tmp/repo", bare_path="/tmp/bare")
        mergerequests.set_check_result(mr["id"], status="passed")
        mergerequests.set_review_status(mr["id"], review_status="approved")

        # The reviewer asked for changes; the engineer pushed a newer head; a
        # fresh approval followed on that head.
        store.post_message(task_id, "reviewer", kind="result",
                           text="CHANGES REQUESTED: needs tests")
        store.post_message(task_id, "engineer", kind="commit", text="addressed (abc1234)")
        store.post_message(task_id, "reviewer", kind="result", text="APPROVED: looks good")

        decision, _ = await handle_event(task_id, "worker_finished", decide_fn=_make_failing_decider())

        assert decision.action == "done"
        assert "reviewer approved and the check passed" in decision.why


class TestDoneRequiresDelivery:
    """Rule 2: with auto_merge on, done means the MR is actually merged; a merge
    auto-merge cannot deliver is the engineer's next, not done."""

    @pytest.mark.asyncio
    async def test_auto_merge_blocked_by_conflicts_sends_engineer(self, db_path, monkeypatch):
        """Approved + check passed, but auto-merge is blocked by a conflict →
        the engineer is next with the conflict reason in its instruction."""
        task = store.create_task("fix something", repo="acme")
        task_id = task["id"]

        mr = mergerequests.create_mr(
            task_id=task_id, repo="acme", source_branch="sisyphus/test",
            target_branch="main", source_path="/tmp/repo", bare_path="/tmp/bare")
        mergerequests.set_review_status(mr["id"], review_status="approved")
        mergerequests.set_check_result(mr["id"], status="passed", command="pytest -q")
        store.post_message(task_id, "reviewer", kind="result", text="APPROVED: looks good")

        # The repo opted into auto-merge, and the branch does not merge cleanly.
        monkeypatch.setattr("core.repos.get_repo",
                            lambda name: {"name": name, "auto_merge": True})
        monkeypatch.setattr("core.mergerequests.check_mergeability",
                            lambda *a, **k: {"ok": True, "mergeable": False,
                                             "conflicts": ["feature.py"]})

        decision, _ = await handle_event(task_id, "worker_finished", decide_fn=_make_failing_decider())

        assert decision.action == "spawn_worker"
        assert decision.role == "engineer"
        assert "auto-merge is blocked" in decision.why
        assert "does not merge cleanly" in decision.instruction

    @pytest.mark.asyncio
    async def test_approved_and_merged_is_done(self, db_path, monkeypatch):
        """Approved + check passed + the MR is actually merged → done."""
        task = store.create_task("fix something", repo="acme")
        task_id = task["id"]

        mr = mergerequests.create_mr(
            task_id=task_id, repo="acme", source_branch="sisyphus/test",
            target_branch="main", source_path="/tmp/repo", bare_path="/tmp/bare")
        mergerequests.set_review_status(mr["id"], review_status="approved")
        mergerequests.set_check_result(mr["id"], status="passed", command="pytest -q")
        mergerequests.set_status(mr["id"], "merged")
        store.post_message(task_id, "reviewer", kind="result", text="APPROVED: looks good")

        monkeypatch.setattr("core.repos.get_repo",
                            lambda name: {"name": name, "auto_merge": True})

        decision, _ = await handle_event(task_id, "worker_finished", decide_fn=_make_failing_decider())

        assert decision.action == "done"
        assert "reviewer approved and the check passed" in decision.why

    @pytest.mark.asyncio
    async def test_no_auto_merge_still_done_with_open_mr(self, db_path):
        """Without auto_merge, an approved + check-passed MR that a human has
        not merged yet is still done — exactly as before."""
        task = store.create_task("fix something", repo="acme")
        task_id = task["id"]

        mr = mergerequests.create_mr(
            task_id=task_id, repo="acme", source_branch="sisyphus/test",
            target_branch="main", source_path="/tmp/repo", bare_path="/tmp/bare")
        mergerequests.set_review_status(mr["id"], review_status="approved")
        mergerequests.set_check_result(mr["id"], status="passed", command="pytest -q")
        store.post_message(task_id, "reviewer", kind="result", text="APPROVED: looks good")

        decision, _ = await handle_event(task_id, "worker_finished", decide_fn=_make_failing_decider())

        assert decision.action == "done"
        assert "reviewer approved and the check passed" in decision.why


class TestNoopNoteDedupe:
    """A no-op or note decision should only post one explanatory message, not repeat it."""

    @pytest.mark.asyncio
    async def test_noop_posts_one_note_then_dedupes(self, db_path):
        """First no-op pass posts a note; identical subsequent passes don't repeat it.

        This tests the regression for task-788f0e41: when the watchdog queued `continue`
        events on a task that was blocked, the orchestrator posted an identical noop note
        every 2 minutes instead of just once. The fix dedupes against the last message.
        """
        task = store.create_task("something", repo="sisyphus")
        task_id = task["id"]

        # Engineer posts result with MR, but we won't spawn a reviewer (mocked to return None).
        store.post_message(task_id, "engineer", kind="result", text="done")
        mergerequests.create_mr(
            task_id=task_id,
            repo="sisyphus",
            source_branch="sisyphus/test",
            target_branch="main",
            source_path="/tmp/repo",
            bare_path="/tmp/bare"
        )

        # First pass: the branch's own review is running (mocked), so a noop decision is made.
        async def no_decide(t, thread_text):
            raise AssertionError("Should not call decider")

        with patch("core.orchestrator._mr_review", return_value={"verdict": "", "running": True}):
            decision1, _ = await handle_event(task_id, "continue", decide_fn=no_decide)

        assert decision1.action == "noop"
        thread_after_first = store.get_thread(task_id)
        # Find the orchestrator note with the noop message.
        noop_notes = [m["text"] for m in thread_after_first if m["author"] == "orchestrator" and "[noop]" in m["text"]]
        assert len(noop_notes) == 1, f"Expected 1 noop note, got {len(noop_notes)}: {noop_notes}"
        noop_message = noop_notes[0]

        # Second pass: same conditions, same noop decision. The note should NOT be posted again.
        with patch("core.orchestrator._mr_review", return_value={"verdict": "", "running": True}):
            decision2, _ = await handle_event(task_id, "continue", decide_fn=no_decide)

        assert decision2.action == "noop"
        thread_after_second = store.get_thread(task_id)
        noop_notes_2 = [m["text"] for m in thread_after_second
                        if m["author"] == "orchestrator" and "[noop]" in m["text"]]
        assert len(noop_notes_2) == 1, (f"Expected 1 noop note after 2 passes, "
                                         f"got {len(noop_notes_2)}: {noop_notes_2}")
        assert noop_notes_2[0] == noop_message, "Noop message should be unchanged"


class TestQaVerdict:
    """A device check (qa) is the last word on a change a person would see: a
    FAIL goes back to the engineer, and anything but PASS blocks done — on
    SLN-25944 tests, check and two approvals were green while the symptom
    stayed on the device."""

    def _approved_task(self):
        task = store.create_task("fix the login backspace", repo="sisyphus")
        tid = task["id"]
        store.post_message(tid, "engineer", kind="commit", text="fix(auth): clamp")
        store.post_message(tid, "engineer", kind="result", text="done")
        mergerequests.create_mr(task_id=tid, repo="sisyphus", source_branch="sisyphus/t",
                                target_branch="main", source_path="/tmp/r", bare_path="/tmp/b")
        store.post_message(tid, "reviewer", kind="result", text="APPROVED: looks right")
        return store.get_task(tid)

    def test_qa_fail_sends_it_back_to_the_engineer_with_the_findings(self, db_path):
        task = self._approved_task()
        store.post_message(task["id"], "qa", kind="result",
                           text="FAIL — after 6 digits clearFocus() hides the keyboard")
        d = orchestrator._obvious_step(task, store.get_thread(task["id"]))
        assert d.action == "spawn_worker" and d.role == "engineer"
        assert d.why == "rule: qa failed on the device"
        assert "clearFocus() hides the keyboard" in d.instruction

    def test_a_commit_after_the_fail_is_the_answer_not_another_fail(self, db_path):
        task = self._approved_task()
        store.post_message(task["id"], "qa", kind="result", text="FAIL — still there")
        store.post_message(task["id"], "engineer", kind="commit", text="fix(auth): keep focus")
        d = orchestrator._obvious_step(task, store.get_thread(task["id"]))
        assert d is None or d.why != "rule: qa failed on the device"

    def test_an_unanswered_inconclusive_blocks_done(self, db_path):
        task = self._approved_task()
        store.post_message(task["id"], "qa", kind="result", text="INCONCLUSIVE — the build failed")
        with patch.object(mergerequests, "merge_block_reason", return_value=None), \
             patch.object(orchestrator, "_reviewer_approved", return_value=True):
            d = orchestrator._obvious_step(task, store.get_thread(task["id"]))
        assert d is None or d.action != "done"

    def test_qa_pass_lets_done_through(self, db_path):
        task = self._approved_task()
        store.post_message(task["id"], "qa", kind="result", text="PASS — backspace erases")
        with patch.object(mergerequests, "merge_block_reason", return_value=None), \
             patch.object(orchestrator, "_reviewer_approved", return_value=True):
            d = orchestrator._obvious_step(task, store.get_thread(task["id"]))
        assert d is not None and d.action == "done"

    def test_a_qa_result_is_not_an_engineer_result_to_review(self, db_path):
        task = store.create_task("check it", repo="sisyphus")
        mergerequests.create_mr(task_id=task["id"], repo="sisyphus", source_branch="sisyphus/t",
                                target_branch="main", source_path="/tmp/r", bare_path="/tmp/b")
        store.post_message(task["id"], "qa", kind="result", text="REPRODUCED — backspace dead")
        d = orchestrator._obvious_step(task, store.get_thread(task["id"]))
        assert d is None or d.role != "reviewer"

    def test_verdict_parsing(self):
        th = [{"kind": "result", "author": "qa", "text": "  not reproduced: fine"}]
        assert orchestrator._latest_qa_verdict(th) == (0, "NOT REPRODUCED")
        assert orchestrator._latest_qa_verdict([]) == (-1, "")


class TestRepoWantsDeviceCheck:
    """repos[].qa: a delivered change gets qa on a device before the reviewer,
    and is not done without a PASS on its last commit."""

    def _delivered(self):
        task = store.create_task("fix the login backspace", repo="kmp")
        tid = task["id"]
        store.post_message(tid, "engineer", kind="commit", text="fix(auth): keep focus")
        store.post_message(tid, "engineer", kind="result", text="done")
        mergerequests.create_mr(task_id=tid, repo="kmp", source_branch="sisyphus/t",
                                target_branch="main", source_path="/tmp/r", bare_path="/tmp/b")
        return store.get_task(tid)

    def test_qa_runs_before_the_reviewer(self, db_path):
        task = self._delivered()
        with patch("core.repos.load_repos", return_value=[{"name": "kmp", "source": "/x", "qa": "android"}]):
            d = orchestrator._obvious_step(task, store.get_thread(task["id"]))
        assert d.role == "qa" and d.device == "android"
        assert d.why == "rule: the repo wants a device check before review"

    def test_after_qa_the_reviewer_runs(self, db_path):
        task = self._delivered()
        store.post_message(task["id"], "qa", kind="result", text="PASS — backspace erases")
        with patch("core.repos.load_repos", return_value=[{"name": "kmp", "source": "/x", "qa": "android"}]):
            d = orchestrator._obvious_step(task, store.get_thread(task["id"]))
        assert d.role == "reviewer"

    def test_not_done_without_a_pass_on_the_last_commit(self, db_path):
        task = self._delivered()
        store.post_message(task["id"], "reviewer", kind="result", text="APPROVED")
        with patch("core.repos.load_repos", return_value=[{"name": "kmp", "source": "/x", "qa": "android"}]), \
             patch.object(mergerequests, "merge_block_reason", return_value=None), \
             patch.object(orchestrator, "_reviewer_approved", return_value=True):
            d = orchestrator._obvious_step(task, store.get_thread(task["id"]))
        assert d is None or d.action != "done"

    def test_a_repo_without_qa_is_unchanged(self, db_path):
        task = self._delivered()
        with patch("core.repos.load_repos", return_value=[{"name": "kmp", "source": "/x"}]):
            d = orchestrator._obvious_step(task, store.get_thread(task["id"]))
        assert d.role == "reviewer"
