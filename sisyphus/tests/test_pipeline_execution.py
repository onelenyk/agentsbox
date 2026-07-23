import asyncio
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

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
async def test_fsm_race_preserves_real_status(db_path, card, tmp_path):
    """Simulates the orphan-recovery race: a concurrent recovery pass flips the
    step to 'error' behind the pipeline's back while the agent is still
    running. When the agent later reports a real 'timeout', the FSM-repair
    branch (core/pipeline.py) must re-apply that actual outcome — not
    fabricate 'completed' — so the step's stored status matches reality."""
    from core.state import update_step_status

    pipeline_path = _write_pipeline_yaml(tmp_path, "fsm-race", [
        {"id": "engineer", "agent": "engineer-agent"},
    ])

    async def _racing_launch(*args, **kwargs):
        step_id = kwargs["step_id"]
        # Simulate a concurrent recover_orphaned_steps() call corrupting the
        # step (bypassing the FSM directly, just like recovery.py does).
        update_step_status(step_id, "error", error="orchestrator_crash")
        return _agent_result(
            status="timeout", summary="",
            parsed_result=None,
        )

    stack, _ = _enter_standard_patches(_racing_launch)
    with stack:
        result = await run_pipeline(pipeline_path, card)

    assert result["status"] == "failed"
    assert "timeout" in result["error"]

    steps = get_steps(result["run_id"])
    assert len(steps) == 1
    assert steps[0]["status"] == "timeout"


@pytest.mark.asyncio
async def test_agent_comments_become_activities(db_path, card, tmp_path):
    """A step whose agent posts comments (events.jsonl) + files gets those
    comments on the card timeline and its files registered as resources —
    and no summary-fallback activity is added."""
    import json as _json
    from pathlib import Path as _P
    from core.state import get_activities_for_card, get_resources_for_run

    pipeline_path = _write_pipeline_yaml(tmp_path, "commenting", [
        {"id": "engineer", "agent": "engineer-agent"},
    ])

    async def _commenting_launch(agent_def, prompt, worktree_path, output_dir, *a, **kw):
        out = _P(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "notes.md").write_text("findings")
        with open(out / "events.jsonl", "a") as f:
            f.write(_json.dumps({"ts": "2026-07-22T00:00:00+00:00", "type": "comment",
                                 "text": "found the bug", "resources": ["notes.md"]}) + "\n")
            f.write(_json.dumps({"ts": "2026-07-22T00:00:01+00:00", "type": "comment",
                                 "text": "fixed it", "resources": []}) + "\n")
        (out / "result.json").write_text(_json.dumps({"status": "completed", "summary": "done"}))
        return _agent_result(summary="done")

    stack, _ = _enter_standard_patches(_commenting_launch)
    with stack:
        result = await run_pipeline(pipeline_path, card)

    assert result["status"] == "completed"

    activities = get_activities_for_card(card["id"])
    assert [a["text"] for a in activities] == ["found the bug", "fixed it"]
    assert activities[0]["resources"] == ["notes.md"]
    assert all(a["author_type"] == "agent" for a in activities)

    filenames = {r["filename"] for r in get_resources_for_run(result["run_id"])}
    assert "notes.md" in filenames
    assert "result.json" in filenames
    assert "events.jsonl" not in filenames


@pytest.mark.asyncio
async def test_no_comments_falls_back_to_summary_activity(db_path, card, tmp_path):
    from core.state import get_activities_for_card

    pipeline_path = _write_pipeline_yaml(tmp_path, "no-comments", [
        {"id": "engineer", "agent": "engineer-agent"},
    ])

    mock_launch = AsyncMock()
    mock_launch.side_effect = [_agent_result(summary="Engineer done")]

    stack, _ = _enter_standard_patches(mock_launch)
    with stack:
        result = await run_pipeline(pipeline_path, card)

    assert result["status"] == "completed"

    activities = get_activities_for_card(card["id"])
    assert len(activities) == 1
    assert activities[0]["text"] == "Engineer done"
    assert activities[0]["author_type"] == "agent"


@pytest.mark.asyncio
async def test_no_comments_falls_back_on_timed_out_step(db_path, card, tmp_path):
    """Regression: a step that times out with zero comments must STILL get a
    fallback activity (from result.error), so a failed step's timeline is never
    empty. Guards against the class of bug seen when a drifted server ran an
    older _run_step that lacked the fallback and produced 0 activities."""
    from core.state import get_activities_for_card

    pipeline_path = _write_pipeline_yaml(tmp_path, "timeout-fallback", [
        {"id": "engineer", "agent": "engineer-agent"},
    ])

    mock_launch = AsyncMock()
    mock_launch.side_effect = [
        AgentResult(
            status="timeout", summary="",
            error="Agent exceeded 300s idle timeout — no output",
        ),
    ]

    stack, _ = _enter_standard_patches(mock_launch)
    with stack:
        result = await run_pipeline(pipeline_path, card)

    assert result["status"] == "failed"

    activities = get_activities_for_card(card["id"])
    assert len(activities) == 1
    assert "idle timeout" in activities[0]["text"]
    assert activities[0]["author_type"] == "agent"


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
        idle_timeout=None, cancel_event=None, runner_name="", step_id="",
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


# ── Worktree tests ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_shared_worktree_creates_one_worktree(db_path, card, tmp_path):
    shared_pipeline = {
        "name": "shared-wt",
        "description": "test shared worktree",
        "shared_worktree": True,
        "branch": "main",
        "steps": [
            {"id": "s1", "agent": "engineer-agent"},
            {"id": "s2", "agent": "engineer-agent"},
        ],
    }
    pipeline_path = tmp_path / "shared.yaml"
    pipeline_path.write_text(yaml.dump(shared_pipeline))

    mock_create_wt = MagicMock(return_value="/tmp/fake_shared_wt")
    mock_remove_wt = MagicMock()
    mock_init_clone = MagicMock()

    mock_launch = AsyncMock()
    mock_launch.side_effect = [
        _agent_result(summary="Step 1 done"),
        _agent_result(summary="Step 2 done"),
    ]

    with patch("core.pipeline.init_bare_clone", new=mock_init_clone), \
         patch("core.pipeline.create_worktree", new=mock_create_wt), \
         patch("core.pipeline.remove_worktree", new=mock_remove_wt), \
         patch("core.pipeline.load_agent", side_effect=_mock_load_agent), \
         patch("core.pipeline._execute_setup_commands", new_callable=AsyncMock), \
         patch("core.pipeline.validate_pipeline", return_value=[]), \
         patch("core.templates.render_prompt", return_value="mock prompt"), \
         patch("core.pipeline.save_resource", return_value="/fake/path"), \
         patch("core.launcher.launch_agent", new=mock_launch):
        result = await run_pipeline(
            str(pipeline_path), card,
            gitlab_repo_url="https://github.com/test/repo.git",
        )

    assert result["status"] == "completed"

    mock_create_wt.assert_called_once()
    assert mock_create_wt.call_args[0][2] == "_shared"

    mock_remove_wt.assert_called_once()
    assert mock_remove_wt.call_args[0][2] == "_shared"


@pytest.mark.asyncio
async def test_per_step_worktree_default_mode(db_path, card, tmp_path):
    pipeline_path = _write_pipeline_yaml(tmp_path, "per-step-wt", [
        {"id": "s1", "agent": "engineer-agent"},
        {"id": "s2", "agent": "engineer-agent"},
    ])

    mock_create_wt = MagicMock()
    mock_remove_wt = MagicMock()
    mock_init_clone = MagicMock()

    mock_launch = AsyncMock()
    mock_launch.side_effect = [
        _agent_result(summary="Step 1 done"),
        _agent_result(summary="Step 2 done"),
    ]

    fake_repos_dir = tmp_path / "repos"
    wt_dir = fake_repos_dir / "worktrees"
    wt_dir.mkdir(parents=True, exist_ok=True)

    def _side_effect_create_wt(bare_path, run_id, step_id, branch="main"):
        (wt_dir / f"{run_id}_{step_id}").mkdir(parents=True, exist_ok=True)
        return str(wt_dir / f"{run_id}_{step_id}")

    mock_create_wt.side_effect = _side_effect_create_wt

    with patch("core.pipeline.init_bare_clone", new=mock_init_clone), \
         patch("core.pipeline.create_worktree", new=mock_create_wt), \
         patch("core.pipeline.remove_worktree", new=mock_remove_wt), \
         patch("core.pipeline.load_agent", side_effect=_mock_load_agent), \
         patch("core.pipeline._execute_setup_commands", new_callable=AsyncMock), \
         patch("core.pipeline.validate_pipeline", return_value=[]), \
         patch("core.templates.render_prompt", return_value="mock prompt"), \
         patch("core.pipeline.save_resource", return_value="/fake/path"), \
         patch("core.launcher.launch_agent", new=mock_launch), \
         patch("core.worktree.REPOS_DIR", new=fake_repos_dir):
        result = await run_pipeline(
            pipeline_path, card,
            gitlab_repo_url="https://github.com/test/repo.git",
        )

    assert result["status"] == "completed"
    assert mock_create_wt.call_count == 2
    assert mock_remove_wt.call_count == 2


@pytest.mark.asyncio
async def test_worktree_cleanup_on_failure(db_path, card, tmp_path):
    pipeline_path = _write_pipeline_yaml(tmp_path, "fail-cleanup", [
        {"id": "s1", "agent": "engineer-agent"},
        {"id": "s2", "agent": "engineer-agent"},
    ])

    mock_create_wt = MagicMock()
    mock_remove_wt = MagicMock()
    mock_init_clone = MagicMock()

    mock_launch = AsyncMock()
    mock_launch.side_effect = [
        _agent_result(summary="Step 1 done"),
        _agent_result(status="failed", summary="Step 2 failed"),
    ]

    fake_repos_dir = tmp_path / "repos"
    wt_dir = fake_repos_dir / "worktrees"
    wt_dir.mkdir(parents=True, exist_ok=True)

    def _side_effect_create_wt(bare_path, run_id, step_id, branch="main"):
        (wt_dir / f"{run_id}_{step_id}").mkdir(parents=True, exist_ok=True)
        return str(wt_dir / f"{run_id}_{step_id}")

    mock_create_wt.side_effect = _side_effect_create_wt

    with patch("core.pipeline.init_bare_clone", new=mock_init_clone), \
         patch("core.pipeline.create_worktree", new=mock_create_wt), \
         patch("core.pipeline.remove_worktree", new=mock_remove_wt), \
         patch("core.pipeline.load_agent", side_effect=_mock_load_agent), \
         patch("core.pipeline._execute_setup_commands", new_callable=AsyncMock), \
         patch("core.pipeline.validate_pipeline", return_value=[]), \
         patch("core.templates.render_prompt", return_value="mock prompt"), \
         patch("core.pipeline.save_resource", return_value="/fake/path"), \
         patch("core.launcher.launch_agent", new=mock_launch), \
         patch("core.worktree.REPOS_DIR", new=fake_repos_dir):
        result = await run_pipeline(
            pipeline_path, card,
            gitlab_repo_url="https://github.com/test/repo.git",
        )

    assert result["status"] == "failed"

    assert mock_create_wt.call_count == 2
    assert mock_remove_wt.call_count == 2


@pytest.mark.asyncio
async def test_shared_worktree_cleanup_on_failure(db_path, card, tmp_path):
    shared_pipeline = {
        "name": "shared-fail",
        "description": "test shared worktree failure cleanup",
        "shared_worktree": True,
        "branch": "main",
        "steps": [
            {"id": "s1", "agent": "engineer-agent"},
        ],
    }
    pipeline_path = tmp_path / "shared_fail.yaml"
    pipeline_path.write_text(yaml.dump(shared_pipeline))

    mock_create_wt = MagicMock(return_value="/tmp/fake_shared_wt")
    mock_remove_wt = MagicMock()
    mock_init_clone = MagicMock()

    mock_launch = AsyncMock()
    mock_launch.side_effect = [
        _agent_result(status="failed", summary="Boom"),
    ]

    with patch("core.pipeline.init_bare_clone", new=mock_init_clone), \
         patch("core.pipeline.create_worktree", new=mock_create_wt), \
         patch("core.pipeline.remove_worktree", new=mock_remove_wt), \
         patch("core.pipeline.load_agent", side_effect=_mock_load_agent), \
         patch("core.pipeline._execute_setup_commands", new_callable=AsyncMock), \
         patch("core.pipeline.validate_pipeline", return_value=[]), \
         patch("core.templates.render_prompt", return_value="mock prompt"), \
         patch("core.pipeline.save_resource", return_value="/fake/path"), \
         patch("core.launcher.launch_agent", new=mock_launch):
        result = await run_pipeline(
            str(pipeline_path), card,
            gitlab_repo_url="https://github.com/test/repo.git",
        )

    assert result["status"] == "failed"

    mock_create_wt.assert_called_once()
    assert mock_create_wt.call_args[0][2] == "_shared"

    mock_remove_wt.assert_called_once()
    assert mock_remove_wt.call_args[0][2] == "_shared"
