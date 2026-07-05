"""Integration tests for jira-sdlc pipeline with mock agents."""
import pytest
import json

from core.state import create_card, create_run, create_step, get_steps, get_card
from core.fsm import transition_step
from core.schemas import BaseResult


class TestPipelineBasics:
    def test_create_card(self, db_path):
        card = create_card("jira-sdlc", ["android"], "test jira task")
        assert card["status"] == "backlog"
        assert card["pipeline_name"] == "jira-sdlc"
        assert card["id"].startswith("card-")

    def test_get_card(self, db_path, card):
        fetched = get_card(card["id"])
        assert fetched is not None
        assert fetched["id"] == card["id"]
        assert fetched["status"] == "backlog"

    def test_create_run(self, db_path, card):
        run = create_run(card["id"])
        assert run["status"] == "running"
        assert run["card_id"] == card["id"]
        assert run["id"].startswith("run-")

    def test_create_step(self, db_path, card):
        run = create_run(card["id"])
        step = create_step(run["id"], "Android Engineer", 1)
        assert step["status"] == "pending"
        assert step["agent_name"] == "Android Engineer"
        assert step["step_order"] == 1


class TestFSM:
    def test_valid_transition(self, db_path, card):
        run = create_run(card["id"])
        step = create_step(run["id"], "Engineer", 1)

        ok1 = transition_step(step["id"], "running")
        assert ok1

        ok2 = transition_step(
            step["id"], "completed",
            json.dumps({"status": "completed", "summary": "ok"}),
        )
        assert ok2

        steps = get_steps(run["id"])
        assert steps[0]["status"] == "completed"

    def test_invalid_transition(self, db_path, card):
        """Terminal 'completed' status rejects further transitions."""
        run = create_run(card["id"])
        step = create_step(run["id"], "Engineer", 1)

        assert transition_step(step["id"], "running")
        assert transition_step(
            step["id"], "completed",
            json.dumps({"status": "completed", "summary": "ok"}),
        )

        assert not transition_step(step["id"], "running")

    def test_no_output_auto_fail(self, db_path, card):
        """completed without result_json -> auto-failed by FSM."""
        run = create_run(card["id"])
        step = create_step(run["id"], "Engineer", 1)

        transition_step(step["id"], "running")
        assert transition_step(step["id"], "completed")

        steps = get_steps(run["id"])
        assert steps[0]["status"] == "failed"
        error = steps[0].get("error") or ""
        assert "no_output" in error

    def test_pending_rejects_non_running(self, db_path, card):
        run = create_run(card["id"])
        step = create_step(run["id"], "Engineer", 1)

        assert not transition_step(step["id"], "completed")
        assert transition_step(step["id"], "running")


class TestResultSchema:
    def test_valid_result(self):
        r = BaseResult(status="completed", summary="ok")
        assert r.status == "completed"
        assert r.summary == "ok"

    def test_invalid_status(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            BaseResult(status="invalid_status", summary="x")

    def test_reviewer_result(self):
        r = BaseResult(status="completed", summary="ok", verdict="approved")
        assert r.verdict == "approved"

    def test_reviewer_changes_requested(self):
        r = BaseResult(
            status="completed", summary="needs fix",
            verdict="changes_requested",
        )
        assert r.verdict == "changes_requested"


class TestLoopCap:
    def test_max_loops_exceeded_pattern(self, db_path, card):
        MAX_REVIEW_LOOPS = 3

        run = create_run(card["id"])
        eng_step = create_step(run["id"], "Engineer", 1)
        rev_step = create_step(run["id"], "Reviewer", 2)

        loop_count = 0
        for i in range(MAX_REVIEW_LOOPS + 1):
            transition_step(eng_step["id"], "running")
            transition_step(
                eng_step["id"], "completed",
                json.dumps({"status": "completed", "summary": "changes applied"}),
            )

            transition_step(rev_step["id"], "running")
            if i < MAX_REVIEW_LOOPS:
                transition_step(
                    rev_step["id"], "completed",
                    json.dumps({
                        "status": "completed",
                        "verdict": "changes_requested",
                        "summary": "still needs changes",
                    }),
                )
                loop_count += 1

        assert loop_count == MAX_REVIEW_LOOPS


class TestResourceIsolation:
    def test_step_order_isolation(self, db_path, card):
        import os
        from core.resources import save_resource, get_available_resources

        run = create_run(card["id"])
        s1 = create_step(run["id"], "Engineer", 1)
        s2 = create_step(run["id"], "Reviewer", 2)
        s3 = create_step(run["id"], "QA", 2)

        run_dir = os.path.join(
            os.environ.get("WORKSPACE_ROOT", "/tmp"), "runs", run["id"], "resources",
        )
        os.makedirs(run_dir, exist_ok=True)

        save_resource(
            run["id"], s1["id"], "data_a.md", "content A",
            step_order=1, resource_type="agent_output",
        )
        save_resource(
            run["id"], s2["id"], "data_b.md", "content B",
            step_order=2, resource_type="agent_output",
        )
        save_resource(
            run["id"], s3["id"], "data_c.md", "content C",
            step_order=2, resource_type="agent_output",
        )

        available = get_available_resources(run["id"], step_order=2, exclude_step_id=s3["id"])
        assert len(available) == 1
        assert available[0]["filename"] == "data_a.md"
