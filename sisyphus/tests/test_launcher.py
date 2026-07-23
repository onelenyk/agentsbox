"""Integration tests for launch_agent using 7 mock agent scripts."""
import asyncio
import json
import os
import pathlib
import pytest

from core.launcher import launch_agent, AgentResult
from core.runners import init_runners_table, add_runner
from core.state import create_step

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


def _make_output_dir(run, step_id):
    """Build an output_dir that _derive_run_id can parse back to run['id'].

    Returns (output_dir, step) where *step* is the DB step record whose
    id matches what launch_agent will use for resource registration.
    """
    workspace_root = os.environ["WORKSPACE_ROOT"]
    d = pathlib.Path(workspace_root) / "runs" / run["id"] / "resources" / step_id
    d.mkdir(parents=True, exist_ok=True)
    # Create a step with the exact id matching the directory name so the
    # resources.step_id FK constraint is satisfied.
    step = create_step(run["id"], step_id, 0)
    # Override the auto-generated id to match the directory name.
    from core.db import get_conn
    conn = get_conn()
    conn.execute("UPDATE steps SET id = ? WHERE id = ?", (step_id, step["id"]))
    conn.commit()
    conn.close()
    step["id"] = step_id
    return str(d), step


# =========================================================================
# Tests
# =========================================================================


class TestMockEngineer:

    @pytest.mark.asyncio
    async def test_engineer_completed(self, db_path, run):
        worktree = pathlib.Path(os.environ["WORKSPACE_ROOT"]) / "worktree"
        worktree.mkdir(parents=True, exist_ok=True)
        _setup_run_shim(worktree)

        output_dir, step = _make_output_dir(run, "engineer_ok")

        init_runners_table()
        add_runner(
            "mock-engineer",
            "python3",
            extra_args=[
                str(MOCK_DIR / "mock_engineer.py"),
                "--output-dir", output_dir,
            ],
        )

        agent_def = {"name": "Test Engineer", "runner": "mock-engineer"}
        result = await launch_agent(
            agent_def, "analyze code", str(worktree), output_dir,
            timeout=30, step_id=step["id"],
        )

        assert result.status == "completed"
        assert "Mock engineer analysis" in result.summary
        assert result.risk == "low"
        assert result.parsed_result is not None
        assert result.parsed_result["status"] == "completed"
        assert result.parsed_result["risk"] == "low"

    @pytest.mark.asyncio
    async def test_engineer_failed(self, db_path, run):
        worktree = pathlib.Path(os.environ["WORKSPACE_ROOT"]) / "worktree"
        worktree.mkdir(parents=True, exist_ok=True)
        _setup_run_shim(worktree)

        output_dir, step = _make_output_dir(run, "engineer_fail")

        init_runners_table()
        add_runner(
            "mock-engineer-fail",
            "python3",
            extra_args=[
                str(MOCK_DIR / "mock_engineer.py"),
                "--output-dir", output_dir,
                "--status", "failed",
            ],
        )

        agent_def = {"name": "Test Engineer Fail", "runner": "mock-engineer-fail"}
        result = await launch_agent(
            agent_def, "analyze code", str(worktree), output_dir,
            timeout=30, step_id=step["id"],
        )

        assert result.status == "failed"
        assert result.parsed_result is not None
        assert result.parsed_result["status"] == "failed"


class TestMockReviewerApproved:

    @pytest.mark.asyncio
    async def test_reviewer_approved(self, db_path, run):
        worktree = pathlib.Path(os.environ["WORKSPACE_ROOT"]) / "worktree"
        worktree.mkdir(parents=True, exist_ok=True)
        _setup_run_shim(worktree)

        output_dir, step = _make_output_dir(run, "reviewer_ok")

        init_runners_table()
        add_runner(
            "mock-reviewer-ok",
            "python3",
            extra_args=[
                str(MOCK_DIR / "mock_reviewer_approved.py"),
                "--output-dir", output_dir,
            ],
        )

        agent_def = {"name": "Test Reviewer", "runner": "mock-reviewer-ok"}
        result = await launch_agent(
            agent_def, "review plan", str(worktree), output_dir,
            timeout=30, step_id=step["id"],
        )

        assert result.verdict == "approved"
        assert "plan approved" in result.summary.lower()
        assert result.status == "completed"


class TestMockReviewerChanges:

    @pytest.mark.asyncio
    async def test_reviewer_changes_requested(self, db_path, run):
        worktree = pathlib.Path(os.environ["WORKSPACE_ROOT"]) / "worktree"
        worktree.mkdir(parents=True, exist_ok=True)
        _setup_run_shim(worktree)

        output_dir, step = _make_output_dir(run, "reviewer_changes")

        init_runners_table()
        add_runner(
            "mock-reviewer-changes",
            "python3",
            extra_args=[
                str(MOCK_DIR / "mock_reviewer_changes.py"),
                "--output-dir", output_dir,
            ],
        )

        agent_def = {"name": "Test Reviewer", "runner": "mock-reviewer-changes"}
        result = await launch_agent(
            agent_def, "review plan", str(worktree), output_dir,
            timeout=30, step_id=step["id"],
        )

        assert result.verdict == "changes_requested"
        assert result.status == "completed"


class TestMockPM:

    @pytest.mark.asyncio
    async def test_pm_basic_result(self, db_path, run):
        worktree = pathlib.Path(os.environ["WORKSPACE_ROOT"]) / "worktree"
        worktree.mkdir(parents=True, exist_ok=True)
        _setup_run_shim(worktree)

        output_dir, step = _make_output_dir(run, "pm")

        init_runners_table()
        add_runner(
            "mock-pm",
            "python3",
            extra_args=[
                str(MOCK_DIR / "mock_pm.py"),
                "--output-dir", output_dir,
            ],
        )

        agent_def = {"name": "Test PM", "runner": "mock-pm"}
        result = await launch_agent(
            agent_def, "analyze jira", str(worktree), output_dir,
            timeout=30, step_id=step["id"],
        )

        assert result.status == "completed"
        assert result.parsed_result is not None


class TestMockAgentCrash:

    @pytest.mark.asyncio
    async def test_agent_crash(self, db_path, run):
        worktree = pathlib.Path(os.environ["WORKSPACE_ROOT"]) / "worktree"
        worktree.mkdir(parents=True, exist_ok=True)
        _setup_run_shim(worktree)

        output_dir, step = _make_output_dir(run, "crash")

        init_runners_table()
        add_runner(
            "mock-crash",
            "python3",
            extra_args=[
                str(MOCK_DIR / "mock_agent_crash.py"),
            ],
        )

        agent_def = {"name": "Test Crash", "runner": "mock-crash"}
        result = await launch_agent(
            agent_def, "anything", str(worktree), output_dir,
            timeout=30, step_id=step["id"],
        )

        assert result.status in ("failed", "error")
        assert result.error is not None


class TestMockAgentInvalidJson:

    @pytest.mark.asyncio
    async def test_agent_invalid_json(self, db_path, run):
        worktree = pathlib.Path(os.environ["WORKSPACE_ROOT"]) / "worktree"
        worktree.mkdir(parents=True, exist_ok=True)
        _setup_run_shim(worktree)

        output_dir, step = _make_output_dir(run, "invalid_json")

        init_runners_table()
        add_runner(
            "mock-invalid-json",
            "python3",
            extra_args=[
                str(MOCK_DIR / "mock_agent_invalid_json.py"),
                "--output-dir", output_dir,
            ],
        )

        agent_def = {"name": "Test Invalid JSON", "runner": "mock-invalid-json"}
        result = await launch_agent(
            agent_def, "anything", str(worktree), output_dir,
            timeout=30, step_id=step["id"],
        )

        assert result.status == "failed"
        assert result.error is not None
        err_lower = result.error.lower()
        assert "schema_validation_failed" in err_lower or "invalid json" in err_lower


class TestMockAgentTimeout:

    @pytest.mark.asyncio
    async def test_agent_timeout(self, db_path, run):
        worktree = pathlib.Path(os.environ["WORKSPACE_ROOT"]) / "worktree"
        worktree.mkdir(parents=True, exist_ok=True)
        _setup_run_shim(worktree)

        output_dir, step = _make_output_dir(run, "timeout")

        init_runners_table()
        add_runner(
            "mock-timeout",
            "python3",
            extra_args=[
                str(MOCK_DIR / "mock_agent_timeout.py"),
                "--output-dir", output_dir,
                "--sleep", "2",
            ],
        )

        agent_def = {"name": "Test Timeout", "runner": "mock-timeout"}
        result = await launch_agent(
            agent_def, "anything", str(worktree), output_dir,
            timeout=1, step_id=step["id"],
        )

        assert result.status == "timeout"


class TestIdleTimeout:

    @pytest.mark.asyncio
    async def test_idle_timeout_fires_on_silent_hang(self, db_path, run):
        """No output at all for longer than idle_timeout → killed, even under total timeout."""
        worktree = pathlib.Path(os.environ["WORKSPACE_ROOT"]) / "worktree"
        worktree.mkdir(parents=True, exist_ok=True)
        _setup_run_shim(worktree)

        output_dir, step = _make_output_dir(run, "idle-hang")

        init_runners_table()
        add_runner(
            "mock-idle-hang",
            "python3",
            extra_args=[
                str(MOCK_DIR / "mock_agent_timeout.py"),
                "--output-dir", output_dir,
                "--sleep", "5",
            ],
        )

        agent_def = {"name": "Test Idle Hang", "runner": "mock-idle-hang"}
        result = await launch_agent(
            agent_def, "anything", str(worktree), output_dir,
            timeout=30, idle_timeout=1, step_id=step["id"],
        )

        assert result.status == "timeout"
        assert "idle timeout" in result.error

    @pytest.mark.asyncio
    async def test_idle_timeout_does_not_fire_with_active_output(self, db_path, run):
        """Periodic output resets the idle clock, so a slow-but-alive agent isn't killed."""
        worktree = pathlib.Path(os.environ["WORKSPACE_ROOT"]) / "worktree"
        worktree.mkdir(parents=True, exist_ok=True)
        _setup_run_shim(worktree)

        output_dir, step = _make_output_dir(run, "idle-active")

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
            timeout=30, idle_timeout=1, step_id=step["id"],
        )

        assert result.status == "completed"


class TestNoResultJson:

    @pytest.mark.asyncio
    async def test_exit_zero_without_result_json_fails(self, db_path, run):
        """Fail-fast: statuses are never invented from the exit code."""
        worktree = pathlib.Path(os.environ["WORKSPACE_ROOT"]) / "worktree"
        worktree.mkdir(parents=True, exist_ok=True)
        _setup_run_shim(worktree)

        output_dir, step = _make_output_dir(run, "no-result")

        init_runners_table()
        add_runner(
            "mock-no-result",
            "python3",
            extra_args=[
                str(MOCK_DIR / "mock_agent_timeout.py"),
                "--output-dir", output_dir,
                "--sleep", "0",
            ],
        )

        agent_def = {"name": "Test No Result", "runner": "mock-no-result"}
        result = await launch_agent(
            agent_def, "anything", str(worktree), output_dir,
            timeout=30, step_id=step["id"],
        )

        assert result.status == "failed"
        assert "agent_no_result" in result.error
        # No fabricated result.json, and stdout is not saved as analysis.md
        assert not (pathlib.Path(output_dir) / "result.json").exists()
        assert not (pathlib.Path(output_dir) / "analysis.md").exists()
        # But the log was still captured (under core.resources' RUNS_DIR,
        # which is frozen at import time — not necessarily this test's env)
        import core.resources
        log = core.resources.RUNS_DIR / run["id"] / "logs" / f"{step['id']}.log"
        assert log.exists()


class TestCancelEvent:

    @pytest.mark.asyncio
    async def test_cancel_event(self, db_path, run):
        worktree = pathlib.Path(os.environ["WORKSPACE_ROOT"]) / "worktree"
        worktree.mkdir(parents=True, exist_ok=True)
        _setup_run_shim(worktree)

        output_dir, step = _make_output_dir(run, "cancel")

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
            timeout=30, cancel_event=cancel_event, step_id=step["id"],
        )

        assert result.status == "cancelled"


class TestBinaryNotFound:

    @pytest.mark.asyncio
    async def test_binary_not_found(self, db_path, run):
        worktree = pathlib.Path(os.environ["WORKSPACE_ROOT"]) / "worktree"
        worktree.mkdir(parents=True, exist_ok=True)

        output_dir, step = _make_output_dir(run, "bin")

        init_runners_table()
        add_runner(
            "mock-missing-bin",
            "nonexistent_binary_xyz",
            extra_args=[],
        )

        agent_def = {"name": "Test Missing Bin", "runner": "mock-missing-bin"}
        result = await launch_agent(
            agent_def, "anything", str(worktree), output_dir,
            timeout=30, step_id=step["id"],
        )

        assert result.status == "error"
        assert "not found" in result.error.lower() or "nonexistent" in result.error.lower()
