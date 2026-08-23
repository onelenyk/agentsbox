"""Tests for core/worker.py — spawning a role-scoped worker.

launch_agent (the real subprocess spawner) is mocked; these tests verify the
wiring around it: worktree/opencode.json setup for repo-backed tasks, the
worker_started message, and the crash-fallback detection (session still
active after the process exits = it never called finish()).
"""
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from core import sessions, store
from core.agents import update_agent
from core.launcher import AgentResult
from core.worker import cleanup_task, spawn_worker


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

    @pytest.mark.asyncio
    async def test_named_but_unresolvable_repo_fails_never_runs_unconfined(self, db_path):
        # A task that names a repo the worker can't prepare must FAIL — never
        # silently fall through to the repo-less scratch dir, where the agent's
        # native tools would run with no git isolation and could reach the real
        # source repo on disk. (This is exactly how a live run once committed to
        # the wrong repo's main.)
        task = store.create_task("edit something", repo="ghost-repo")   # never registered
        mock_launch = AsyncMock(return_value=AgentResult(status="completed", summary="ok"))
        with patch("core.worker.launch_agent", mock_launch):
            with pytest.raises(ValueError, match="ghost-repo"):
                await spawn_worker(task["id"], "engineer", "do it", repo_name="ghost-repo")
        mock_launch.assert_not_called()   # the agent never started


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
        agent_cfg = config["agent"]["sisyphus-worker"]
        assert agent_cfg["mode"] == "primary"
        # The model must be written in: a primary agent without one makes
        # `opencode run` hang with no output at all, which surfaces to the user
        # only as "decider produced no decision".
        assert agent_cfg.get("model")

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


def test_decider_prompt_pins_the_agent_roster(db_path):
    from core.worker import _build_decider_prompt
    from core.agents import get_agent
    p = _build_decider_prompt(get_agent("orchestrator"), "the thread",
                              ["explorer", "engineer", "reviewer"])
    assert "explorer, engineer, reviewer" in p
    assert "MUST be one of" in p   # tells the model not to invent agent names


class TestDeciderRetry:
    @pytest.mark.asyncio
    async def test_retries_until_a_decision_is_recorded(self, db_path, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        from core.worker import spawn_decider
        task = store.create_task("x", repo="sisyphus")
        calls = {"n": 0}

        async def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] >= 2:   # the agent only calls a decide tool on attempt 2
                for s in list(sessions._SESSIONS.values()):
                    if s["task_id"] == task["id"]:
                        s["decision"] = {"action": "note", "why": "ok"}
            return AgentResult(status="completed", summary="ok")

        with patch("core.worker.launch_agent", AsyncMock(side_effect=flaky)):
            dec = await spawn_decider(task["id"], "orchestrator", "thread")
        assert dec == {"action": "note", "why": "ok"}
        assert calls["n"] == 2   # retried once


class TestModelOverride:
    @pytest.mark.asyncio
    async def test_spawn_worker_passes_model_override(self, db_path):
        store.create_task("test goal")
        agent_name = "explorer"
        update_agent(agent_name, model="gpt-5-agent-override")
        task = store.create_task("test model override")
        mock_launch = AsyncMock(return_value=AgentResult(status="completed", summary="ok"))

        with patch("core.worker.launch_agent", mock_launch):
            await spawn_worker(task["id"], agent_name, "test instruction")

        mock_launch.assert_called_once()
        kwargs = mock_launch.call_args.kwargs
        assert kwargs.get("model_override") == "gpt-5-agent-override"

    @pytest.mark.asyncio
    async def test_spawn_decider_passes_model_override(self, db_path, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        from core.agents import init_agents_table
        init_agents_table()
        from core.worker import spawn_decider
        update_agent("orchestrator", model="gpt-5-decider-override")
        task = store.create_task("coordinate", repo="sisyphus")
        mock_launch = AsyncMock(return_value=AgentResult(status="completed", summary="ok"))

        with patch("core.worker.launch_agent", mock_launch):
            await spawn_decider(task["id"], "orchestrator", "thread")

        kwargs = mock_launch.call_args.kwargs
        assert kwargs.get("model_override") == "gpt-5-decider-override"


class TestCleanupSalvage:

    def _make_worktree(self, base: Path, task_id: str, role: str, dirty: bool) -> Path:
        wt = base / "repos" / "worktrees" / f"{task_id}_{role}"
        wt.mkdir(parents=True)
        subprocess.run(["git", "init"], cwd=str(wt), capture_output=True, text=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test"], cwd=str(wt),
            capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "test"], cwd=str(wt),
            capture_output=True, text=True,
        )
        (wt / "file.txt").write_text("initial")
        subprocess.run(["git", "add", "file.txt"], cwd=str(wt), capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(wt), capture_output=True, text=True)
        if dirty:
            (wt / "file.txt").write_text("modified")
        return wt

    def test_dirty_worktree_salvages_patch(self, db_path, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        task = store.create_task("test salvage")
        wt = self._make_worktree(tmp_path, task["id"], "engineer", dirty=True)

        cleanup_task(task["id"])

        attachments_dir = tmp_path / "tasks" / task["id"] / "attachments"
        patch_file = attachments_dir / f"salvaged-{wt.name}.patch"
        assert patch_file.is_file()
        content = patch_file.read_text()
        assert "modified" in content

    def test_runner_config_alone_is_not_salvaged(self, db_path, tmp_path, monkeypatch):
        """opencode.json / .opencode / .claude are written into every worktree
        by the launcher itself — a tree dirty with ONLY those has no work to
        salvage. Every real run got 'salvaged' before this, one WARNING per
        worker per day, burying the true salvages."""
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        task = store.create_task("runner config only")
        wt = self._make_worktree(tmp_path, task["id"], "reviewer", dirty=False)
        (wt / "opencode.json").write_text("{}")
        (wt / ".opencode").mkdir()
        (wt / ".opencode" / "agent.md").write_text("persona")
        (wt / ".claude").mkdir()
        (wt / ".claude" / "settings.json").write_text("{}")

        cleanup_task(task["id"])

        attachments_dir = tmp_path / "tasks" / task["id"] / "attachments"
        assert not attachments_dir.exists() or not list(attachments_dir.iterdir())

    def test_real_work_is_still_salvaged_next_to_runner_config(self, db_path, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        task = store.create_task("real work plus config")
        wt = self._make_worktree(tmp_path, task["id"], "engineer", dirty=True)
        (wt / "opencode.json").write_text("{}")

        cleanup_task(task["id"])

        patch_file = tmp_path / "tasks" / task["id"] / "attachments" / f"salvaged-{wt.name}.patch"
        assert patch_file.is_file()
        content = patch_file.read_text()
        assert "modified" in content
        assert "opencode.json" not in content

    def test_clean_worktree_produces_no_attachment(self, db_path, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        task = store.create_task("test clean")
        wt = self._make_worktree(tmp_path, task["id"], "engineer", dirty=False)

        cleanup_task(task["id"])

        attachments_dir = tmp_path / "tasks" / task["id"] / "attachments"
        assert not attachments_dir.exists() or not list(attachments_dir.iterdir())


class TestCleanupRetainsRunLogs:
    def test_run_logs_survive_normal_cleanup(self, db_path, tmp_path, monkeypatch):
        # Run logs are the only forensic record of what a worker did — a
        # terminal-status cleanup must keep them (only deep delete reclaims).
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        task = store.create_task("keep my logs")
        run_dir = tmp_path / "runs" / task["id"] / "logs"
        run_dir.mkdir(parents=True)
        (run_dir / "engineer.log").write_text("evidence")

        cleanup_task(task["id"])
        assert (run_dir / "engineer.log").is_file()

        cleanup_task(task["id"], deep=True)
        assert not (tmp_path / "runs" / task["id"]).exists()


class TestSetAccessWriteAlias:
    def test_write_level_actually_chmods(self, tmp_path):
        # core.worker calls set_access(path, "write"); an unknown level is a
        # silent no-op, so "write" must be a real mode (alias of "edit").
        import os as _os
        from core.worktree import set_access

        d = tmp_path / "wt"; d.mkdir()
        f = d / "a.txt"; f.write_text("x")
        f.chmod(0o444)

        set_access(str(d), "write")
        assert _os.access(str(f), _os.W_OK), "file left read-only — 'write' was a no-op"


class TestWorkerEndDiagnosis:
    """Every runner failure reached the thread as the single word "no output",
    which covers four faults needing four different fixes. These pin the
    distinctions apart."""

    def _result(self, **kw):
        from core.launcher import AgentResult
        return AgentResult(**kw)

    def test_stderr_is_surfaced_when_nothing_reached_stdout(self):
        from core.worker import worker_end_message
        msg = worker_end_message("explorer", self._result(
            status="completed", raw_stdout="",
            raw_stderr="Superset: claude not found in PATH."))
        assert "not found in PATH" in msg

    def test_total_silence_points_at_the_smoke_test(self):
        from core.worker import worker_end_message
        msg = worker_end_message("explorer", self._result(
            status="completed", raw_stdout="", raw_stderr=""))
        assert "no output at all" in msg
        assert "smoke test" in msg.lower()

    def test_work_done_but_unreported_is_not_an_infrastructure_fault(self):
        """The agent ran a full turn and just never called finish — a prompt or
        tool-visibility problem, not a broken runner."""
        from core.worker import worker_end_message
        msg = worker_end_message("engineer", self._result(
            status="completed", raw_stdout='{"type":"text"}', usage={"steps": 7}))
        assert "7 model step" in msg
        assert "never called its finishing tool" in msg

    def test_output_without_model_steps_is_its_own_case(self):
        from core.worker import worker_end_message
        msg = worker_end_message("engineer", self._result(
            status="completed", raw_stdout="starting up", usage={"steps": 0}))
        assert "no model steps were billed" in msg

    def test_an_explicit_error_still_wins(self):
        from core.worker import worker_end_message
        msg = worker_end_message("engineer", self._result(
            status="timeout", error="Agent exceeded 1800s timeout", raw_stdout="x"))
        assert "exceeded 1800s timeout" in msg

    def test_cancellation_is_still_reported_as_not_a_failure(self):
        from core.worker import worker_end_message
        msg = worker_end_message("engineer", self._result(status="cancelled"))
        assert "not a" in msg and "failure" in msg


class TestDeliveredWorkGuard:
    """A run that opened its MR and then stalled must not read as lost work —
    the orchestrator respawned a second engineer to redo a finished one-line
    change (task-60088d8f) off exactly that wording."""

    class _R:
        status = "timeout"
        error = ""
        raw_stdout = "output"
        raw_stderr = ""
        usage = {"steps": 5}

    def _task_with_run(self, mr_in_run: bool, mr_before_run: bool = False):
        from core import store
        task = store.create_task("guard", repo="sisyphus")
        if mr_before_run:
            store.post_message(task["id"], "system", kind="worker_started", text="run 1")
            store.post_message(task["id"], "engineer", kind="mr", text="old MR")
        store.post_message(task["id"], "system", kind="worker_started", text="run 2")
        if mr_in_run:
            store.post_message(task["id"], "engineer", kind="mr", text="the MR")
        return task

    def test_timeout_after_open_mr_says_work_is_delivered(self, db_path):
        from core.worker import worker_end_message
        task = self._task_with_run(mr_in_run=True)
        msg = worker_end_message("engineer", self._R(), task_id=task["id"])
        assert "Do NOT spawn another engineer" in msg

    def test_timeout_without_an_mr_stays_a_plain_failure(self, db_path):
        from core.worker import worker_end_message
        task = self._task_with_run(mr_in_run=False)
        msg = worker_end_message("engineer", self._R(), task_id=task["id"])
        assert "Do NOT spawn another engineer" not in msg

    def test_an_mr_from_an_earlier_run_does_not_count(self, db_path):
        """A reviewer sent the old MR back; the fresh run that then failed did
        not deliver anything — claiming it did would strand the fix."""
        from core.worker import worker_end_message
        task = self._task_with_run(mr_in_run=False, mr_before_run=True)
        msg = worker_end_message("engineer", self._R(), task_id=task["id"])
        assert "Do NOT spawn another engineer" not in msg
