"""Pipeline engine — DAG walker with parallel execution and reviewer loop.

Combines topological sort (BFS waves), asyncio.gather for parallel step execution,
and a reviewer-retry loop that re-runs engineer steps when reviewer verdict is
"changes_requested" (up to MAX_REVIEW_LOOPS iterations before failing).
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional
from collections import defaultdict, deque

import yaml

from core.state import create_run, create_step, update_step_status, update_card_status, update_run_status
from core.fsm import transition_step
from core.resources import get_available_resources, init_run_dir, save_resource
from core.worktree import (
    create_worktree,
    init_bare_clone,
    get_repo_name_from_url,
    remove_worktree,
    set_access,
)
from core.pipeline_validator import validate_pipeline
from agents import load_agent

logger = logging.getLogger(__name__)

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"

# Fails on the 4th "changes_requested" (i.e. 3 retries allowed).
MAX_REVIEW_LOOPS = 3


async def run_pipeline(
    pipeline_yaml_path: str,
    card: dict,
    gitlab_repo_url: str = None,
    cancel_event: asyncio.Event = None,
) -> dict:
    """Run a complete pipeline for a card.

    Args:
        pipeline_yaml_path: Path to the pipeline YAML file.
        card: Card dict (must contain ``id``, ``description``, ``platforms``).
        gitlab_repo_url: Optional GitLab repo URL used to create bare clone
            and per-step worktrees.
        cancel_event: Optional ``asyncio.Event`` — set to cancel mid-flight.

    Returns:
        ``{"run_id": ..., "status": "completed"|"failed", "error": ...}``
    """
    errors = validate_pipeline(pipeline_yaml_path)
    if errors:
        update_card_status(card["id"], "failed")
        return {
            "status": "failed",
            "error": f"Pipeline validation failed: {'; '.join(errors)}",
        }

    pipeline = yaml.safe_load(Path(pipeline_yaml_path).read_text())

    run = create_run(card["id"])
    run_id = run["id"]
    update_card_status(card["id"], "running")
    init_run_dir(run_id)

    steps = pipeline.get("steps", [])
    if not steps:
        update_card_status(card["id"], "completed")
        update_run_status(run_id, "completed")
        return {"run_id": run_id, "status": "completed"}

    waves = _topological_sort(steps)
    if not waves:
        error_msg = (
            "Pipeline has no executable steps — "
            "all steps may depend on missing step ids"
        )
        update_card_status(card["id"], "failed")
        update_run_status(run_id, "failed", error_msg)
        return {
            "run_id": run_id,
            "status": "failed",
            "error": error_msg,
        }

    bare_path = None
    repo_name = None

    # Auto-resolve repo from platform if no URL provided
    if not gitlab_repo_url:
        from core.repos import get_repo_by_platform
        platform = card.get("platforms", ["android"])
        if isinstance(platform, str):
            platform = json.loads(platform)
        if isinstance(platform, list):
            platform = platform[0] if platform else "android"
        repo = get_repo_by_platform(platform)
        if repo:
            gitlab_repo_url = repo["source"]

    if gitlab_repo_url:
        repo_name = get_repo_name_from_url(gitlab_repo_url)
        workspace_root = os.environ.get("WORKSPACE_ROOT", "/workspace")
        bare_path = f"{workspace_root}/repos/{run_id}_{repo_name}"
        init_bare_clone(gitlab_repo_url, bare_path)

    loop_tracker: dict = {}

    for wave_num, wave_steps in enumerate(waves):
        if cancel_event and cancel_event.is_set():
            update_card_status(card["id"], "failed")
            update_run_status(run_id, "failed", "cancelled")
            return {"run_id": run_id, "status": "failed", "error": "cancelled"}

        logger.info(
            "Wave %d/%d  steps=%s",
            wave_num + 1,
            len(waves),
            [s["id"] for s in wave_steps],
        )

        step_orders = {s["id"]: _get_step_order(s["id"], steps) for s in wave_steps}

        min_order = min(step_orders.values())
        resources = (
            get_available_resources(run_id, min_order) if min_order >= 0 else []
        )

        for s in wave_steps:
            await _execute_setup_commands(
                s, run_id, step_orders[s["id"]], resources
            )

        review_retry_count = 0

        while True:
            if cancel_event and cancel_event.is_set():
                update_card_status(card["id"], "failed")
                update_run_status(run_id, "failed", "cancelled")
                return {"run_id": run_id, "status": "failed", "error": "cancelled"}

            step_records = {}
            for s in wave_steps:
                sid = s["id"]
                agent_def = load_agent(s.get("agent", "android-engineer"))
                step = create_step(run_id, agent_def["name"], step_orders[sid])
                transition_step(step["id"], "running")
                step_records[sid] = step

            if review_retry_count > 0:
                resources = (
                    get_available_resources(run_id, min_order)
                    if min_order >= 0
                    else []
                )

            tasks = [
                _run_step(
                    agent_def=load_agent(s.get("agent", "android-engineer")),
                    step_def=s,
                    step_record=step_records[s["id"]],
                    run_id=run_id,
                    card=card,
                    resources=resources,
                    bare_path=bare_path,
                    repo_name=repo_name,
                    timeout=_agent_timeout(s),
                    cancel_event=cancel_event,
                )
                for s in wave_steps
            ]

            results = await asyncio.gather(*tasks, return_exceptions=True)

            has_changes_requested = False
            changes_reviewers: list = []

            for i, result in enumerate(results):
                s = wave_steps[i]
                sid = s["id"]
                step = step_records[sid]

                if isinstance(result, Exception):
                    transition_step(
                        step["id"], "failed",
                        error=f"Agent crashed: {result}",
                    )
                    error_msg = f"Step '{sid}' crashed: {result}"
                    update_card_status(card["id"], "failed")
                    update_run_status(run_id, "failed", error_msg)
                    return {
                        "run_id": run_id,
                        "status": "failed",
                        "error": error_msg,
                    }

                result_json = (
                    json.dumps(result.parsed_result)
                    if result.parsed_result
                    else None
                )
                transition_step(
                    step["id"], result.status, result_json, result.error,
                )

                if result.status in ("failed", "timeout", "error"):
                    error_msg = (
                        f"Step '{sid}' {result.status}: "
                        f"{result.error or 'no error details'}"
                    )
                    update_card_status(card["id"], "failed")
                    update_run_status(run_id, "failed", error_msg)
                    return {
                        "run_id": run_id,
                        "status": "failed",
                        "error": error_msg,
                    }

                if result.verdict == "changes_requested":
                    has_changes_requested = True
                    changes_reviewers.append((i, sid))

            if has_changes_requested:
                review_retry_count += 1
                unique_engineers: set = set()

                for idx, reviewer_sid in changes_reviewers:
                    loop_tracker[reviewer_sid] = (
                        loop_tracker.get(reviewer_sid, 0) + 1
                    )

                    if loop_tracker[reviewer_sid] > MAX_REVIEW_LOOPS:
                        step = step_records[reviewer_sid]
                        update_step_status(
                            step["id"], "failed",
                            error="max_review_loops_exceeded",
                        )
                        error_msg = (
                            f"max_review_loops_exceeded: step "
                            f"'{reviewer_sid}' requested changes "
                            f"{MAX_REVIEW_LOOPS + 1} times "
                            f"(max {MAX_REVIEW_LOOPS})"
                        )
                        update_card_status(card["id"], "failed")
                        update_run_status(run_id, "failed", error_msg)
                        return {
                            "run_id": run_id,
                            "status": "failed",
                            "error": error_msg,
                        }

                    engineer_sid = _find_engineer_step(reviewer_sid, steps)
                    if not engineer_sid:
                        logger.warning(
                            "Reviewer '%s' has no engineer dependency "
                            "— skipping retry",
                            reviewer_sid,
                        )
                        continue
                    unique_engineers.add(engineer_sid)

                    reviewer_result = results[idx]
                    feedback_data = {
                        "reviewer": reviewer_sid,
                        "verdict": "changes_requested",
                        "summary": reviewer_result.summary,
                        "loop": loop_tracker[reviewer_sid],
                        "parsed_result": reviewer_result.parsed_result,
                    }
                    save_resource(
                        run_id,
                        engineer_sid,
                        "reviewer_feedback.json",
                        json.dumps(feedback_data, indent=2),
                        step_order=-1,  # visible to every re-run
                        resource_type="reviewer_feedback",
                    )

                for engineer_sid in sorted(unique_engineers):
                    logger.info(
                        "Re-running engineer '%s' (reviewer loop %d/%d)",
                        engineer_sid,
                        loop_tracker.get(
                            changes_reviewers[0][1], 0
                        ),
                        MAX_REVIEW_LOOPS,
                    )
                    err = await _rerun_engineer(
                        engineer_sid=engineer_sid,
                        steps=steps,
                        run_id=run_id,
                        card=card,
                        resources=resources,
                        bare_path=bare_path,
                        repo_name=repo_name,
                        cancel_event=cancel_event,
                    )
                    if err:
                        update_card_status(card["id"], "failed")
                        update_run_status(run_id, "failed", err)
                        return {"run_id": run_id, "status": "failed", "error": err}

                continue

            break

        if bare_path:
            for s in wave_steps:
                try:
                    remove_worktree(bare_path, run_id, s["id"])
                except Exception:
                    pass

    return {"run_id": run_id, "status": "completed"}


async def _run_step(
    agent_def: dict,
    step_def: dict,
    step_record: dict,
    run_id: str,
    card: dict,
    resources: list,
    bare_path: Optional[str],
    repo_name: Optional[str],
    timeout: int,
    cancel_event: asyncio.Event,
):
    """Execute a single pipeline step.

    Creates a worktree (when repo is provided), renders the prompt,
    launches the agent subprocess, and returns the ``AgentResult``.
    """
    from core.templates import render_prompt
    from core.launcher import launch_agent

    sid = step_def["id"]
    workspace_root = os.environ.get("WORKSPACE_ROOT", "/workspace")
    output_dir = f"{workspace_root}/runs/{run_id}/resources/{sid}"
    worktree_path = "/tmp"  # fallback when no repo

    if bare_path and repo_name:
        branch = step_def.get("branch", "Staging")
        worktree_path = create_worktree(bare_path, run_id, sid, branch)
        access = agent_def.get("access", "read")
        set_access(worktree_path, access)

    prompt = render_prompt(
        agent_def,
        card,
        {"id": sid, "run_id": run_id, "worktree_path": worktree_path},
        resources,
    )

    return await launch_agent(
        agent_def, prompt, worktree_path, output_dir, timeout, cancel_event,
        runner_name=agent_def.get("runner"),
        step_id=step_record["id"],
    )


async def _rerun_engineer(
    engineer_sid: str,
    steps: list,
    run_id: str,
    card: dict,
    resources: list,
    bare_path: Optional[str],
    repo_name: Optional[str],
    cancel_event: asyncio.Event,
) -> Optional[str]:
    """Re-run a single engineer step (called during reviewer retry loop).

    Returns an error string on failure, or ``None`` on success.
    """
    eng_step_def = _find_step_def(engineer_sid, steps)
    if not eng_step_def:
        return f"Engineer step '{engineer_sid}' not found in pipeline steps"

    agent_def = load_agent(eng_step_def.get("agent", "android-engineer"))
    timeout = _agent_timeout(eng_step_def)
    step_order = _get_step_order(engineer_sid, steps)

    # Include reviewer feedback resources (step_order = -1) in the snapshot.
    eng_resources = (
        get_available_resources(run_id, step_order) if step_order >= 0 else []
    )

    step_record = create_step(run_id, agent_def["name"], step_order)

    result = await _run_step(
        agent_def=agent_def,
        step_def=eng_step_def,
        step_record=step_record,
        run_id=run_id,
        card=card,
        resources=eng_resources,
        bare_path=bare_path,
        repo_name=repo_name,
        timeout=timeout,
        cancel_event=cancel_event,
    )

    if isinstance(result, Exception):
        update_step_status(step_record["id"], "failed", error=str(result))
        return f"Engineer re-run crashed: {result}"

    result_json = (
        json.dumps(result.parsed_result) if result.parsed_result else None
    )
    transition_step(step_record["id"], result.status, result_json, result.error)

    if result.status in ("failed", "timeout", "error"):
        update_step_status(
            step_record["id"], result.status, result_json, result.error,
        )
        return (
            f"Engineer re-run '{engineer_sid}' {result.status}: "
            f"{result.error or 'no error details'}"
        )

    return None


async def _execute_setup_commands(
    step_def: dict, run_id: str, step_order: int, resources: list,
) -> None:
    """Run each setup tool for a step before the agent launches."""
    sid = step_def["id"]
    for tool_cmd in step_def.get("setup", []):
        tool_name = tool_cmd.get("tool", "")
        if not tool_name:
            continue

        logger.info("Setup tool '%s' for step '%s'", tool_name, sid)
        tool_path = TOOLS_DIR / f"{tool_name}.py"
        if not tool_path.exists():
            logger.warning(
                "Setup tool '%s' not found at %s — skipping", tool_name, tool_path,
            )
            continue

        tool_args = tool_cmd.get("args", [])
        cmd = ["python3", str(tool_path)] + [str(a) for a in tool_args]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=60,
            )
        except asyncio.TimeoutError:
            logger.warning("Setup tool '%s' timed out after 60s", tool_name)
            continue
        except Exception as exc:
            logger.warning("Setup tool '%s' failed to launch: %s", tool_name, exc)
            continue

        output = stdout.decode("utf-8", errors="replace")
        if stderr:
            stderr_str = stderr.decode("utf-8", errors="replace")
            logger.debug("Setup tool '%s' stderr: %s", tool_name, stderr_str[:500])

        if proc.returncode != 0 and not output.strip():
            logger.warning(
                "Setup tool '%s' exited %d with no stdout", tool_name, proc.returncode,
            )
            continue

        save_resource(
            run_id, sid, f"{tool_name}_output.json", output,
            step_order=step_order, resource_type="tool_output",
        )


def _topological_sort(steps: list) -> list[list[dict]]:
    """Topological sort into execution waves (BFS levels).

    Each wave is a list of step-def dicts that can run in parallel.
    Steps with no ``depends_on`` go in wave 0; waves progress level by level.

    Returns an empty list if the graph cannot be sorted (should not happen
    after pipeline validation).
    """
    graph: dict = defaultdict(list)
    indegree: dict = {}
    step_map: dict = {}

    for s in steps:
        sid = s["id"]
        step_map[sid] = s
        indegree.setdefault(sid, 0)

        deps = s.get("depends_on", [])
        if isinstance(deps, str):
            deps = [deps]
        for dep in deps:
            if dep in step_map:  # only count valid deps
                graph[dep].append(sid)
                indegree[sid] = indegree.get(sid, 0) + 1

    # Start with all nodes that have no incoming edges (indegree 0).
    queue = deque([sid for sid in step_map if indegree.get(sid, 0) == 0])
    waves = []

    while queue:
        current_wave = []
        next_queue = deque()

        for _ in range(len(queue)):
            node = queue.popleft()
            current_wave.append(step_map[node])

            for neighbor in graph.get(node, []):
                indegree[neighbor] -= 1
                if indegree[neighbor] == 0:
                    next_queue.append(neighbor)

        if current_wave:
            waves.append(current_wave)
        queue = next_queue

    return waves


def _find_engineer_step(reviewer_sid: str, steps: list) -> Optional[str]:
    """Return the engineer step id that a reviewer depends on."""
    for s in steps:
        if s["id"] == reviewer_sid:
            deps = s.get("depends_on", [])
            if isinstance(deps, str):
                deps = [deps]
            return deps[0] if deps else None
    return None


def _find_step_def(sid: str, steps: list) -> Optional[dict]:
    """Find a step definition dict by id."""
    for s in steps:
        if s.get("id") == sid:
            return s
    return None


def _get_step_order(sid: str, steps: list) -> int:
    """Return the 0‑based index of a step in the pipeline definition."""
    for i, s in enumerate(steps):
        if s.get("id") == sid:
            return i
    return 0


def _agent_timeout(step_def: dict) -> int:
    """Extract timeout (seconds) from a step definition's agent config."""
    agent_def = load_agent(step_def.get("agent", "android-engineer"))
    return agent_def.get("opencode_config", {}).get("timeout", 300)
