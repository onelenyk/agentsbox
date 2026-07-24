"""Tests for core/worker.py — spawning a role-scoped worker.

launch_agent (the real subprocess spawner) is mocked; these tests verify the
wiring around it: worktree/opencode.json setup for repo-backed tasks, the
worker_started message, and the crash-fallback detection (session still
active after the process exits = it never called finish()).
"""
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from core import sessions, store
from core.launcher import AgentResult
from core.worker import spawn_worker


@pytest.fixture(autouse=True)
def _clear_sessions():
    sessions.clear()
    yield
    sessions.clear()


class TestNoRepo:
    @pytest.mark.asyncio
    async def test_spawns_in_tmp_and_posts_started(self, db_path):
        task = store.create_task("investigate something")
        mock_launch = AsyncMock(return_value=AgentResult(status="completed", summary="ok"))

        with patch("core.worker.launch_agent", mock_launch):
            await spawn_worker(task["id"], "explorer", "look around")

        call = mock_launch.call_args
        assert call.args[2] == "/tmp"  # worktree_path

        thread = store.get_thread(task["id"])
        kinds = [m["kind"] for m in thread]
        assert "worker_started" in kinds
        assert thread[0]["text"] == "spawning explorer: look around"

    @pytest.mark.asyncio
    async def test_prompt_includes_goal_instruction_and_thread(self, db_path):
        task = store.create_task("count the files")
        store.post_message(task["id"], "human", text="please be thorough")
        mock_launch = AsyncMock(return_value=AgentResult(status="completed", summary="ok"))

        with patch("core.worker.launch_agent", mock_launch):
            await spawn_worker(task["id"], "explorer", "count now")

        prompt = mock_launch.call_args.args[1]
        assert "count the files" in prompt
        assert "count now" in prompt
        assert "please be thorough" in prompt
        assert "finish" in prompt

    @pytest.mark.asyncio
    async def test_unknown_task_raises(self, db_path):
        with pytest.raises(ValueError):
            await spawn_worker("task-nope", "explorer", "x")

    @pytest.mark.asyncio
    async def test_unknown_role_raises(self, db_path):
        task = store.create_task("g")
        with pytest.raises(ValueError):
            await spawn_worker(task["id"], "wizard", "x")


class TestRepoBackedWorktree:
    @pytest.mark.asyncio
    async def test_writes_opencode_config_pointing_at_daemon(self, db_path, tmp_path, monkeypatch):
        task = store.create_task("fix the bug", repo="myrepo")
        mock_launch = AsyncMock(return_value=AgentResult(status="completed", summary="ok"))
        fake_worktree = tmp_path / "worktree"
        fake_worktree.mkdir()

        with patch("core.worker.get_repo", return_value={"source": "https://example.com/r.git", "branch": "main"}), \
             patch("core.worker.init_bare_clone"), \
             patch("core.worker.create_worktree", return_value=str(fake_worktree)), \
             patch("core.worker.set_access") as mock_access, \
             patch("core.worker.launch_agent", mock_launch):
            await spawn_worker(task["id"], "engineer", "fix it", repo_name="myrepo")

        config = json.loads((fake_worktree / "opencode.json").read_text())
        mcp = config["mcp"]["sisyphus"]
        assert mcp["type"] == "remote"
        assert mcp["url"].endswith("/mcp/agent")
        assert mcp["headers"]["Authorization"].startswith("Bearer ")

        mock_access.assert_called_once_with(str(fake_worktree), "write")  # engineer -> write
        assert mock_launch.call_args.args[2] == str(fake_worktree)


class TestCrashFallback:
    @pytest.mark.asyncio
    async def test_session_still_active_after_exit_posts_worker_finished(self, db_path):
        """The worker never called finish() via MCP (e.g. crashed) — the
        session is still active when launch_agent returns, so spawn_worker
        must record that as a worker_finished fallback."""
        task = store.create_task("g")
        mock_launch = AsyncMock(
            return_value=AgentResult(status="timeout", error="Agent exceeded 300s idle timeout")
        )

        with patch("core.worker.launch_agent", mock_launch):
            await spawn_worker(task["id"], "explorer", "x")

        kinds_and_text = [(m["kind"], m["text"]) for m in store.get_thread(task["id"])]
        assert any(k == "worker_finished" and "idle timeout" in t for k, t in kinds_and_text)

    @pytest.mark.asyncio
    async def test_session_revoked_when_worker_never_finishes(self, db_path):
        task = store.create_task("g")
        mock_launch = AsyncMock(return_value=AgentResult(status="failed", error="crash"))

        with patch("core.worker.launch_agent", mock_launch):
            await spawn_worker(task["id"], "explorer", "x")

        # No session should remain active for this task after a crash.
        active = [s for s in sessions._SESSIONS.values() if s["task_id"] == task["id"]]
        assert active == []

    @pytest.mark.asyncio
    async def test_normal_finish_does_not_double_post(self, db_path):
        """If the worker DID call finish() (simulated by revoking the session
        mid-mock, as the real MCP handler would), spawn_worker must not also
        post a fallback worker_finished."""
        task = store.create_task("g")

        async def _finishing_launch(*args, **kwargs):
            # Simulate the agent calling finish() via MCP during the run:
            # find the just-minted session for this task and revoke it,
            # exactly like web/agent_mcp.py::_finish does.
            for tok, sess in list(sessions._SESSIONS.items()):
                if sess["task_id"] == task["id"]:
                    store.post_message(task["id"], sess["role"], kind="result", text="done")
                    sessions.revoke(tok)
            return AgentResult(status="completed", summary="done")

        with patch("core.worker.launch_agent", _finishing_launch):
            await spawn_worker(task["id"], "explorer", "x")

        kinds = [m["kind"] for m in store.get_thread(task["id"])]
        assert kinds.count("worker_finished") == 0  # only the real "result", no fallback
        assert "result" in kinds
