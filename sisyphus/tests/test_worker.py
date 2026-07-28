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
    async def test_spawns_in_scratch_dir_and_posts_started(self, db_path, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        task = store.create_task("investigate something")
        mock_launch = AsyncMock(return_value=AgentResult(status="completed", summary="ok"))

        with patch("core.worker.launch_agent", mock_launch):
            await spawn_worker(task["id"], "explorer", "look around")

        call = mock_launch.call_args
        worktree = call.args[2]  # worktree_path
        assert worktree == str(tmp_path / "scratch" / task["id"])
        # a private scratch cwd with our agent-MCP config, not a shared /tmp
        assert (tmp_path / "scratch" / task["id"] / "opencode.json").exists()

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
             patch("core.git_ops.ensure_task_branch", return_value="sisyphus/" + task["id"]), \
             patch("core.worker.create_worktree", return_value=str(fake_worktree)) as mock_ct, \
             patch("core.worker.set_access") as mock_access, \
             patch("core.worker.launch_agent", mock_launch):
            await spawn_worker(task["id"], "engineer", "fix it", repo_name="myrepo")

        # engineer checks out the task branch ATTACHED (detach=False) so its
        # commits advance the branch; read-only roles would be detached.
        assert mock_ct.call_args.kwargs.get("detach") is False

        config = json.loads((fake_worktree / "opencode.json").read_text())
        # Named "sisyphus_agent", not "sisyphus" — confirmed live that this
        # machine already has a global/per-project MCP entry literally named
        # "sisyphus" (the repo's own control-plane mcp_server.py, set up for
        # chat use), which silently wins any name collision and leaves our
        # tools completely absent from the model's real tool list.
        mcp = config["mcp"]["sisyphus_agent"]
        # Local (stdio) MCP, not remote (HTTP) — opencode's remote transport
        # proved unreliable; local mirrors mcp_server.py's proven pattern.
        assert mcp["type"] == "local"
        assert mcp["command"][-1].endswith("agent_mcp_stdio.py")
        assert mcp["environment"]["SISYPHUS_SESSION_TOKEN"]
        assert mcp["environment"]["SISYPHUS_DAEMON_URL"]
        # "plugin": [] (not --pure) is what disables the user's global
        # oh-my-openagent plugin — --pure was confirmed (via live opencode
        # debug logs) to also strip project-local MCP servers, killing our
        # own tools along with the plugin.
        assert config["plugin"] == []
        # Our own agent, defined entirely in project config with an explicit
        # "primary" mode — not a borrowed built-in name. A built-in name's
        # primary/subagent classification lives in the GLOBAL config and can
        # be changed there outside anything we control (confirmed live: a
        # "build" agent that worked one day was reclassified as a subagent
        # the next, silently falling back to the persona this exists to
        # avoid).
        assert config["agent"] == {"sisyphus-worker": {"mode": "primary"}}

        # Worktrees are always writable regardless of role (opencode needs to
        # write its own .opencode/ session state) — access control for what a
        # role may DO lives in its tool allowlist, not filesystem permissions.
        mock_access.assert_called_once_with(str(fake_worktree), "write")
        assert mock_launch.call_args.args[2] == str(fake_worktree)
        # pure is no longer passed at all — --pure strips project-local MCP
        # servers, which would take our own tools down with the plugin.
        assert "pure" not in mock_launch.call_args.kwargs
        # agent=our own agent name, matching the one defined in the config
        # above — never a name borrowed from opencode's own built-ins.
        assert mock_launch.call_args.kwargs["agent"] == "sisyphus-worker"


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


class TestSpawnDecider:
    @pytest.mark.asyncio
    async def test_returns_decision_recorded_by_the_agent(self, db_path, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        from core.agents import init_agents_table
        init_agents_table()
        from core.worker import spawn_decider
        task = store.create_task("coordinate", repo="sisyphus")

        async def fake_launch(*a, **k):
            # simulate the orchestrator agent calling a decide_* tool
            for s in list(sessions._SESSIONS.values()):
                if s["task_id"] == task["id"]:
                    s["decision"] = {"action": "note", "why": "thinking"}
            return AgentResult(status="completed", summary="ok")

        with patch("core.worker.launch_agent", AsyncMock(side_effect=fake_launch)):
            dec = await spawn_decider(task["id"], "orchestrator", "the thread")
        assert dec == {"action": "note", "why": "thinking"}

    @pytest.mark.asyncio
    async def test_returns_none_when_agent_records_nothing(self, db_path, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        from core.agents import init_agents_table
        init_agents_table()
        from core.worker import spawn_decider
        task = store.create_task("coordinate", repo="sisyphus")
        with patch("core.worker.launch_agent",
                   AsyncMock(return_value=AgentResult(status="completed", summary="ok"))):
            dec = await spawn_decider(task["id"], "orchestrator", "thread")
        assert dec is None

    @pytest.mark.asyncio
    async def test_unknown_agent_returns_none(self, db_path):
        from core.worker import spawn_decider
        task = store.create_task("x", repo="sisyphus")
        assert await spawn_decider(task["id"], "nope", "thread") is None
