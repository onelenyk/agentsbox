"""Tests for core/operations.py — unified card lifecycle operations."""
import asyncio
from unittest.mock import patch

from core.state import get_card
from core.operations import (
    resolve_pipeline_path,
    create_card_only,
    create_and_run,
    create_and_run_async,
    run_card,
    resume_card,
    cancel_card,
    get_card_detail,
    list_cards_filtered,
)


class TestResolvePipelinePath:
    def test_resolve_pipeline_path_exists(self):
        path = resolve_pipeline_path("investigate")
        assert path.exists()
        assert path.name == "investigate.yaml"

    def test_resolve_pipeline_path_missing(self):
        path = resolve_pipeline_path("nonexistent-pipeline")
        assert not path.exists()
        assert path.name == "nonexistent-pipeline.yaml"


class TestCreateCardOnly:
    def test_create_card_only_basics(self, db_path):
        card = create_card_only("investigate", ["android"], "test card")
        assert card["status"] == "backlog"
        assert card["id"].startswith("card-")
        assert card["pipeline_name"] == "investigate"

    def test_create_card_only_with_jira(self, db_path):
        card = create_card_only(
            "jira-sdlc", ["ios"], "jira card", jira_url="https://jira.example.com/ISSUE-1",
        )
        assert card["status"] == "backlog"
        assert card["pipeline_name"] == "jira-sdlc"
        assert card["jira_url"] == "https://jira.example.com/ISSUE-1"


class TestCreateAndRun:
    def test_create_and_run_returns_dict(self, db_path):
        with patch("core.pipeline.run_pipeline") as mock_run:
            mock_run.return_value = {"status": "completed", "run_id": "run-mocked"}
            result = create_and_run("investigate", ["android"], "test run")
        assert isinstance(result, dict)
        assert result["status"] == "completed"
        assert result["run_id"] == "run-mocked"

    def test_create_and_run_has_run_id(self, db_path):
        with patch("core.pipeline.run_pipeline") as mock_run:
            mock_run.return_value = {"status": "completed", "run_id": "run-mocked"}
            result = create_and_run("investigate", ["android"], "test run id")
        assert "run_id" in result
        assert result["run_id"].startswith("run-")


class TestCreateAndRunAsync:
    def test_create_and_run_async_returns_card_id(self, db_path):
        card_id = asyncio.run(
            create_and_run_async("investigate", ["android"], "async test"),
        )
        assert isinstance(card_id, str)
        assert card_id.startswith("card-")

    def test_create_and_run_async_card_in_db(self, db_path):
        card_id = asyncio.run(
            create_and_run_async("investigate", ["android"], "async db test"),
        )
        card = get_card(card_id)
        assert card is not None
        assert card["id"] == card_id


class TestRunCard:
    def test_run_card_changes_status(self, db_path, card):
        asyncio.run(run_card(card["id"]))
        updated = get_card(card["id"])
        assert updated is not None
        assert updated["status"] == "running"

    def test_run_card_unknown_raises(self, db_path):
        try:
            asyncio.run(run_card("card-nonexistent"))
            assert False, "Expected ValueError"
        except ValueError as e:
            assert "not found" in str(e)


class TestResumeCard:
    def test_resume_card_runs_without_error(self, db_path):
        from core.state import create_run, create_step

        card = create_card_only("investigate", ["android"], "resume test")
        run = create_run(card["id"])
        create_step(run["id"], "Android Engineer", 1)

        with patch("core.pipeline.run_pipeline") as mock_run:
            mock_run.return_value = {"status": "completed", "run_id": run["id"]}
            asyncio.run(resume_card(card["id"]))
            mock_run.assert_called_once()

    def test_resume_card_unknown_raises(self, db_path):
        try:
            asyncio.run(resume_card("card-nonexistent"))
            assert False, "Expected ValueError"
        except ValueError as e:
            assert "not found" in str(e)

    def test_resume_card_no_run_raises(self, db_path):
        card = create_card_only("investigate", ["android"], "no run resume")
        try:
            asyncio.run(resume_card(card["id"]))
            assert False, "Expected ValueError"
        except ValueError as e:
            assert "No runs found" in str(e)


class TestCancelCard:
    def test_cancel_card_sets_failed(self, db_path):
        card = create_card_only("investigate", ["android"], "cancel test")
        asyncio.run(cancel_card(card["id"]))
        updated = get_card(card["id"])
        assert updated is not None
        assert updated["status"] == "failed"

    def test_cancel_card_unknown_raises(self, db_path):
        try:
            asyncio.run(cancel_card("card-nonexistent"))
            assert False, "Expected ValueError"
        except ValueError as e:
            assert "not found" in str(e)


class TestGetCardDetail:
    def test_get_card_detail_keys(self, db_path):
        card = create_card_only("investigate", ["android"], "detail test")
        detail = get_card_detail(card["id"])
        assert isinstance(detail, dict)
        assert "card" in detail
        assert "run" in detail
        assert "steps" in detail
        assert "resources" in detail
        assert "repo_name" in detail

    def test_get_card_detail_card_matches(self, db_path):
        card = create_card_only("investigate", ["android"], "detail match")
        detail = get_card_detail(card["id"])
        assert detail["card"]["id"] == card["id"]

    def test_get_card_detail_unknown_raises(self, db_path):
        try:
            get_card_detail("card-nonexistent")
            assert False, "Expected ValueError"
        except ValueError as e:
            assert "not found" in str(e)

    def test_get_card_detail_with_run(self, db_path, card, run):
        detail = get_card_detail(card["id"])
        assert detail["run"] is not None
        assert detail["run"]["id"] == run["id"]
        assert detail["steps"] == []
        assert detail["resources"] == []


class TestListCardsFiltered:
    def test_list_cards_filtered_by_pipeline(self, db_path):
        create_card_only("investigate", ["android"], "card a")
        create_card_only("jira-sdlc", ["ios"], "card b")
        create_card_only("investigate", ["kmp"], "card c")

        result = list_cards_filtered(pipeline="investigate")
        assert len(result) == 2
        assert all(c["pipeline_name"] == "investigate" for c in result)

    def test_list_cards_filtered_by_status(self, db_path):
        create_card_only("investigate", ["android"], "backlog card")
        create_card_only("jira-sdlc", ["ios"], "another backlog")

        result = list_cards_filtered(status="backlog")
        assert len(result) >= 2
        assert all(c["status"] == "backlog" for c in result)

    def test_list_cards_filtered_by_platform(self, db_path):
        create_card_only("investigate", ["android"], "android card")
        create_card_only("jira-sdlc", ["ios"], "ios card")
        create_card_only("investigate", ["kmp"], "kmp card")

        result = list_cards_filtered(platform="android")
        assert len(result) >= 1
        for c in result:
            plats = c.get("platforms", "[]")
            import json
            assert "android" in (json.loads(plats) if isinstance(plats, str) else plats)

    def test_list_cards_filtered_combined(self, db_path):
        create_card_only("investigate", ["android"], "investigate android")
        create_card_only("jira-sdlc", ["android"], "jira android")
        create_card_only("investigate", ["ios"], "investigate ios")

        result = list_cards_filtered(pipeline="investigate", platform="android")
        assert len(result) == 1
        assert result[0]["pipeline_name"] == "investigate"

    def test_list_cards_filtered_no_match(self, db_path):
        create_card_only("investigate", ["android"], "only card")
        result = list_cards_filtered(pipeline="nonexistent")
        assert result == []

    def test_list_cards_filtered_all(self, db_path):
        create_card_only("investigate", ["android"], "card a")
        create_card_only("jira-sdlc", ["ios"], "card b")
        result = list_cards_filtered()
        assert len(result) >= 2
