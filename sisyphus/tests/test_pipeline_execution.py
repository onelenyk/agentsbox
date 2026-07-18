import asyncio
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest
import yaml

from core.pipeline import run_pipeline
from core.runners import init_runners_table, add_runner
from core.state import get_card, get_steps
from core.launcher import AgentResult

HERE = Path(__file__).parent
MOCK_AGENTS_DIR = HERE / "mock_agents"


# ── Helpers ────────────────────────────────────────────────────────────


def _write_pipeline_yaml(tmp_path, name, steps):
    pipeline = {"name": name, "description": "test pipeline", "steps": steps}
    path = tmp_path / "test_pipeline.yaml"
    path.write_text(yaml.dump(pipeline))
    return str(path)


def _mock_load_agent(agent_name):
    agent_map = {
        "pm-agent": {
            "name": "PM Agent",
            "runner": "mock-pm",
            "opencode_config": {"timeout": 5},
        },
        "engineer-agent": {
            "name": "Engineer Agent",
            "runner": "mock-engineer",
            "opencode_config": {"timeout": 5},
        },
        "reviewer-agent": {
            "name": "Reviewer Agent",
            "runner": "mock-reviewer",
            "opencode_config": {"timeout": 5},
        },
    }
    return agent_map.get(
        agent_name,
        {
            "name": agent_name,
            "runner": "mock-default",
            "opencode_config": {"timeout": 5},
        },
    )


def _agent_result(status="completed", summary="Done", verdict=None,
                  parsed_result=None):
    if parsed_result is None:
        parsed_result = {"status": status, "summary": summary}
        if verdict:
            parsed_result["verdict"] = verdict
    return AgentResult(
        status=status,
        summary=summary,
        verdict=verdict,
        parsed_result=parsed_result,
    )


@pytest.fixture(autouse=True)
def _setup_runners(db_path):
    init_runners_table()
    add_runner(
        "mock-engineer", "python3",
        extra_args=[str(MOCK_AGENTS_DIR / "mock_engineer.py")],
    )
    add_runner(
        "mock-pm", "python3",
        extra_args=[str(MOCK_AGENTS_DIR / "mock_pm.py")],
    )
    add_runner(
        "mock-reviewer", "python3",
        extra_args=[str(MOCK_AGENTS_DIR / "mock_reviewer_approved.py")],
    )


# ── Common patches helper ──────────────────────────────────────────────


def _enter_standard_patches(mock_launch=None):
    stack = ExitStack()
    stack.enter_context(
        patch("core.pipeline.load_agent", side_effect=_mock_load_agent))
    stack.enter_context(
        patch("core.pipeline._execute_setup_commands", new_callable=AsyncMock))
    stack.enter_context(
        patch("core.pipeline.validate_pipeline", return_value=[]))
    stack.enter_context(
        patch("core.templates.render_prompt", return_value="mock prompt"))
    stack.enter_context(
        patch("core.pipeline.save_resource", return_value="/fake/path"))
    stack.enter_context(
        patch("core.resources.save_agent_log", return_value="/fake/log"))
    if mock_launch is None:
        mock_launch = AsyncMock()
    stack.enter_context(
        patch("core.launcher.launch_agent", new=mock_launch))
    return stack, mock_launch


# ── Tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_single_step_pipeline(db_path, card, tmp_path):
    pipeline_path = _write_pipeline_yaml(tmp_path, "single-step", [
        {"id": "engineer", "agent": "engineer-agent"},
    ])

    mock_launch = AsyncMock()
    mock_launch.side_effect = [
        _agent_result(summary="Engineer done"),
    ]

    stack, _ = _enter_standard_patches(mock_launch)
    with stack:
        result = await run_pipeline(pipeline_path, card)

    assert result["status"] == "completed"

    updated_card = get_card(card["id"])
    assert updated_card["status"] == "completed"

    steps = get_steps(result["run_id"])
    assert len(steps) == 1
    assert steps[0]["status"] == "completed"
    assert steps[0]["result_json"] is not None


@pytest.mark.asyncio
async def test_multi_step_linear_pipeline(db_path, card, tmp_path):
    pipeline_path = _write_pipeline_yaml(tmp_path, "multi-step", [
        {"id": "pm", "agent": "pm-agent"},
        {"id": "engineer", "agent": "engineer-agent", "depends_on": ["pm"]},
        {"id": "reviewer", "agent": "reviewer-agent",
         "depends_on": ["engineer"]},
    ])

    mock_launch = AsyncMock()
    mock_launch.side_effect = [
        _agent_result(summary="PM done"),
        _agent_result(summary="Engineer done"),
        _agent_result(summary="Reviewer approved", verdict="approved"),
    ]

    stack, _ = _enter_standard_patches(mock_launch)
    with stack:
        result = await run_pipeline(pipeline_path, card)

    assert result["status"] == "completed"

    updated_card = get_card(card["id"])
    assert updated_card["status"] == "completed"

    steps = get_steps(result["run_id"])
    assert len(steps) == 3
    for step in steps:
        assert step["status"] == "completed"


@pytest.mark.asyncio
async def test_reviewer_retry_loop(db_path, card, tmp_path):
    pipeline_path = _write_pipeline_yaml(tmp_path, "review-retry", [
        {"id": "engineer", "agent": "engineer-agent"},
        {"id": "reviewer", "agent": "reviewer-agent",
         "depends_on": ["engineer"]},
    ])

    mock_launch = AsyncMock()
    mock_launch.side_effect = [
        _agent_result(summary="Engineer done"),
        _agent_result(verdict="changes_requested", summary="Changes needed"),
        _agent_result(summary="Engineer fixed"),
        _agent_result(verdict="approved", summary="Approved"),
    ]

    stack, _ = _enter_standard_patches(mock_launch)
    with stack:
        result = await run_pipeline(pipeline_path, card)

    assert result["status"] == "completed"

    updated_card = get_card(card["id"])
    assert updated_card["status"] == "completed"

    steps = get_steps(result["run_id"])
    assert len(steps) >= 3
    completed = [s for s in steps if s["status"] == "completed"]
    assert len(completed) >= 3


@pytest.mark.asyncio
async def test_max_review_loops_exceeded(db_path, card, tmp_path):
    pipeline_path = _write_pipeline_yaml(tmp_path, "max-loops", [
        {"id": "engineer", "agent": "engineer-agent"},
        {"id": "reviewer", "agent": "reviewer-agent",
         "depends_on": ["engineer"]},
    ])

    mock_launch = AsyncMock()

    side_effects = [
        _agent_result(summary="Engineer done"),
    ]
    for _ in range(3):
        side_effects.append(
            _agent_result(verdict="changes_requested",
                          summary="Changes needed"))
        side_effects.append(_agent_result(summary="Engineer fixed"))
    side_effects.append(
        _agent_result(verdict="changes_requested", summary="Changes needed"))

    mock_launch.side_effect = side_effects

    with patch("core.pipeline.load_agent", side_effect=_mock_load_agent), \
         patch("core.pipeline._execute_setup_commands",
               new_callable=AsyncMock), \
         patch("core.pipeline.validate_pipeline", return_value=[]), \
         patch("core.templates.render_prompt", return_value="mock prompt"), \
         patch("core.pipeline.save_resource", return_value="/fake/path"), \
         patch("core.pipeline.update_step_status"), \
         patch("core.launcher.launch_agent", new=mock_launch):
        result = await run_pipeline(pipeline_path, card)

    assert result["status"] == "failed"
    assert "max_review_loops_exceeded" in result.get("error", "")

    updated_card = get_card(card["id"])
    assert updated_card["status"] == "failed"


@pytest.mark.asyncio
async def test_cancel_during_pipeline(db_path, card, tmp_path):
    pipeline_path = _write_pipeline_yaml(tmp_path, "cancel-test", [
        {"id": "engineer", "agent": "engineer-agent"},
    ])

    mock_launch = AsyncMock()

    async def _cancel_sensing_launch(
        agent_def, prompt, worktree_path, output_dir, timeout,
        cancel_event=None, runner_name="", step_id="",
    ):
        await asyncio.sleep(0.1)
        if cancel_event and cancel_event.is_set():
            return AgentResult(status="failed", error="cancelled",
                               summary="Cancelled")
        return _agent_result(summary="Done")

    mock_launch.side_effect = _cancel_sensing_launch

    cancel_event = asyncio.Event()

    async def _set_cancel():
        await asyncio.sleep(0.05)
        cancel_event.set()

    with patch("core.pipeline.load_agent", side_effect=_mock_load_agent), \
         patch("core.pipeline._execute_setup_commands",
               new_callable=AsyncMock), \
         patch("core.pipeline.validate_pipeline", return_value=[]), \
         patch("core.launcher.launch_agent", new=mock_launch), \
         patch("core.templates.render_prompt", return_value="mock prompt"):
        cancel_task = asyncio.create_task(_set_cancel())
        result = await run_pipeline(pipeline_path, card,
                                    cancel_event=cancel_event)
        await cancel_task

    assert result["status"] == "failed"
    assert "cancelled" in result.get("error", "")


@pytest.mark.asyncio
async def test_pipeline_validation_failure(db_path, card, tmp_path):
    pipeline = {"description": "no name field",
                "steps": [{"id": "s1", "agent": "engineer-agent"}]}
    pipeline_path = tmp_path / "test_invalid.yaml"
    pipeline_path.write_text(yaml.dump(pipeline))

    with patch("core.pipeline_validator.load_agent",
               side_effect=_mock_load_agent), \
         patch("core.pipeline_validator.list_agents",
               return_value=["pm-agent", "engineer-agent",
                             "reviewer-agent"]), \
         patch("core.pipeline._execute_setup_commands",
               new_callable=AsyncMock):
        result = await run_pipeline(str(pipeline_path), card)

    assert result["status"] == "failed"
    assert "validation failed" in result.get("error", "").lower()

    updated_card = get_card(card["id"])
    assert updated_card["status"] == "failed"


@pytest.mark.asyncio
async def test_empty_steps_pipeline(db_path, card, tmp_path):
    pipeline_path = _write_pipeline_yaml(tmp_path, "empty-steps", [])

    with patch("core.pipeline.validate_pipeline", return_value=[]):
        result = await run_pipeline(pipeline_path, card)

    assert result["status"] == "completed"

    updated_card = get_card(card["id"])
    assert updated_card["status"] == "completed"


@pytest.mark.asyncio
async def test_no_waves_broken_deps_pipeline(db_path, card, tmp_path):
    pipeline_path = _write_pipeline_yaml(tmp_path, "broken-deps", [
        {"id": "s1", "agent": "engineer-agent", "depends_on": ["s2"]},
        {"id": "s2", "agent": "engineer-agent", "depends_on": ["s1"]},
    ])

    with patch("core.pipeline.load_agent", side_effect=_mock_load_agent), \
         patch("core.pipeline._execute_setup_commands",
               new_callable=AsyncMock), \
         patch("core.pipeline.validate_pipeline", return_value=[]), \
         patch("core.templates.render_prompt", return_value="mock prompt"), \
         patch("core.pipeline.save_resource", return_value="/fake/path"), \
         patch("core.pipeline._topological_sort", return_value=[]):
        result = await run_pipeline(pipeline_path, card)

    assert result["status"] == "failed"
    assert "no executable steps" in result.get("error", "")

    updated_card = get_card(card["id"])
    assert updated_card["status"] == "failed"
