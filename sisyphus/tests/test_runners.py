"""Tests for core/runners.py — CRUD + health check."""
import asyncio

import pytest
from core.db import get_conn
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
        r = add_runner("test-runner", "test-binary", "claude-cli", "opus", "API_KEY", "https://api.test.com")
        assert r["name"] == "test-runner"
        assert r["binary"] == "test-binary"
        assert r["type"] == "claude-cli"
        assert r["model"] == "opus"
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
        """With an enabled runner but none flagged default, fall back to the
        oldest enabled one. The seeded runner IS the default, so clear the flag
        first — otherwise the first query hits and the fallback never runs
        (which is why this test passed while that branch was crashing)."""
        init_runners_table()
        seeded = get_default_runner()
        set_default_runner(seeded["id"])
        conn = get_conn()
        conn.execute("UPDATE runners SET is_default = 0")
        conn.commit()
        conn.close()

        default = get_default_runner()
        assert default is not None
        assert default["id"] == seeded["id"]

    def test_get_default_runner_none_when_all_disabled(self, db_path):
        init_runners_table()
        conn = get_conn()
        conn.execute("UPDATE runners SET enabled = 0, is_default = 0")
        conn.commit()
        conn.close()
        assert get_default_runner() is None

    def test_update_runner(self, db_path):
        init_runners_table()
        r = add_runner("updatable", "old-binary")
        updated = update_runner(r["id"], name="updated-name", model="claude-4")
        assert updated["name"] == "updated-name"
        assert updated["model"] == "claude-4"
        assert updated["binary"] == "old-binary"

    def test_timeout_columns_are_nullable_and_stored(self, db_path):
        init_runners_table()
        r = add_runner("timed", "opencode", "opencode",
                       idle_timeout=900, timeout=2700)
        assert r["idle_timeout"] == 900
        assert r["timeout"] == 2700
        # Blank (None) on create means "use the type default".
        plain = add_runner("untimed", "opencode", "opencode")
        assert plain["idle_timeout"] is None
        assert plain["timeout"] is None

    def test_update_can_set_and_clear_timeout_overrides(self, db_path):
        init_runners_table()
        r = add_runner("timed", "opencode", "opencode")
        set_row = update_runner(r["id"], idle_timeout=1200, timeout=3600)
        assert set_row["idle_timeout"] == 1200 and set_row["timeout"] == 3600
        # None is a REAL value for the nullable columns — it clears the
        # override back to the type default, unlike the other fields where
        # None means "leave alone".
        cleared = update_runner(r["id"], idle_timeout=None, timeout=None)
        assert cleared["idle_timeout"] is None
        assert cleared["timeout"] is None

    def test_set_default_runner(self, db_path):
        init_runners_table()
        r1 = add_runner("runner-a", "binary-a")
        add_runner("runner-b", "binary-b")
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

    def test_missing_api_key_env_is_a_note_not_a_failure(self, db_path, monkeypatch):
        """Both supported CLIs hold their own login, and the daemon's env is
        nearly empty under launchd — so an unset variable must not report a
        working runtime as unhealthy."""
        init_runners_table()
        monkeypatch.delenv("SOME_ABSENT_KEY", raising=False)
        # The claim is about the key, not the binary: CI has no opencode
        # installed, and a missing binary is (rightly) fatal on its own.
        import core.runner_detect
        monkeypatch.setattr(core.runner_detect, "resolve", lambda b: "/usr/bin/true")
        r = add_runner("with-key-env", "opencode", "opencode",
                       api_key_env="SOME_ABSENT_KEY")
        result = asyncio.run(health_check(r["id"]))
        assert result["status"] == "healthy"
        row = get_runner(r["id"])
        assert row["health_status"] == "healthy"
        # Still surfaced, just not as a failure.
        assert "SOME_ABSENT_KEY" in (row["health_error"] or "")
        assert "auth.json" in row["health_error"]

    def test_missing_binary_is_still_fatal(self, db_path):
        init_runners_table()
        r = add_runner("ghost", "definitely-not-installed-xyz", "opencode")
        result = asyncio.run(health_check(r["id"]))
        assert result["status"] == "unhealthy"
        assert "not found" in result["error"]

    def test_health_check_missing_runner(self, db_path):
        init_runners_table()
        result = asyncio.run(health_check("nonexistent-id"))
        assert result["status"] == "error"


class TestTheInstalledCliVersionIsWatched:
    """Every launch flag this daemon uses belongs to a CLI it does not own. When
    opencode renames one, runs break inside a worker nobody is watching and the
    failure reads as the model misbehaving. Health says which version is there."""

    def test_a_drifted_version_is_a_note_not_a_failure(self):
        from core import runner_profiles as rp
        assert rp.version_note("opencode", rp.VERIFIED_VERSIONS["opencode"]) is None
        note = rp.version_note("opencode", "99.0.0")
        assert note and "99.0.0" in note and rp.VERIFIED_VERSIONS["opencode"] in note

    def test_an_unreadable_version_says_nothing(self):
        from core import runner_profiles as rp
        assert rp.parse_version("command not found") == ""
        assert rp.version_note("opencode", "") is None

    @pytest.mark.asyncio
    async def test_health_reports_the_drift_and_stays_healthy(self, db_path, monkeypatch):
        from unittest.mock import patch
        from types import SimpleNamespace
        from core import runners

        r = runners.add_runner("probe", binary="opencode", runner_type="opencode")
        monkeypatch.setattr("core.runner_detect.resolve", lambda b: "/abs/opencode")
        with patch.object(runners.subprocess, "run",
                          return_value=SimpleNamespace(stdout="99.0.0\n", stderr="", returncode=0)):
            result = await runners.health_check(r["id"])

        assert result["status"] == "healthy"       # a new CLI version is not a broken runner
        assert "99.0.0" in result["error"]


class TestHealthTellsTheTruthAboutABaseUrl:
    """The one word this page exists to get right is "healthy"."""

    @pytest.mark.asyncio
    async def test_an_http_status_means_the_server_is_there(self, db_path, monkeypatch):
        import urllib.error
        from core import runners

        r = runners.add_runner("probe-url", binary="opencode", runner_type="opencode",
                               base_url="https://openrouter.ai/api/v1")
        monkeypatch.setattr("core.runner_detect.resolve", lambda b: "/abs/opencode")
        monkeypatch.setattr("core.runners._version_note", lambda runner, path: None)

        def head(req, timeout=5):      # what OpenRouter actually answers to HEAD
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

        monkeypatch.setattr(runners.urllib.request, "urlopen", head)
        result = await runners.health_check(r["id"])
        assert result["status"] == "healthy", result["error"]

    @pytest.mark.asyncio
    async def test_a_transport_failure_is_still_unhealthy(self, db_path, monkeypatch):
        import urllib.error
        from core import runners

        r = runners.add_runner("probe-dead", binary="opencode", runner_type="opencode",
                               base_url="https://nope.invalid/v1")
        monkeypatch.setattr("core.runner_detect.resolve", lambda b: "/abs/opencode")
        monkeypatch.setattr("core.runners._version_note", lambda runner, path: None)

        def head(req, timeout=5):
            raise urllib.error.URLError("nodename nor servname provided")

        monkeypatch.setattr(runners.urllib.request, "urlopen", head)
        result = await runners.health_check(r["id"])
        assert result["status"] == "unhealthy"
        assert "unreachable" in result["error"]
