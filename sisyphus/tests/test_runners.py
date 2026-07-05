"""Tests for core/runners.py — CRUD + health check."""
import asyncio
from core.runners import (
    init_runners_table,
    add_runner,
    get_runner,
    get_runner_by_name,
    get_default_runner,
    list_runners,
    update_runner,
    delete_runner,
    set_default_runner,
    health_check,
)


class TestRunnerCRUD:
    def test_init_table_and_seed(self, db_path):
        init_runners_table()
        runners = list_runners()
        assert len(runners) >= 1
        default = get_default_runner()
        assert default is not None
        assert default["is_default"] == 1

    def test_add_runner(self, db_path):
        init_runners_table()
        r = add_runner("test-runner", "test-binary", "custom", "gpt-4", "API_KEY", "https://api.test.com")
        assert r["name"] == "test-runner"
        assert r["binary"] == "test-binary"
        assert r["type"] == "custom"
        assert r["model"] == "gpt-4"
        assert r["api_key_env"] == "API_KEY"
        assert r["base_url"] == "https://api.test.com"
        assert r["is_default"] == 0

    def test_add_runner_as_default(self, db_path):
        init_runners_table()
        r = add_runner("new-default", "new-binary", is_default=True)
        assert r["is_default"] == 1
        default = get_default_runner()
        assert default["name"] == "new-default"

    def test_get_runner_by_name(self, db_path):
        init_runners_table()
        add_runner("find-me", "find-binary")
        r = get_runner_by_name("find-me")
        assert r is not None
        assert r["name"] == "find-me"

    def test_get_runner_by_name_missing(self, db_path):
        init_runners_table()
        assert get_runner_by_name("nonexistent") is None

    def test_get_default_runner_falls_back(self, db_path):
        init_runners_table()
        default = get_default_runner()
        assert default is not None

    def test_update_runner(self, db_path):
        init_runners_table()
        r = add_runner("updatable", "old-binary")
        updated = update_runner(r["id"], name="updated-name", model="claude-4")
        assert updated["name"] == "updated-name"
        assert updated["model"] == "claude-4"
        assert updated["binary"] == "old-binary"

    def test_set_default_runner(self, db_path):
        init_runners_table()
        r1 = add_runner("runner-a", "binary-a")
        r2 = add_runner("runner-b", "binary-b")
        set_default_runner(r1["id"])
        d = get_default_runner()
        assert d["id"] == r1["id"]

    def test_delete_runner(self, db_path):
        init_runners_table()
        r = add_runner("to-delete", "del-binary")
        rid = r["id"]
        delete_runner(rid)
        assert get_runner(rid) is None

    def test_list_runners(self, db_path):
        init_runners_table()
        count_before = len(list_runners())
        add_runner("listed", "list-binary")
        assert len(list_runners()) == count_before + 1


class TestRunnerHealth:
    def test_health_check_updates_status(self, db_path):
        init_runners_table()
        r = add_runner("health-test", "opencode", model="test")
        result = asyncio.run(health_check(r["id"]))
        assert result["status"] in ("healthy", "unhealthy")
        assert result["checked_at"] is not None
        updated = get_runner(r["id"])
        assert updated["health_status"] in ("healthy", "unhealthy")

    def test_health_check_missing_runner(self, db_path):
        init_runners_table()
        result = asyncio.run(health_check("nonexistent-id"))
        assert result["status"] == "error"
