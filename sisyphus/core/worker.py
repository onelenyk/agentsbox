"""Worker execution — spawn a role-scoped agent CLI subprocess pointed at the
agent-facing MCP server, instead of the old file contract.

Reuses core.worktree (git isolation) and core.launcher.launch_agent (subprocess
lifecycle: timeout, idle-timeout, cancellation) unmodified. The only new thing
is per-run agent-MCP wiring: a session token + a generated config pointing the
CLI at a LOCAL (stdio) MCP bridge (tools/agent_mcp_stdio.py), which forwards to
the daemon's /mcp/agent over HTTP. Local, not remote — opencode's remote-MCP
transport proved unreliable in practice (see docs/adr/); every other MCP server
on this machine, and our own control-plane mcp_server.py, already use the
local/stdio pattern.

Which CLI runs is a property of the resolved runner row, and everything that
differs between CLIs — config file shape and location, and how the model must
spell our tool names in a call — comes from core.runner_profiles. This module
resolves the runner's type once per run and threads it through both.
"""
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

from . import agents, sessions, store
from .launcher import AgentResult, launch_agent
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
# core.runner_profiles.write_mcp_config and spawn_worker's launch_agent call).
# Ours, not borrowed from opencode's own built-ins — see that function's
# docstring for why borrowing a built-in name (originally "build") turned out
# not to be durable. opencode-only: Claude Code has no equivalent concept.
_WORKER_AGENT = "sisyphus-worker"

# The name of our MCP server entry (see core.runner_profiles.write_mcp_config
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
    under tasks/<id> and the run logs under runs/<id>. Both are kept for
    done/failed tasks — the change patch stays retrievable, and the run logs
    are the only forensic record of what a worker actually did (the one
    "commit blocked by sandbox" incident was undiagnosable for days precisely
    because cleanup had eaten them)."""
    import glob
    import shutil
    import subprocess

    ws = os.environ.get("WORKSPACE_ROOT", "/workspace")
    patterns = [
        f"{ws}/repos/worktrees/{task_id}_*",   # per-role worktrees
        f"{ws}/scratch/{task_id}*",            # repo-less + orchestrator scratch dirs
    ]
    # The per-task bare clone holds the branch a local Merge Request lands/merges
    # from — keep it while an MR is still open (unless this is a full delete).
    from . import mergerequests
    if deep or not mergerequests.open_mr_exists(task_id):
        patterns.append(f"{ws}/repos/{task_id}_*")   # per-task bare clone
    if deep:
        patterns.append(f"{ws}/tasks/{task_id}")   # attachments + saved patches
        patterns.append(f"{ws}/runs/{task_id}")    # run logs (forensics)

    def _salvage(worktree_path: str) -> None:
        # The runner's own config (opencode.json, .opencode/, .claude/) is
        # written into every worktree by us — "uncommitted work" it is not.
        # Without the exclusion every single run got "salvaged", which spammed
        # a WARNING per worker per day and buried the real salvages it exists
        # to make visible.
        from .runner_profiles import workspace_artifacts
        exclude = ["--"] + [f":(exclude){p}" for p in workspace_artifacts()]
        try:
            r = subprocess.run(
                ["git", "-C", worktree_path, "status", "--porcelain",
                 "--untracked-files=normal", *exclude],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0 or not r.stdout.strip():
                return
            subprocess.run(
                ["git", "-C", worktree_path, "add", "-A", "-N"],
                capture_output=True, text=True, timeout=30,
            )
            diff = subprocess.run(
                ["git", "-C", worktree_path, "diff", "HEAD", *exclude],
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




def _effective_model(model_override: Optional[str], runner_name: Optional[str]) -> str:
    """The model this run will actually use: the agent's override, else the
    runner row's. opencode needs it written into the generated agent config —
    a primary agent with no model hangs the CLI (see
    core.runner_profiles.write_mcp_config)."""
    if model_override:
        return model_override
    from .runners import resolve_runner
    return (resolve_runner(runner_name or "") or {}).get("model") or ""


def _write_runner_config(rtype: str, worktree_path: str, output_dir: str,
                         token: str, model: str = "",
                         binary: str = "") -> Optional[str]:
    """Point *rtype*'s CLI at our stdio MCP bridge for this one run.

    Returns a config path to hand back to the launcher, or None when the CLI
    finds the config itself (opencode reads opencode.json from its cwd).
    """
    from . import runner_profiles
    return runner_profiles.write_mcp_config(
        rtype,
        model=model,
        worktree_path=worktree_path,
        output_dir=output_dir,
        server_name=_MCP_SERVER_NAME,
        command=[_PYTHON, _BRIDGE_SCRIPT],
        env={
            "SISYPHUS_SESSION_TOKEN": token,
            "SISYPHUS_DAEMON_URL": DAEMON_URL,
        },
        agent_name=_WORKER_AGENT,
        binary=binary,
    )


def _build_prompt(role_def: dict, goal: str, instruction: str, thread: str,
                  prefix: str = f"{_MCP_SERVER_NAME}_") -> str:
    # Name the tools explicitly, in the runner's own server-prefixed form
    # (*prefix*, from core.runner_profiles.tool_prefix — opencode spells it
    # `<server>_<tool>`, Claude Code `mcp__<server>__<tool>`) — a repo that
    # itself contains Sisyphus's source (e.g. exploring this very project) can
    # otherwise confuse the model into calling a similarly-named control-plane
    # tool (mcp_server.py's sisyphus_get_card etc.) instead of the real agent
    # tool. Naming them exactly, and warning it off anything else
    # "sisyphus"-looking it may read in the code, removes the ambiguity.
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


# The real limits a worker runs under. Named, so the Daemon page can show what
# is actually enforced instead of a config key that enforces nothing — the page
# advertised `default_timeout: 300` from config.yaml, which no code has ever
# read, while workers in fact get 1800s. Anyone sizing a long job off that page
# would have been off by 6x.
WORKER_TIMEOUT_SECONDS = 1800       # total wall-clock for one worker run
WORKER_IDLE_TIMEOUT_SECONDS = 300   # silence before the run is abandoned


# Written into the thread when a run is cancelled, and matched when deciding
# whether a run merely stopped or genuinely produced nothing. Shared so the two
# sides cannot drift apart.
STOPPED_MARKER = "STOPPED BY A HUMAN"


def was_stopped_by_a_human(thread: list) -> bool:
    """True if the most recent finished run was cancelled rather than failed.

    A pinned (single-agent) task is driven by plain code, not by a model: no
    result on the thread means fail. So a stopped pinned task was failed on the
    spot, and stayed unfailable-but-unresumable — replying re-ran the same
    branch and failed it again. The distinction has to be readable from the
    thread, because the thread is the only state.
    """
    for m in reversed(thread):
        if m.get("kind") == "worker_finished":
            return STOPPED_MARKER in (m.get("text") or "")
        if m.get("kind") in ("result", "worker_started"):
            return False
    return False


def _published_mr_in_current_run(task_id: str) -> bool:
    """Did the run that is ending now get as far as opening a merge request?
    Only the segment after the LAST worker_started counts — an MR from an
    earlier iteration (e.g. one a reviewer sent back for changes) must not make
    a fresh failed run look like delivered work."""
    thread = store.get_thread(task_id)
    last_start = None
    for i, m in enumerate(thread):
        if m["kind"] == "worker_started":
            last_start = i
    if last_start is None:
        return False
    return any(m["kind"] == "mr" for m in thread[last_start:])


def worker_end_message(role: str, result, task_id: str = "") -> str:
    """What goes on the thread when a worker stops without reporting a result.

    The orchestrator reads exactly this line to choose the next action, so the
    wording is behaviour, not prose. A cancelled worker used to report with the
    same phrasing as a crashed one — "ended without reporting a result" — and
    the moment a stopped task was resumed the orchestrator answered
    "[fail] explorer ended without a result", failing work that was merely
    interrupted by a human.
    """
    if getattr(result, "status", "") == "cancelled":
        return (f"{role} was {STOPPED_MARKER} before it reported. This is not a "
                f"failure and nothing is known to be wrong with the work — the "
                f"instruction was interrupted part-way. Do not fail the task for "
                f"this; continue from the human's next instruction.")
    msg = (f"{role} ended without reporting a result "
           f"(status={getattr(result, 'status', '?')}): {_diagnose(result)}")
    if task_id and _published_mr_in_current_run(task_id):
        # The exact failure this guards (task-60088d8f): an engineer opened the
        # MR, stalled on dead tooling, timed out — and the orchestrator, reading
        # "ended without a result", spawned a second engineer to redo a finished
        # job. The next line is behaviour, not prose: the decider reads it.
        msg += (" HOWEVER: a merge request was already opened during this run — "
                "the work is delivered, not lost. Do NOT spawn another engineer "
                "to redo it; the next step is a reviewer (or done, if one has "
                "already approved).")
    return msg


def _diagnose(result) -> str:
    """Why a worker stopped without closing its session, in words that point
    somewhere.

    Every runner failure this system has hit reached the thread as the single
    word "no output", which is true and useless: it covers a CLI that never
    started, one that died with an explanation on stderr, one that hung
    producing nothing, and one that did a full turn of real work and simply
    never called the finishing tool. Those need four different fixes, and the
    evidence telling them apart — stderr, and whether any model steps were
    billed — was already on the result and thrown away.
    """
    explicit = (getattr(result, "error", "") or "").strip()
    if explicit:
        return explicit

    stdout = (getattr(result, "raw_stdout", "") or "").strip()
    stderr = (getattr(result, "raw_stderr", "") or "").strip()
    steps = ((getattr(result, "usage", None) or {}).get("steps") or 0)

    if not stdout:
        if stderr:
            # The Claude wrapper-script failure said exactly what was wrong here
            # and nobody saw it for a day.
            return (f"the runner wrote nothing to stdout and exited. Its error was: "
                    f"{stderr[:400]}")
        return ("the runner produced no output at all — it never started, or it hung "
                "and was killed. Run a smoke test on this runner (Runners → Smoke "
                "test) to see which.")

    if steps:
        # The infrastructure worked; the agent chose not to finish.
        return (f"the agent ran ({steps} model step(s)) but never called its finishing "
                f"tool, so the work is unreported rather than undone. Check the run log "
                f"for what it did; if its tools were missing, the MCP wiring is at fault, "
                f"and if they were present it stopped early.")

    return (f"the runner produced output but no model steps were billed — it likely "
            f"failed before reaching the model. Last output: {(stderr or stdout)[:300]}")


def _prepare_workspace(task_id: str, role: str, task: dict, requested_repo):
    """Everything a worker needs on disk before its CLI starts: the bare clone,
    the task branch, the worktree, its access mode and the repo's setup hook.

    Every step is blocking git or shell work that can run for minutes on a big
    repo or a real setup hook. spawn_worker is a coroutine on the daemon's only
    event loop, so running this inline froze the whole daemon — web, API,
    dispatcher, watchdog, autopilots — until the clone finished. It is called
    through asyncio.to_thread for that reason; keep it sync and keep it here.

    Returns (worktree_path, setup_result).
    """
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
        # A worktree has exactly what git tracks; anything gitignored the build
        # needs (local.properties, .env, credentials) is absent unless this repo
        # says how to produce it. Runs AFTER set_access on purpose — set_access
        # rewrites every mode in the tree to 644/755, so copying first would
        # widen a 600 credentials file. Raises on a configured-but-failed hook:
        # see core.setup_hook's failure policy. _spawn_worker_safe turns that
        # into "<role> failed to start: …" on the thread, and no agent runs
        # against a half-prepared worktree.
        from .setup_hook import apply_setup
        setup_result = apply_setup(requested_repo, worktree_path)
    else:
        # A repo-less task (research / planning / Q&A — nothing to check out)
        # still needs a private cwd where opencode can write its own session
        # state and read our generated opencode.json. A per-task scratch dir,
        # not a shared /tmp, so runs don't collide and cleanup can reclaim it.
        ws = os.environ.get("WORKSPACE_ROOT", "/workspace")
        worktree_path = f"{ws}/scratch/{task_id}"
        os.makedirs(worktree_path, exist_ok=True)
        setup_result = None     # no repo, so nothing to set up — not "setup succeeded"
    return worktree_path, setup_result


async def spawn_worker(
    task_id: str,
    role: str,
    instruction: str,
    repo_name: Optional[str] = None,
    timeout: Optional[int] = WORKER_TIMEOUT_SECONDS,
    idle_timeout: Optional[int] = WORKER_IDLE_TIMEOUT_SECONDS,
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
    worktree_path, setup_result = await asyncio.to_thread(
        _prepare_workspace, task_id, role, task, requested_repo)
    instruction_first_line = instruction.split("\n")[0][:120]
    token = sessions.mint(task_id, role, worktree=worktree_path,
                          instruction=instruction_first_line,
                          model=model_override or "",
                          runner=runner_name or "",
                          kind="worker")
    store.post_message(
        task_id, author="system", kind="worker_started",
        text=f"spawning {role}: {instruction}",
    )
    if setup_result and setup_result.get("ran"):
        # Only when a hook actually did something. A repo without one stays
        # silent rather than posting a reassuring line about work that never
        # happened — the thread is where a human reconstructs what the worker
        # was given, and an unconditional "setup ok" would be a lie for every
        # repo that configures nothing.
        from .setup_hook import summarise as _summarise_setup
        store.post_message(task_id, author="system", kind="note",
                           text=_summarise_setup(setup_result))

    # Layout matches what launcher._derive_run_id expects (runs/{run_id}/
    # resources/{step_id}), using task_id as the run id and role as the step
    # id — so worker logs land under runs/{task_id}/logs/{role}.log instead
    # of being mis-derived to a shared "runs/{role}/" bucket.
    output_dir = f"{os.environ.get('WORKSPACE_ROOT', '/workspace')}/runs/{task_id}/resources/{role}"
    os.makedirs(output_dir, exist_ok=True)

    # Which CLI actually runs decides two things the prompt and config must
    # agree on: where its MCP config lives, and how the model must spell our
    # tool names. Resolve once, through the same chain the launcher uses.
    from . import runner_profiles
    from .runners import resolve_runner
    runner_row = resolve_runner(runner_name or "")
    rtype = runner_profiles.normalize_type(runner_row)
    # Read what the checkout's own opencode config declares BEFORE the write
    # below replaces the winning slot — then say it on the thread. A repo is
    # added by a human who vouches for it, so its config is overridden rather
    # than refused; overridden LOUDLY, because a silently neutralized config
    # is indistinguishable from one that was honoured (docs/known-gaps.md).
    declared = (runner_profiles.repo_declared_opencode(worktree_path)
                if rtype == "opencode" else {"mcp": [], "plugin": [], "files": []})
    mcp_config = _write_runner_config(rtype, worktree_path, output_dir, token,
                                      model=_effective_model(model_override, runner_name),
                                      binary=runner_row.get("binary", ""))
    if declared["files"]:
        bits = []
        if declared["plugin"]:
            bits.append(f"plugin list suppressed ({', '.join(declared['plugin'])})")
        if declared["mcp"]:
            bits.append(f"{len(declared['mcp'])} MCP server(s) disabled by name "
                        f"({', '.join(declared['mcp'])})")
        store.post_message(task_id, "system", kind="note",
                           text=(f"[sandbox] this checkout carries its own opencode config "
                                 f"({', '.join(declared['files'])}) — overridden: "
                                 + ("; ".join(bits) if bits else "nothing it declared survives")))
    prefix = runner_profiles.tool_prefix(rtype, _MCP_SERVER_NAME)

    prompt = _build_prompt(role_def, task["goal"], instruction,
                           store.render_thread(task_id, for_prompt=True),
                           prefix=prefix)

    agent_def = {"name": role, "opencode_config": {}}
    # Publish a cancel event for the lifetime of the run. The launcher's monitor
    # loop already knows what to do with it; until now nobody handed it one, so
    # stopping a task meant killing a pid by hand.
    from . import cancellation
    cancel_event = cancellation.register(task_id, role)
    try:
        if cancel_event.is_set():
            # Stopped before we got as far as launching. Starting the runner
            # just to kill it a moment later wastes a model call and muddies the
            # thread; report it the same way a mid-run cancellation reports.
            logger.info("worker %s on %s was cancelled before launch", role, task_id)
            result = AgentResult(status="cancelled", error="cancelled before launch")
        else:
            result = await launch_agent(
                agent_def, prompt, worktree_path, output_dir,
                timeout=timeout, idle_timeout=idle_timeout,
                step_id=f"{task_id}-{role}",
                runner_name=runner_name,
                # Our own agent, defined in the project config above — see
                # core.runner_profiles.write_mcp_config for why a borrowed built-in
                # name ("build") wasn't durable.
                agent=_WORKER_AGENT,
                model_override=model_override,
                cancel_event=cancel_event,
                mcp_config=mcp_config,
                # So the UI can tell a worker that is thinking from one that
                # has died. The thread stays empty either way.
                on_output=lambda: sessions.note_output(task_id),
            )
    finally:
        cancellation.unregister(task_id, role)

    from .usage import record_run
    record_run(task_id, role, result.model, result.usage, result.duration_ms)

    # If the worker never called finish()/approve()/request_changes(), its
    # session is still active — that's the crash/timeout fallback signal.
    if sessions.resolve(token) is not None:
        sessions.revoke(token)
        store.post_message(task_id, author="system", kind="worker_finished",
                           text=worker_end_message(role, result, task_id))
        # Deferred import: core.orchestrator imports spawn_worker from this
        # module at its top level, so importing it back here at module load
        # time would be circular. By call time both modules are fully loaded.
        from .orchestrator import emit
        emit(task_id, "worker_finished")


def _build_decider_prompt(agent: dict, thread_text: str, roster: list,
                          prefix: str = f"{_MCP_SERVER_NAME}_") -> str:
    """Prompt for the orchestrator agent: read the thread, call exactly one
    decision tool. The persona (decision doctrine) comes from the agent row;
    *roster* is the exact set of worker agents it may spawn (so it can't invent
    an agent name that doesn't exist — a real hallucination we hit in testing).
    *prefix* is the runner's MCP tool-naming convention (see _build_prompt)."""
    tools = ", ".join(prefix + t for t in agent["tools"] if t.startswith("decide_"))
    names = ", ".join(roster) or "explorer, engineer, reviewer"
    return (
        "# How this run ends — read this first\n"
        "You are an orchestrator. Read the task thread below and choose EXACTLY "
        f"ONE next action by CALLING one of these tools: {tools}. Call it as a "
        "real tool invocation (like `read` or `bash`), not as text describing it. "
        "Make exactly one call, then stop. A plain-text reply does nothing.\n"
        "IMPORTANT: the tool call only RECORDS your decision — it is executed "
        "AFTER this session ends. You will never see its effects (no worker "
        "output, no thread changes) inside this session; the absence of visible "
        "results does NOT mean the action failed, and calling decide_* again "
        "will be rejected. One call, then end your turn.\n\n"
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
    token = sessions.mint(task_id, agent_name, worktree=scratch,
                          instruction="deciding next step",
                          model=model_override or "",
                          runner=runner_name or "",
                          kind="decider")

    output_dir = f"{ws}/runs/{task_id}/resources/orchestrator"
    os.makedirs(output_dir, exist_ok=True)

    from . import runner_profiles
    from .runners import resolve_runner
    runner_row = resolve_runner(runner_name or "")
    rtype = runner_profiles.normalize_type(runner_row)
    mcp_config = _write_runner_config(rtype, scratch, output_dir, token,
                                      model=_effective_model(model_override, runner_name),
                                      binary=runner_row.get("binary", ""))

    roster = [a["name"] for a in agents.list_agents(kind="worker") if a.get("enabled", 1)]
    prompt = _build_decider_prompt(
        agent, thread_text, roster,
        prefix=runner_profiles.tool_prefix(rtype, _MCP_SERVER_NAME),
    )

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
            model_override=model_override, mcp_config=mcp_config,
            on_output=lambda: sessions.note_output(task_id),
        )
        record_run(task_id, agent_name, result.model, result.usage, result.duration_ms)
        session = sessions.resolve(token)
        decision = session.get("decision") if session else None
        if decision:
            break

    sessions.revoke(token)
    return decision
