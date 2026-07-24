"""Worker execution — spawn a role-scoped opencode subprocess pointed at the
agent-facing MCP server, instead of the old file contract.

Reuses core.worktree (git isolation) and core.launcher.launch_agent (subprocess
lifecycle: timeout, idle-timeout, cancellation) unmodified. The only new thing
is per-run agent-MCP wiring: a session token + a generated project-level
opencode.json pointing opencode at /mcp/agent with that token.
"""
import json
import os
from pathlib import Path
from typing import Optional

from . import sessions, store
from .launcher import launch_agent
from .repos import get_repo
from .roles import get_role
from .worktree import create_worktree, get_repo_name_from_url, init_bare_clone, set_access

# How much filesystem access each role gets in its worktree (reuses the
# existing read/analyze/write levels from core.worktree.set_access).
_ACCESS_BY_ROLE = {"explorer": "analyze", "engineer": "write", "reviewer": "read"}

DAEMON_URL = os.environ.get("SISYPHUS_DAEMON_URL", "http://127.0.0.1:7677")


def _prepare_repo(task_id: str, repo_name: Optional[str]):
    """Returns (bare_path, branch), or (None, None) if there's no repo to clone."""
    if not repo_name:
        return None, None
    repo = get_repo(repo_name)
    if not repo:
        return None, None
    workspace_root = os.environ.get("WORKSPACE_ROOT", "/workspace")
    resolved_name = get_repo_name_from_url(repo["source"])
    bare_path = f"{workspace_root}/repos/{task_id}_{resolved_name}"
    init_bare_clone(repo["source"], bare_path)
    return bare_path, repo.get("branch", "main")


def _write_opencode_config(worktree_path: str, token: str) -> None:
    """Point opencode at our agent-MCP server. Role restriction is enforced
    server-side (by the token), so no tool-allowlisting is needed here."""
    config = {
        "$schema": "https://opencode.ai/config.json",
        "mcp": {
            "sisyphus": {
                "type": "remote",
                "url": f"{DAEMON_URL}/mcp/agent",
                "headers": {"Authorization": f"Bearer {token}"},
                "enabled": True,
            }
        },
    }
    Path(worktree_path, "opencode.json").write_text(json.dumps(config, indent=2))


def _build_prompt(role_def: dict, goal: str, instruction: str, thread: str) -> str:
    return (
        f"{role_def['persona']}\n\n"
        f"## Task goal\n{goal}\n\n"
        f"## Your instruction\n{instruction}\n\n"
        f"## Thread so far\n{thread}\n\n"
        "## Reporting back\n"
        "You have MCP tools (the `sisyphus` server) to report progress and your "
        "result. Call `comment` at each milestone. Call `finish` (or, if you are "
        "a reviewer, `approve`/`request_changes`) as your LAST action — that is "
        "the only way this step ends. If those tools are ever unavailable, write "
        'a JSON file `result.json` ({"status":"completed"|"failed","summary":...}) '
        "into $SISYPHUS_OUTPUT_DIR as a fallback."
    )


async def spawn_worker(
    task_id: str,
    role: str,
    instruction: str,
    repo_name: Optional[str] = None,
    timeout: Optional[int] = 1800,
    idle_timeout: Optional[int] = 300,
) -> None:
    """Launch one worker for *task_id*. Fire-and-forget: the outcome lands on
    the thread via the agent's own tool calls, not in a return value."""
    role_def = get_role(role)
    task = store.get_task(task_id)
    if not task:
        raise ValueError(f"unknown task {task_id}")

    bare_path, branch = _prepare_repo(task_id, repo_name or task.get("repo"))
    if bare_path:
        worktree_path = create_worktree(bare_path, task_id, role, branch or "main")
        set_access(worktree_path, _ACCESS_BY_ROLE.get(role, "read"))
    else:
        worktree_path = "/tmp"

    token = sessions.mint(task_id, role, worktree=worktree_path)
    if worktree_path != "/tmp":
        _write_opencode_config(worktree_path, token)

    store.post_message(
        task_id, author="system", kind="worker_started",
        text=f"spawning {role}: {instruction}",
    )

    prompt = _build_prompt(role_def, task["goal"], instruction, store.render_thread(task_id))
    output_dir = f"{os.environ.get('WORKSPACE_ROOT', '/workspace')}/tasks/{task_id}/{role}"
    os.makedirs(output_dir, exist_ok=True)

    agent_def = {"name": role, "opencode_config": {}}
    result = await launch_agent(
        agent_def, prompt, worktree_path, output_dir,
        timeout=timeout, idle_timeout=idle_timeout, step_id=f"{task_id}-{role}",
    )

    # If the worker never called finish()/approve()/request_changes(), its
    # session is still active — that's the crash/timeout fallback signal.
    if sessions.resolve(token) is not None:
        sessions.revoke(token)
        store.post_message(
            task_id, author="system", kind="worker_finished",
            text=(
                f"{role} ended without reporting a result "
                f"(status={result.status}): {result.error or result.summary or 'no output'}"
            ),
        )
        # Deferred import: core.orchestrator imports spawn_worker from this
        # module at its top level, so importing it back here at module load
        # time would be circular. By call time both modules are fully loaded.
        from .orchestrator import emit
        emit(task_id, "worker_finished")
