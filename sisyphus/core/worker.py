"""Worker execution — spawn a role-scoped opencode subprocess pointed at the
agent-facing MCP server, instead of the old file contract.

Reuses core.worktree (git isolation) and core.launcher.launch_agent (subprocess
lifecycle: timeout, idle-timeout, cancellation) unmodified. The only new thing
is per-run agent-MCP wiring: a session token + a generated project-level
opencode.json pointing opencode at a LOCAL (stdio) MCP bridge
(tools/agent_mcp_stdio.py), which forwards to the daemon's /mcp/agent over
HTTP. Local, not remote — opencode's remote-MCP transport proved unreliable
in practice (see docs/adr/); every other MCP server on this machine, and our
own control-plane mcp_server.py, already use the local/stdio pattern.
"""
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

from . import agents, sessions, store
from .launcher import launch_agent
from .repos import get_repo
from .worktree import create_worktree, get_repo_name_from_url, init_bare_clone, set_access

DAEMON_URL = os.environ.get("SISYPHUS_DAEMON_URL", "http://127.0.0.1:7677")
_BRIDGE_SCRIPT = str(Path(__file__).resolve().parent.parent / "tools" / "agent_mcp_stdio.py")
# The absolute interpreter path, not bare "python3" — bare "python3" resolves
# via PATH inside opencode's spawned subprocess, which may silently differ
# from the interpreter actually running this daemon (confirmed to matter: a
# working spike used an absolute path, the bare-"python3" version produced
# zero requests reaching the daemon across three full live worker runs).
_PYTHON = sys.executable

# The name of the agent/persona we ask opencode to run as (see
# _write_opencode_config and spawn_worker's launch_agent call). Ours, not
# borrowed from opencode's own built-ins — see that function's docstring for
# why borrowing a built-in name (originally "build") turned out not to be
# durable.
_WORKER_AGENT = "sisyphus-worker"

# The name of our project-local MCP server entry (see _write_opencode_config
# and _build_prompt). NOT "sisyphus" — confirmed live that this machine
# already has an MCP server registered globally/per-project under the literal
# name "sisyphus" (almost certainly this repo's own control-plane
# mcp_server.py, set up for chat-based Sisyphus use outside of this worker
# system entirely). When our per-task config also tried to claim that name,
# the pre-existing one won: the model's real available-tools list showed
# only the old control-plane's double-prefixed tools (sisyphus_sisyphus_*)
# and none of ours — not a hallucination, a genuine name collision. A name
# nothing else on this machine uses avoids it entirely.
_MCP_SERVER_NAME = "sisyphus_agent"

# How many times to (re)run the orchestrator agent for one decision before
# giving up — a weak model occasionally ends its turn without calling a tool.
DECIDER_ATTEMPTS = 2


def cleanup_task(task_id: str, deep: bool = False) -> None:
    """Reclaim a finished task's disk: its git worktrees, per-task bare clone,
    and run logs. Called when a task hits a terminal status (see
    core.orchestrator._terminate) or is deleted. Best-effort; a git worktree
    dir removed out from under the bare clone is fine because the bare clone
    (its only registrar) is removed in the same sweep.

    *deep* (task deletion only) also removes the durable attachments/diff store
    under tasks/<id>. It's kept for done/failed tasks so the change patch stays
    retrievable, and only wiped when the task itself is deleted."""
    import glob
    import shutil
    import subprocess

    ws = os.environ.get("WORKSPACE_ROOT", "/workspace")
    patterns = [
        f"{ws}/repos/worktrees/{task_id}_*",   # per-role worktrees
        f"{ws}/scratch/{task_id}*",            # repo-less + orchestrator scratch dirs
        f"{ws}/runs/{task_id}",                # run logs
    ]
    # The per-task bare clone holds the branch a local Merge Request lands/merges
    # from — keep it while an MR is still open (unless this is a full delete).
    from . import mergerequests
    if deep or not mergerequests.open_mr_exists(task_id):
        patterns.append(f"{ws}/repos/{task_id}_*")   # per-task bare clone
    if deep:
        patterns.append(f"{ws}/tasks/{task_id}")   # attachments + saved patches

    def _salvage(worktree_path: str) -> None:
        try:
            r = subprocess.run(
                ["git", "-C", worktree_path, "status", "--porcelain", "--untracked-files=normal"],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0 or not r.stdout.strip():
                return
            subprocess.run(
                ["git", "-C", worktree_path, "add", "-A", "-N"],
                capture_output=True, text=True, timeout=30,
            )
            diff = subprocess.run(
                ["git", "-C", worktree_path, "diff", "HEAD"],
                capture_output=True, text=True, timeout=60,
            )
            if diff.returncode != 0 or not diff.stdout.strip():
                return
            dirname = os.path.basename(worktree_path)
            from . import attachments
            saved = attachments.save_text(task_id, f"salvaged-{dirname}.patch", diff.stdout)
            logger.warning("cleanup_task: salvaged uncommitted work from %s → %s", worktree_path, saved)
        except Exception as e:
            logger.debug("cleanup_task: salvage failed for %s: %s", worktree_path, e)

    worktree_glob = f"{ws}/repos/worktrees/{task_id}_*"
    for pattern in patterns:
        for path in glob.glob(pattern):
            if pattern == worktree_glob:
                _salvage(path)
            shutil.rmtree(path, ignore_errors=True)

    # A bare clone kept alive (open MR) has just lost its worktree dirs; prune
    # the stale registrations so a later respawn can re-add the same paths.
    for bare in glob.glob(f"{ws}/repos/{task_id}_*"):
        try:
            subprocess.run(["git", "-C", bare, "worktree", "prune"],
                           capture_output=True, text=True, timeout=30)
        except Exception as e:
            logger.debug("cleanup_task: worktree prune failed in %s: %s", bare, e)


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
    """Point opencode at our agent-MCP bridge as a LOCAL (stdio) MCP server.
    Role restriction is enforced server-side (by the token), so no
    tool-allowlisting is needed in this config.

    "plugin": [] overrides (not merges with) the user's global plugin list —
    this is what actually stops the global oh-my-openagent plugin from
    hijacking the run (it reroutes all tool calls through its own
    task/skill_mcp indirection and never lets the model see our raw
    tools). --pure was tried first, but confirmed by live probing (opencode
    debug logs + a direct tool-list run) to strip project-local MCP servers
    too — with --pure our own server never even connects. An explicit empty
    "plugin" array disables just the plugin, leaving project-local "mcp"
    entries (this one included) intact.

    "agent": {_WORKER_AGENT: {"mode": "primary"}} defines our OWN agent
    entirely within project config, instead of asking opencode to run one of
    its own built-in agents by name (originally "build"). That approach
    worked at first but broke without any change on our side: the global
    oh-my-openagent config can (and, on this machine, later did) reclassify
    a built-in agent name as a subagent, at which point opencode silently
    falls back to its own default — the very custom persona we were trying
    to avoid, reintroducing the skill_mcp indirection bug. A name we mint and
    define ourselves, with "mode" set explicitly, isn't subject to that.

    The MCP server is named _MCP_SERVER_NAME ("sisyphus_agent"), not
    "sisyphus" — see that constant's comment for why the obvious name
    collides with something already on this machine.
    """
    config = {
        "$schema": "https://opencode.ai/config.json",
        "plugin": [],
        "agent": {_WORKER_AGENT: {"mode": "primary"}},
        "mcp": {
            _MCP_SERVER_NAME: {
                "type": "local",
                "command": [_PYTHON, _BRIDGE_SCRIPT],
                "environment": {
                    "SISYPHUS_SESSION_TOKEN": token,
                    "SISYPHUS_DAEMON_URL": DAEMON_URL,
                },
                "enabled": True,
            }
        },
    }
    Path(worktree_path, "opencode.json").write_text(json.dumps(config, indent=2))


def _build_prompt(role_def: dict, goal: str, instruction: str, thread: str) -> str:
    # Name the tools explicitly, with opencode's actual server-prefixed form
    # (_MCP_SERVER_NAME + "_" + tool) — a repo that itself contains Sisyphus's
    # source (e.g. exploring this very project) can otherwise confuse the
    # model into calling a similarly-named control-plane tool (mcp_server.py's
    # sisyphus_get_card etc.) instead of the real agent tool. Naming them
    # exactly, and warning it off anything else "sisyphus"-looking it may
    # read in the code, removes the ambiguity.
    prefix = f"{_MCP_SERVER_NAME}_"
    tool_names = ", ".join(f"{prefix}{t}" for t in role_def["tools"])
    if "finish" in role_def["tools"]:
        finish_tool = f"{prefix}finish"
        finish_example = f'{finish_tool}(summary="Found 5 top-level directories: docs, sisyphus, ...")'
    else:
        finish_tool = f"{prefix}approve or {prefix}request_changes"
        finish_example = (
            f'{prefix}approve(summary="LGTM, tests pass") — or '
            f'{prefix}request_changes(reason="...") if it does not'
        )
    # A plain-text final reply is a live, confirmed failure mode: the model
    # answers the question in chat and stops, never touching any tool, even
    # with a "MANDATORY" section further down the prompt. Stating the rule
    # BEFORE the persona/task (primacy) and with a concrete example call
    # (models follow worked examples far more reliably than abstract "must
    # call" language) is the fix being tried here; the original single
    # end-of-prompt section alone was not enough to change the behavior.
    return (
        "# How this run ends — read this first\n"
        "Nobody reads your chat replies. This is not a conversation — it is "
        "a tool-driven task. Whatever you type as a plain-text message is "
        f"thrown away, not recorded, not seen by anyone. The ONLY way to "
        f"report your result is to actually CALL the `{finish_tool}` tool — "
        "the same way you'd call `read` or `bash`, as a real tool "
        "invocation, not as text describing what you would call.\n"
        f"Your very last action in this run, with no exceptions, looks like: "
        f"{finish_example}\n"
        "If your final action is a plain-text message instead of that tool "
        "call, this run is recorded as FAILED — even if the text answer was "
        "completely correct.\n\n"
        f"{role_def['persona']}\n\n"
        f"## Task goal\n{goal}\n\n"
        f"## Your instruction\n{instruction}\n\n"
        f"## Thread so far\n{thread}\n\n"
        "## Reporting back — MANDATORY, NOT OPTIONAL\n"
        f"You have EXACTLY these MCP tools available: {tool_names}. Use ONLY "
        "these exact names — if you see any other tool or code that looks "
        "'sisyphus'-related while exploring (e.g. a different MCP server or "
        "script), ignore it; it is not yours to call.\n"
        f"Call `{prefix}comment` at each milestone.\n"
        f"Your task is NOT complete until you have actually CALLED "
        f"`{finish_tool}` as a real tool invocation. Do not just describe or "
        "plan to call it — invoke it. Writing your own result.json file, or "
        "replying in chat, INSTEAD OF calling the tool is WRONG and will be "
        "treated as a failed run, even if the content is correct. "
        "The ONLY acceptable reason to skip the tool call is if invoking it "
        "actually errors — not merely because replying in chat feels easier "
        "or faster."
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
    role_def = agents.get_agent(role)   # `role` is now an agent name
    if not role_def:
        raise ValueError(f"unknown agent {role!r}")
    task = store.get_task(task_id)
    if not task:
        raise ValueError(f"unknown task {task_id}")

    # An agent may pin a specific CLI runtime; NULL runner_id → default runner.
    runner_name = None
    if role_def.get("runner_id"):
        from .runners import get_runner
        r = get_runner(role_def["runner_id"])
        runner_name = r["name"] if r else None

    model_override = role_def.get("model") or None

    requested_repo = repo_name or task.get("repo")
    bare_path, branch = _prepare_repo(task_id, requested_repo)
    if requested_repo and not bare_path:
        # The task named a repo we couldn't prepare — unregistered, or the
        # daemon's config cache predates its registration. Do NOT fall through
        # to the repo-less scratch dir: without a checked-out clone the agent's
        # native tools (bash with an arbitrary workdir, glob/edit on absolute
        # paths) have no git isolation and can reach — and commit to — whatever
        # is on disk, including the real source repo. Fail loudly instead; the
        # scratch path is only for tasks that genuinely have no repo.
        raise ValueError(
            f"repo {requested_repo!r} is not available to the worker (not "
            f"registered, or the daemon must reload its config) — refusing to "
            f"run the agent without an isolated worktree")
    if bare_path:
        # Every worker for a task works on the task's own branch
        # (sisyphus/<task_id>), cut from its base branch. Normally that's the repo
        # default, but a plan step is cut from its plan's integration branch
        # (task.base_branch) so it inherits everything earlier steps merged there
        # — that's how dependent steps see each other's code instead of each
        # re-cloning a pristine main and reinventing the interface.
        # The engineer checks it out attached so its commits advance the branch;
        # read-only roles (explorer/reviewer) check it out detached so they don't
        # hold it and block the engineer — only one worktree may hold a branch at
        # a time.
        from .git_ops import ensure_task_branch
        base_br = task.get("base_branch") or branch or "main"
        task_br = ensure_task_branch(bare_path, task_id, base_br)
        worktree_path = create_worktree(
            bare_path, task_id, role, task_br, detach=(role != "engineer"),
        )
        # Always writable: opencode needs to write its own .opencode/ session
        # state in the cwd regardless of role. "Don't modify the repo" for a
        # read-only role is enforced by that role's tool allowlist (no
        # commit/open_mr) and persona prompt, not by filesystem permissions —
        # a read-only worktree just breaks opencode outright.
        set_access(worktree_path, "write")
    else:
        # A repo-less task (research / planning / Q&A — nothing to check out)
        # still needs a private cwd where opencode can write its own session
        # state and read our generated opencode.json. A per-task scratch dir,
        # not a shared /tmp, so runs don't collide and cleanup can reclaim it.
        ws = os.environ.get("WORKSPACE_ROOT", "/workspace")
        worktree_path = f"{ws}/scratch/{task_id}"
        os.makedirs(worktree_path, exist_ok=True)

    token = sessions.mint(task_id, role, worktree=worktree_path)
    _write_opencode_config(worktree_path, token)

    store.post_message(
        task_id, author="system", kind="worker_started",
        text=f"spawning {role}: {instruction}",
    )

    prompt = _build_prompt(role_def, task["goal"], instruction, store.render_thread(task_id, for_prompt=True))
    # Layout matches what launcher._derive_run_id expects (runs/{run_id}/
    # resources/{step_id}), using task_id as the run id and role as the step
    # id — so worker logs land under runs/{task_id}/logs/{role}.log instead
    # of being mis-derived to a shared "runs/{role}/" bucket.
    output_dir = f"{os.environ.get('WORKSPACE_ROOT', '/workspace')}/runs/{task_id}/resources/{role}"
    os.makedirs(output_dir, exist_ok=True)

    agent_def = {"name": role, "opencode_config": {}}
    result = await launch_agent(
        agent_def, prompt, worktree_path, output_dir,
        timeout=timeout, idle_timeout=idle_timeout, step_id=f"{task_id}-{role}",
        runner_name=runner_name,
        # Our own agent, defined in the project config above — see
        # _write_opencode_config's docstring for why a borrowed built-in name
        # ("build") wasn't durable.
        agent=_WORKER_AGENT,
        model_override=model_override,
    )

    from .usage import record_run
    record_run(task_id, role, result.model, result.usage, result.duration_ms)

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


def _build_decider_prompt(agent: dict, thread_text: str, roster: list) -> str:
    """Prompt for the orchestrator agent: read the thread, call exactly one
    decision tool. The persona (decision doctrine) comes from the agent row;
    *roster* is the exact set of worker agents it may spawn (so it can't invent
    an agent name that doesn't exist — a real hallucination we hit in testing)."""
    prefix = f"{_MCP_SERVER_NAME}_"
    tools = ", ".join(prefix + t for t in agent["tools"] if t.startswith("decide_"))
    names = ", ".join(roster) or "explorer, engineer, reviewer"
    return (
        "# How this run ends — read this first\n"
        "You are an orchestrator. Read the task thread below and choose EXACTLY "
        f"ONE next action by CALLING one of these tools: {tools}. Call it as a "
        "real tool invocation (like `read` or `bash`), not as text describing it. "
        "Make exactly one call, then stop. A plain-text reply does nothing.\n\n"
        f"When you call decide_spawn_worker, the `agent` argument MUST be one of "
        f"these EXACT names — do not invent any other: {names}.\n\n"
        f"{agent['persona']}\n\n"
        f"## Task thread\n{thread_text}\n"
    )


async def spawn_decider(task_id: str, agent_name: str, thread_text: str,
                        timeout: Optional[int] = 180, idle_timeout: Optional[int] = 90):
    """Run the orchestrator agent as a local CLI session to produce ONE decision.
    Returns the recorded decision dict (from its decide_* tool call), or None if
    it produced none (crash/timeout). No thread messages, no worktree — it runs
    in an ephemeral scratch dir and only reads the thread."""
    agent = agents.get_agent(agent_name)
    if not agent:
        return None

    runner_name = None
    if agent.get("runner_id"):
        from .runners import get_runner
        r = get_runner(agent["runner_id"])
        runner_name = r["name"] if r else None

    model_override = agent.get("model") or None

    ws = os.environ.get("WORKSPACE_ROOT", "/workspace")
    scratch = f"{ws}/scratch/{task_id}-orch"
    os.makedirs(scratch, exist_ok=True)
    token = sessions.mint(task_id, agent_name, worktree=scratch)
    _write_opencode_config(scratch, token)

    roster = [a["name"] for a in agents.list_agents(kind="worker") if a.get("enabled", 1)]
    prompt = _build_decider_prompt(agent, thread_text, roster)
    output_dir = f"{ws}/runs/{task_id}/resources/orchestrator"
    os.makedirs(output_dir, exist_ok=True)

    # A weaker/cheaper model sometimes finishes its turn without calling a
    # decision tool (no decision recorded). Give it up to DECIDER_ATTEMPTS tries
    # before giving up (→ ask_human) — cheaper than parking the task on a human.
    from .usage import record_run
    agent_def = {"name": agent_name, "opencode_config": {}}
    decision = None
    for attempt in range(DECIDER_ATTEMPTS):
        result = await launch_agent(
            agent_def, prompt, scratch, output_dir,
            timeout=timeout, idle_timeout=idle_timeout, step_id=f"{task_id}-orchestrator",
            runner_name=runner_name, agent=_WORKER_AGENT,
            model_override=model_override,
        )
        record_run(task_id, agent_name, result.model, result.usage, result.duration_ms)
        session = sessions.resolve(token)
        decision = session.get("decision") if session else None
        if decision:
            break

    sessions.revoke(token)
    return decision
