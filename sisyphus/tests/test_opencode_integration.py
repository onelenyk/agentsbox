"""
Real integration tests against the opencode AI runner.

Requires: opencode in $PATH, OPENROUTER_API_KEY set.
Skipped automatically when either is missing.
"""

import os
import shutil
from pathlib import Path

import pytest

from core.launcher import launch_agent
from core.runners import init_runners_table, get_default_runner
from core.schemas import BaseResult

pytestmark = pytest.mark.skipif(
    not shutil.which("opencode") or not os.environ.get("OPENROUTER_API_KEY"),
    reason="opencode binary or OPENROUTER_API_KEY not available",
)


@pytest.fixture
def runner(db_path):
    init_runners_table()
    default = get_default_runner()
    assert default is not None, "No default runner configured"
    return default


@pytest.fixture
def work_env(db_path, run):
    from core.state import create_step
    step = create_step(run["id"], "integration-test", 0)
    ws_root = os.environ.get("WORKSPACE_ROOT", "/tmp")
    output_dir = f"{ws_root}/runs/{run['id']}/resources/{step['id']}"
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    return {"output_dir": output_dir, "run_id": run["id"], "step_id": step["id"]}


class TestOpenCodeReal:

    @pytest.mark.asyncio
    async def test_opencode_runs_and_produces_output(self, runner, work_env):
        result = await launch_agent(
            agent_def={"name": "Smoke", "role": "test", "access": "read"},
            prompt="Reply with exactly: completed.",
            worktree_path="/tmp",
            output_dir=work_env["output_dir"],
            timeout=30,
            runner_name=runner["name"],
            step_id=work_env["step_id"],
        )

        assert result.status in ("completed", "failed"), f"Unexpected: {result.status}"
        assert result.duration_ms > 0
        assert result.parsed_result is not None
        BaseResult(**result.parsed_result)

    @pytest.mark.asyncio
    async def test_sisyphus_prompt_format_works(self, runner, work_env):
        prompt = (
            "You are a test agent.\n"
            "## Your Task\n"
            "Confirm test infrastructure works. Reply briefly.\n"
            "## Output\n"
            "Provide status=completed and a short summary."
        )

        result = await launch_agent(
            agent_def={"name": "PM", "role": "PM", "access": "read"},
            prompt=prompt,
            worktree_path="/tmp",
            output_dir=work_env["output_dir"],
            timeout=60,
            runner_name=runner["name"],
            step_id=work_env["step_id"],
        )

        assert result.status in ("completed", "failed"), f"Unexpected: {result.status}"

    @pytest.mark.asyncio
    async def test_timeout_kills_opencode(self, runner, work_env):
        result = await launch_agent(
            agent_def={"name": "Timeout", "role": "test", "access": "read"},
            prompt="List every file recursively, then search all for TODOs. Be thorough.",
            worktree_path="/tmp",
            output_dir=work_env["output_dir"],
            timeout=3,
            runner_name=runner["name"],
            step_id=work_env["step_id"],
        )

        assert result.status == "timeout", f"Expected timeout, got {result.status}"
