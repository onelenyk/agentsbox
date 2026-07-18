"""Unit tests for state.py CRUD functions: get_run, get_step, update_run_status,
register_resource, get_resources_for_run."""

from core.state import (
    create_run,
    create_step,
    get_run,
    get_step,
    update_run_status,
    register_resource,
    get_resources_for_run,
)


class TestGetRun:
    def test_existing_run_returns_dict(self, db_path, run):
        result = get_run(run["id"])
        assert result is not None
        assert result["id"] == run["id"]
        assert result["card_id"] == run["card_id"]
        assert result["status"] == "running"

    def test_nonexistent_run_returns_none(self, db_path):
        result = get_run("run-nonexistent")
        assert result is None


class TestGetStep:
    def test_existing_step_returns_dict(self, db_path, run):
        step = create_step(run["id"], "TestAgent", 1)
        result = get_step(step["id"])
        assert result is not None
        assert result["id"] == step["id"]
        assert result["agent_name"] == "TestAgent"
        assert result["status"] == "pending"
        assert result["step_order"] == 1

    def test_nonexistent_step_returns_none(self, db_path):
        result = get_step("step-nonexistent")
        assert result is None


class TestUpdateRunStatus:
    def test_set_completed(self, db_path, run):
        update_run_status(run["id"], "completed")
        updated = get_run(run["id"])
        assert updated["status"] == "completed"
        assert updated["finished_at"] is not None

    def test_set_failed_with_error(self, db_path, run):
        update_run_status(run["id"], "failed", error="something crashed")
        updated = get_run(run["id"])
        assert updated["status"] == "failed"
        assert updated["finished_at"] is not None
        assert updated["error"] == "something crashed"

    def test_set_running_finished_at_stays_none(self, db_path, run):
        update_run_status(run["id"], "running")
        updated = get_run(run["id"])
        assert updated["status"] == "running"
        assert updated["finished_at"] is None


class TestRegisterResource:
    def test_basic_registration(self, db_path, run):
        step = create_step(run["id"], "Agent1", 0)
        res = register_resource(run["id"], step["id"], "output.txt")
        assert res is not None
        assert "id" in res
        assert res["run_id"] == run["id"]
        assert res["step_id"] == step["id"]
        assert res["filename"] == "output.txt"
        assert res["resource_type"] == "agent_output"

    def test_custom_resource_type(self, db_path, run):
        step = create_step(run["id"], "Agent1", 0)
        res = register_resource(run["id"], step["id"], "report.pdf", rtype="report")
        assert res["resource_type"] == "report"

    def test_different_step_order_values(self, db_path, run):
        step = create_step(run["id"], "Agent1", 3)
        res = register_resource(run["id"], step["id"], "data.json", step_order=3)
        assert res["step_order"] == 3

        step2 = create_step(run["id"], "Agent2", 5)
        res2 = register_resource(run["id"], step2["id"], "data2.json", step_order=5)
        assert res2["step_order"] == 5


class TestGetResourcesForRun:
    def test_no_resources_returns_empty(self, db_path, run):
        result = get_resources_for_run(run["id"])
        assert result == []

    def test_with_max_step_order_filter(self, db_path, run):
        s0 = create_step(run["id"], "AgentA", 0)
        s1 = create_step(run["id"], "AgentB", 1)
        s2 = create_step(run["id"], "AgentC", 2)

        register_resource(run["id"], s0["id"], "a.txt", step_order=0)
        register_resource(run["id"], s1["id"], "b.txt", step_order=1)
        register_resource(run["id"], s2["id"], "c.txt", step_order=2)

        result = get_resources_for_run(run["id"], max_step_order=1)
        assert len(result) == 2
        filenames = {r["filename"] for r in result}
        assert filenames == {"a.txt", "b.txt"}

    def test_with_exclude_step_id(self, db_path, run):
        s0 = create_step(run["id"], "AgentA", 0)
        s1 = create_step(run["id"], "AgentB", 1)

        register_resource(run["id"], s0["id"], "keep.txt", step_order=0)
        register_resource(run["id"], s1["id"], "exclude.txt", step_order=1)

        result = get_resources_for_run(run["id"], exclude_step_id=s0["id"])
        assert len(result) == 1
        assert result[0]["filename"] == "exclude.txt"
        assert result[0]["step_id"] == s1["id"]

    def test_multiple_resources_correct_filtering(self, db_path, run):
        s_low = create_step(run["id"], "LowAgent", 0)
        s_mid = create_step(run["id"], "MidAgent", 5)
        s_high = create_step(run["id"], "HighAgent", 10)

        register_resource(run["id"], s_low["id"], "low1.txt", step_order=0)
        register_resource(run["id"], s_low["id"], "low2.txt", step_order=0)
        register_resource(run["id"], s_mid["id"], "mid.txt", step_order=5)
        register_resource(run["id"], s_high["id"], "high.txt", step_order=10)

        result = get_resources_for_run(run["id"], max_step_order=5)
        assert len(result) == 3
        orders = {r["step_order"] for r in result}
        assert orders == {0, 5}

        result_excluded = get_resources_for_run(
            run["id"], max_step_order=5, exclude_step_id=s_low["id"]
        )
        assert len(result_excluded) == 1
        assert result_excluded[0]["filename"] == "mid.txt"
