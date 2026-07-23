"""Unit tests for state.py CRUD functions: get_run, get_step, update_run_status,
register_resource, get_resources_for_run."""

from core.state import (
    add_activity,
    claim_card,
    create_card,
    create_run,
    create_step,
    get_activities_for_card,
    get_card,
    get_resource_filenames_for_step,
    get_run,
    get_step,
    update_card_status,
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


class TestClaimCard:
    def test_claims_from_backlog(self, db_path):
        card = create_card("investigate", "test", "test card")
        assert card["status"] == "backlog"
        won = claim_card(card["id"])
        assert won is True
        assert get_card(card["id"])["status"] == "running"

    def test_claims_from_created(self, db_path):
        card = create_card("investigate", "test", "test card")
        update_card_status(card["id"], "created")
        won = claim_card(card["id"])
        assert won is True
        assert get_card(card["id"])["status"] == "running"

    def test_second_claim_loses(self, db_path):
        """Regression test: two concurrent callers racing to claim the same
        card must not both win — only the first UPDATE's WHERE clause
        matches, the second sees status already 'running' and no-ops."""
        card = create_card("investigate", "test", "test card")
        first = claim_card(card["id"])
        second = claim_card(card["id"])
        assert first is True
        assert second is False
        assert get_card(card["id"])["status"] == "running"

    def test_rejects_running_by_default(self, db_path):
        card = create_card("investigate", "test", "test card")
        update_card_status(card["id"], "running")
        won = claim_card(card["id"])
        assert won is False

    def test_rejects_completed_and_failed_by_default(self, db_path):
        for status in ("completed", "failed"):
            card = create_card("investigate", "test", "test card")
            update_card_status(card["id"], status)
            assert claim_card(card["id"]) is False
            assert get_card(card["id"])["status"] == status

    def test_custom_from_statuses(self, db_path):
        card = create_card("investigate", "test", "test card")
        update_card_status(card["id"], "failed")
        assert claim_card(card["id"], from_statuses=("backlog", "created")) is False
        assert claim_card(card["id"], from_statuses=("failed",)) is True
        assert get_card(card["id"])["status"] == "running"


class TestActivities:
    def test_add_and_get_ordered(self, db_path, card):
        add_activity(card["id"], "first", author="Engineer",
                     created_at="2026-07-22T10:00:00+00:00")
        add_activity(card["id"], "second", author="user", author_type="human",
                     created_at="2026-07-22T10:00:05+00:00")

        activities = get_activities_for_card(card["id"])
        assert [a["text"] for a in activities] == ["first", "second"]
        assert activities[0]["author_type"] == "agent"
        assert activities[1]["author_type"] == "human"

    def test_resources_json_round_trip(self, db_path, card):
        added = add_activity(card["id"], "with files", resources=["a.md", "b.json"])
        assert added["resources"] == ["a.md", "b.json"]
        fetched = get_activities_for_card(card["id"])[0]
        assert fetched["resources"] == ["a.md", "b.json"]

    def test_nullable_run_and_step(self, db_path, card):
        added = add_activity(card["id"], "human note", author_type="human")
        assert added["run_id"] is None
        assert added["step_id"] is None

    def test_invalid_author_type_rejected(self, db_path, card):
        import sqlite3
        try:
            add_activity(card["id"], "bad", author_type="robot")
            assert False, "Expected IntegrityError"
        except sqlite3.IntegrityError:
            pass

    def test_created_at_defaults_to_now(self, db_path, card):
        added = add_activity(card["id"], "no ts given")
        assert added["created_at"]  # non-empty ISO string


class TestGetResourceFilenamesForStep:
    def test_returns_registered_filenames(self, db_path, run):
        step = create_step(run["id"], "Engineer", 1)
        register_resource(run["id"], step["id"], "a.md")
        register_resource(run["id"], step["id"], "b.json")
        other = create_step(run["id"], "Other", 2)
        register_resource(run["id"], other["id"], "c.md")

        names = get_resource_filenames_for_step(run["id"], step["id"])
        assert names == {"a.md", "b.json"}

    def test_empty_when_none(self, db_path, run):
        step = create_step(run["id"], "Engineer", 1)
        assert get_resource_filenames_for_step(run["id"], step["id"]) == set()
