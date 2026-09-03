"""Integration tests for launch_agent using mock agent scripts.

launch_agent no longer parses result.json or validates against a schema —
success/failure for MCP-based workers is determined by the caller
(core.worker) from whether the agent-MCP session was closed via a real tool
call. These tests cover the parts of launch_agent that are still generic:
subprocess timeout/idle-timeout/cancel/crash handling, independent of any
reporting contract.
"""
import asyncio
import os
import pathlib
import shutil
import tempfile

import pytest

from core.launcher import launch_agent
from core.runners import init_runners_table, add_runner

MOCK_DIR = pathlib.Path(__file__).parent / "mock_agents"

# ---------------------------------------------------------------------------
# Helper: create a "run" Python shim in worktree_path so that
#   python3 run <mock_script> --output-dir ... "prompt" --format json --auto
# actually runs the mock script with only its recognised arguments.
# ---------------------------------------------------------------------------
_RUN_SHIM = '''
import sys, os

if len(sys.argv) < 2:
    sys.exit(0)

script = sys.argv[1]

# The launcher constructs:
#   [binary, "run"] + extra_args + [prompt, "--format", "json", "--auto"]
# extra_args are flag-value pairs (e.g. --output-dir /tmp/out --sleep 2).
# Walk sys.argv[2:] collecting flag-value pairs; stop at the prompt (first
# non-flag argument that isn't the value of a preceding flag).
filtered = []
i = 2
while i < len(sys.argv):
    arg = sys.argv[i]
    if arg.startswith("--"):
        filtered.append(arg)
        i += 1
        # peek at value
        if i < len(sys.argv) and not sys.argv[i].startswith("--"):
            filtered.append(sys.argv[i])
            i += 1
    else:
        # This is the prompt text — skip it and all following launcher args.
        break

sys.argv = [script] + filtered

with open(script) as f:
    code = compile(f.read(), script, "exec")
    exec(code, {"__name__": "__main__"})
'''


def _setup_run_shim(worktree):
    """Write the 'run' shim into *worktree* so python3 can find it."""
    (pathlib.Path(worktree) / "run").write_text(_RUN_SHIM)


def _make_output_dir(run_id, step_id):
    """Build an output_dir that _derive_run_id can parse back to run_id.
    No DB row needed — launch_agent doesn't touch the database at all."""
    workspace_root = os.environ["WORKSPACE_ROOT"]
    d = pathlib.Path(workspace_root) / "runs" / run_id / "resources" / step_id
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


# =========================================================================
# Tests
# =========================================================================


class TestMockAgentTimeout:

    @pytest.mark.asyncio
    async def test_agent_timeout(self, db_path, monkeypatch):
        # launch_agent wraps the runner in sandbox-exec; inside the worker
        # sandbox sandbox-exec cannot be nested (it exits immediately with
        # "Operation not permitted"), which masks the timeout under test. These
        # tests exercise the timeout loop, not the sandbox, so disable it here.
        monkeypatch.setenv("SISYPHUS_SANDBOX", "0")
        monkeypatch.setenv("SISYPHUS_SANDBOX_REQUIRE", "0")  # waived: this test is about timeouts, not confinement

        worktree = pathlib.Path(os.environ["WORKSPACE_ROOT"]) / "worktree"
        worktree.mkdir(parents=True, exist_ok=True)
        _setup_run_shim(worktree)

        output_dir = _make_output_dir("run-timeout", "timeout")

        init_runners_table()
        add_runner(
            "mock-timeout",
            "python3",
            extra_args=[
                str(MOCK_DIR / "mock_agent_block.py"),
                "--output-dir", output_dir,
            ],
        )

        agent_def = {"name": "Test Timeout", "runner": "mock-timeout"}
        result = await launch_agent(
            agent_def, "anything", str(worktree), output_dir,
            timeout=1, step_id="timeout",
        )

        assert result.status == "timeout"


class TestIdleTimeout:

    @pytest.mark.asyncio
    async def test_idle_timeout_fires_on_silent_hang(self, db_path, monkeypatch):
        """No output at all for longer than idle_timeout → killed, even under total timeout."""
        # See test_agent_timeout: sandbox-exec can't nest in the worker sandbox,
        # so disable the sandbox and let the blocking mock agent exercise the
        # idle-timeout path directly.
        monkeypatch.setenv("SISYPHUS_SANDBOX", "0")
        monkeypatch.setenv("SISYPHUS_SANDBOX_REQUIRE", "0")  # waived: this test is about timeouts, not confinement

        worktree = pathlib.Path(os.environ["WORKSPACE_ROOT"]) / "worktree"
        worktree.mkdir(parents=True, exist_ok=True)
        _setup_run_shim(worktree)

        output_dir = _make_output_dir("run-idle-hang", "idle-hang")

        init_runners_table()
        add_runner(
            "mock-idle-hang",
            "python3",
            extra_args=[
                str(MOCK_DIR / "mock_agent_block.py"),
                "--output-dir", output_dir,
            ],
        )

        agent_def = {"name": "Test Idle Hang", "runner": "mock-idle-hang"}
        result = await launch_agent(
            agent_def, "anything", str(worktree), output_dir,
            timeout=30, idle_timeout=1, step_id="idle-hang",
        )

        assert result.status == "timeout"
        assert "idle timeout" in result.error

    @pytest.mark.asyncio
    async def test_idle_timeout_does_not_fire_with_active_output(self, db_path):
        """Periodic output resets the idle clock, so a slow-but-alive agent isn't killed."""
        worktree = pathlib.Path(os.environ["WORKSPACE_ROOT"]) / "worktree"
        worktree.mkdir(parents=True, exist_ok=True)
        _setup_run_shim(worktree)

        output_dir = _make_output_dir("run-idle-active", "idle-active")

        init_runners_table()
        add_runner(
            "mock-idle-active",
            "python3",
            extra_args=[
                str(MOCK_DIR / "mock_agent_active.py"),
                "--output-dir", output_dir,
                "--ticks", "5",
                "--interval", "0.3",
            ],
        )

        agent_def = {"name": "Test Idle Active", "runner": "mock-idle-active"}
        result = await launch_agent(
            agent_def, "anything", str(worktree), output_dir,
            timeout=30, idle_timeout=1, step_id="idle-active",
        )

        assert result.status == "completed"


class TestNoResultJson:

    @pytest.mark.asyncio
    async def test_does_not_gate_on_result_json(self, db_path):
        """Workers report success via MCP tool calls, not result.json — the
        agent exiting cleanly without ever writing result.json must not be
        treated as a failure; the caller (core.worker) judges success from
        whether the MCP session was closed, not from launch_agent's status."""
        worktree = pathlib.Path(os.environ["WORKSPACE_ROOT"]) / "worktree-mcp"
        worktree.mkdir(parents=True, exist_ok=True)
        _setup_run_shim(worktree)

        output_dir = _make_output_dir("run-mcp-worker", "mcp-worker")

        init_runners_table()
        add_runner(
            "mock-mcp-worker",
            "python3",
            extra_args=[
                str(MOCK_DIR / "mock_agent_timeout.py"),
                "--output-dir", output_dir,
                "--sleep", "0",
            ],
        )

        agent_def = {"name": "Test MCP Worker", "runner": "mock-mcp-worker"}
        result = await launch_agent(
            agent_def, "anything", str(worktree), output_dir,
            timeout=30, step_id="mcp-worker",
        )

        assert result.status == "completed"
        assert not (pathlib.Path(output_dir) / "result.json").exists()


class TestCancelEvent:

    @pytest.mark.asyncio
    async def test_cancel_event(self, db_path):
        worktree = pathlib.Path(os.environ["WORKSPACE_ROOT"]) / "worktree"
        worktree.mkdir(parents=True, exist_ok=True)
        _setup_run_shim(worktree)

        output_dir = _make_output_dir("run-cancel", "cancel")

        init_runners_table()
        add_runner(
            "mock-cancel",
            "python3",
            extra_args=[
                str(MOCK_DIR / "mock_agent_timeout.py"),
                "--output-dir", output_dir,
                "--sleep", "30",
            ],
        )

        cancel_event = asyncio.Event()
        cancel_event.set()

        agent_def = {"name": "Test Cancel", "runner": "mock-cancel"}
        result = await launch_agent(
            agent_def, "anything", str(worktree), output_dir,
            timeout=30, cancel_event=cancel_event, step_id="cancel",
        )

        assert result.status == "cancelled"


class TestBinaryNotFound:

    @pytest.mark.asyncio
    async def test_binary_not_found(self, db_path):
        worktree = pathlib.Path(os.environ["WORKSPACE_ROOT"]) / "worktree"
        worktree.mkdir(parents=True, exist_ok=True)

        output_dir = _make_output_dir("run-bin", "bin")

        init_runners_table()
        add_runner(
            "mock-missing-bin",
            "nonexistent_binary_xyz",
            extra_args=[],
        )

        agent_def = {"name": "Test Missing Bin", "runner": "mock-missing-bin"}
        result = await launch_agent(
            agent_def, "anything", str(worktree), output_dir,
            timeout=30, step_id="bin",
        )

        assert result.status == "error"
        assert "not found" in result.error.lower() or "nonexistent" in result.error.lower()


class TestModelOverride:
    """launch_agent uses model_override when given."""

    @pytest.mark.asyncio
    async def test_override_passed_to_env_and_result(self, db_path):
        runner_name = "test_runner"
        init_runners_table()
        add_runner(runner_name, "python3", model="runner-default-model")

        worktree = tempfile.mkdtemp()
        output_dir = tempfile.mkdtemp()
        try:
            agent_def = {"name": "test-agent", "runner": runner_name}
            prompt = "echo hello"

            result = await launch_agent(
                agent_def, prompt, worktree, output_dir,
                runner_name=runner_name, model_override="gpt-5-override",
                timeout=10,
            )

            assert result.model == "gpt-5-override"
        finally:
            shutil.rmtree(worktree, ignore_errors=True)
            shutil.rmtree(output_dir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_no_override_uses_runner_model(self, db_path):
        runner_name = "test_runner"
        init_runners_table()
        add_runner(runner_name, "python3", model="runner-default-model")

        worktree = tempfile.mkdtemp()
        output_dir = tempfile.mkdtemp()
        try:
            agent_def = {"name": "test-agent", "runner": runner_name}
            prompt = "echo hello"

            result = await launch_agent(
                agent_def, prompt, worktree, output_dir,
                runner_name=runner_name, timeout=10,
            )

            assert result.model == "runner-default-model"
        finally:
            shutil.rmtree(worktree, ignore_errors=True)
            shutil.rmtree(output_dir, ignore_errors=True)


class TestSandboxCanBeRequired:
    """With sandbox.require set, an unconfined run is refused rather than
    merely announced — reported like any other refused launch so the worker
    surfaces it through the path it already has."""

    @pytest.mark.asyncio
    async def test_required_but_unavailable_refuses_to_launch(self, db_path, monkeypatch):
        monkeypatch.setenv("SISYPHUS_SANDBOX", "0")          # unconfined
        monkeypatch.setenv("SISYPHUS_SANDBOX_REQUIRE", "1")  # and not allowed to be

        worktree = pathlib.Path(os.environ["WORKSPACE_ROOT"]) / "worktree"
        worktree.mkdir(parents=True, exist_ok=True)
        _setup_run_shim(worktree)
        output_dir = _make_output_dir("run-require", "require")

        init_runners_table()
        add_runner("mock-require", "python3", extra_args=["-c", "print('ran')"])

        result = await launch_agent({"name": "T", "runner": "mock-require"},
                                    "anything", str(worktree), output_dir,
                                    timeout=5, step_id="require")

        assert result.status == "error"
        assert "sandbox is required" in (result.error or "")
        assert "SISYPHUS_SANDBOX=0" in (result.error or "")

    @pytest.mark.asyncio
    async def test_unconfined_still_runs_when_told_twice(self, db_path, monkeypatch):
        """Running unconfined on a backend host now takes BOTH switches: the
        sandbox off AND the requirement waived. One alone refuses (the test
        above); two is a stated operator decision and the run proceeds."""
        monkeypatch.setenv("SISYPHUS_SANDBOX", "0")
        monkeypatch.setenv("SISYPHUS_SANDBOX_REQUIRE", "0")

        worktree = pathlib.Path(os.environ["WORKSPACE_ROOT"]) / "worktree"
        worktree.mkdir(parents=True, exist_ok=True)
        _setup_run_shim(worktree)
        output_dir = _make_output_dir("run-norequire", "norequire")

        init_runners_table()
        add_runner("mock-norequire", "python3", extra_args=["-c", "print('ran')"])

        result = await launch_agent({"name": "T", "runner": "mock-norequire"},
                                    "anything", str(worktree), output_dir,
                                    timeout=5, step_id="norequire")

        assert result.status != "error", result.error
