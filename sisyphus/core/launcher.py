"""Agent launcher — spawns opencode subprocess with timeout, cancel, validation."""
import asyncio
import json
import logging
import os
import platform
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

OUTPUT_SIZE_CAP = 10 * 1024 * 1024
GRACEFUL_KILL_TIMEOUT = 5

WORKSPACE_ROOT = os.environ.get("WORKSPACE_ROOT", "/workspace")
RUNS_DIR = Path(WORKSPACE_ROOT) / "runs"


def save_agent_log(run_id: str, step_id: str, content: str) -> str:
    """Save agent stdout/stderr to logs directory."""
    log_dir = RUNS_DIR / run_id / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{step_id}.log"
    log_path.write_text(content, encoding="utf-8")
    return str(log_path)


@dataclass
class AgentResult:
    status: str
    summary: str = ""
    error: Optional[str] = None
    duration_ms: float = 0
    raw_stdout: str = ""
    raw_stderr: str = ""
    usage: Optional[dict] = None   # token/cost summary parsed from the runner output
    model: str = ""                # the model that ran (from the runner config)


def _derive_run_id(output_dir: str) -> str:
    """Extract run_id from output_dir path.

    Expected layout: /workspace/runs/{run_id}/resources/{step_id}
    """
    p = Path(output_dir)
    parts = list(p.parts)
    try:
        res_idx = parts.index("resources")
        if res_idx > 0:
            return parts[res_idx - 1]
    except ValueError:
        pass
    if len(parts) >= 2 and parts[-2] == "runs":
        return parts[-1]
    return p.name if p.name else "unknown"


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return

    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=GRACEFUL_KILL_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()


async def _read_stream(stream: asyncio.StreamReader, buf: bytearray, activity: dict, cap: int) -> None:
    """Read *stream* in chunks, stamping ``activity["ts"]`` on every chunk received.

    Reading incrementally (instead of waiting for EOF) is what makes idle
    detection possible — a `read()` that only resolves at EOF gives no signal
    until the process has already finished. Output is capped at *cap* bytes
    while reading, not after, so a long-idle-tolerant agent can't grow the
    buffer unbounded before that cap is applied.
    """
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return
        activity["ts"] = time.monotonic()
        if len(buf) < cap:
            buf.extend(chunk[: cap - len(buf)])


async def _monitored_communicate(
    proc: asyncio.subprocess.Process,
    timeout: Optional[int],
    idle_timeout: Optional[int],
    cancel_event: Optional[asyncio.Event],
    poll_interval: float = 0.5,
) -> tuple:
    """Drive a subprocess to completion while enforcing total/idle timeouts and cancellation.

    Returns ``(status, error, stdout_bytes, stderr_bytes)`` where status is
    one of "completed", "timeout", "cancelled".
    """
    start_time = time.monotonic()
    activity = {"ts": start_time}
    stdout_buf, stderr_buf = bytearray(), bytearray()

    stdout_task = asyncio.create_task(_read_stream(proc.stdout, stdout_buf, activity, OUTPUT_SIZE_CAP))
    stderr_task = asyncio.create_task(_read_stream(proc.stderr, stderr_buf, activity, OUTPUT_SIZE_CAP))
    waiters = {stdout_task, stderr_task}
    cancel_task = None
    if cancel_event is not None:
        cancel_task = asyncio.create_task(cancel_event.wait())
        waiters.add(cancel_task)

    try:
        while True:
            # Wakes immediately on cancel/EOF, or after poll_interval — whichever first.
            await asyncio.wait(waiters, timeout=poll_interval, return_when=asyncio.FIRST_COMPLETED)

            if cancel_event is not None and cancel_event.is_set():
                await _terminate_process(proc)
                return "cancelled", None, bytes(stdout_buf), bytes(stderr_buf)

            now = time.monotonic()
            if timeout is not None and now - start_time > timeout:
                await _terminate_process(proc)
                return "timeout", f"Agent exceeded {timeout}s timeout", bytes(stdout_buf), bytes(stderr_buf)

            if idle_timeout is not None and now - activity["ts"] > idle_timeout:
                await _terminate_process(proc)
                return (
                    "timeout",
                    f"Agent exceeded {idle_timeout}s idle timeout — no output",
                    bytes(stdout_buf),
                    bytes(stderr_buf),
                )

            if stdout_task.done() and stderr_task.done():
                await proc.wait()
                return "completed", None, bytes(stdout_buf), bytes(stderr_buf)
    finally:
        for t in (stdout_task, stderr_task, *([cancel_task] if cancel_task else [])):
            if not t.done():
                t.cancel()


async def launch_agent(
    agent_def: dict,
    prompt: str,
    worktree_path: str,
    output_dir: str,
    timeout: Optional[int] = None,
    idle_timeout: Optional[int] = None,
    cancel_event: Optional[asyncio.Event] = None,
    runner_name: Optional[str] = None,
    step_id: str = "",
    pure: bool = False,
    agent: Optional[str] = None,
    model_override: Optional[str] = None,
) -> AgentResult:
    """Spawn AI runner subprocess, capture output.

    Handles total timeout and idle timeout (no output for N seconds) via
    SIGTERM → 5s → SIGKILL, cancellation via Event, output capping at 10MB.
    Success/failure of the underlying task is NOT judged here — the caller
    (core.worker) determines it from whether the agent-MCP session was closed
    via a real `finish`/`approve`/`request_changes` tool call.

    Runner selection: if *runner_name* is given, looks up that runner.
    Otherwise falls back to agent_def's "runner" field, then default runner,
    then hardcoded "opencode" as last resort.

    *pure*: pass --pure (disables the runner's external plugins). CAUTION:
    confirmed live to also strip project-local MCP servers (opencode.json's
    own "mcp" entries) along with the global plugin — do not combine with a
    worker that depends on its own local MCP server (see core.worker, which
    does NOT set this and instead overrides "plugin": [] in the generated
    project config to disable just the global plugin).

    *agent*: pass --agent <name> to pick a specific opencode agent/persona.
    Needed by task/thread workers: a user's global config can register its
    own custom primary agent (e.g. via a plugin) whose system prompt hardwires
    routing ALL tool calls through a "skill"/"skill_mcp" indirection layer —
    that persona is a separate "agent" config entry, not a plugin, so
    disabling plugins does not stop it from being selected as the default.
    Pass the name of an agent YOU defined in project config (core.worker does
    this), not one of opencode's own built-in names — a built-in name's
    primary/subagent classification lives in the global config too and can
    change there outside of anything this code controls (confirmed live: a
    "build" agent that worked one day was reclassified as a subagent the
    next, silently falling back to the exact persona this param exists to
    avoid).
    """
    from .runners import get_runner_by_name, get_default_runner

    if runner_name:
        runner = get_runner_by_name(runner_name)
    else:
        runner = get_runner_by_name(agent_def.get("runner", ""))
    if not runner:
        runner = get_default_runner()
    if not runner:
        runner = {"binary": "opencode", "model": "", "api_key_env": "", "base_url": "", "extra_args": "[]"}

    # Resolve to an absolute path so the launch works under a minimal PATH
    # (e.g. a launchd-supervised daemon), falling back to the bare name.
    from .runner_detect import resolve as _resolve_binary
    binary = _resolve_binary(runner["binary"]) or runner["binary"]
    # Verify the binary exists up front: once the command is wrapped in
    # sandbox-exec (which does exist), a missing runner would no longer raise
    # FileNotFoundError here — sandbox-exec would spawn and merely exit nonzero,
    # masking the real cause. Fail fast with the actionable message instead.
    if not (os.path.isabs(binary) and os.path.exists(binary)) and shutil.which(binary) is None:
        return AgentResult(
            status="error",
            error=f"Runner binary '{binary}' not found. Check runner config or install it.",
        )
    extra_args = json.loads(runner.get("extra_args", "[]") or "[]")

    start_time = time.monotonic()
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # The worker must not inherit the daemon's WORKSPACE_ROOT (its tool calls
    # could then resolve — and its test runs open — the live daemon DB). But
    # simply DELETING it made every agent-run pytest fall back to /workspace,
    # which doesn't exist on macOS → hundreds of sqlite3.OperationalError noise
    # lines that reviewers kept misreporting as "pre-existing sandbox issues".
    # Point it at a private per-run dir instead: isolated AND functional.
    env = dict(os.environ)
    env["WORKSPACE_ROOT"] = str(output_path / "wsroot")
    Path(env["WORKSPACE_ROOT"]).mkdir(parents=True, exist_ok=True)
    env["SISYPHUS_OUTPUT_DIR"] = str(output_dir)
    env["SISYPHUS_WORKTREE"] = str(worktree_path)
    env["SISYPHUS_DB_READONLY"] = "1"
    if model_override or runner.get("model"):
        env["SISYPHUS_MODEL"] = model_override or runner["model"]

    api_env = runner.get("api_key_env", "")
    if api_env:
        key = os.environ.get(api_env, "")
        if key:
            env[api_env] = key
            env["OPENROUTER_API_KEY"] = key

    if runner.get("base_url"):
        env["OPENROUTER_BASE_URL"] = runner["base_url"]

    agent_env = agent_def.get("opencode_config", {}).get("env", {})
    env.update(agent_env)

    args = [binary, "run"] + extra_args + [
        prompt,
        "--format", "json",
        "--auto",
    ]
    if pure:
        args.append("--pure")
    if agent:
        args.extend(["--agent", agent])
    if worktree_path and worktree_path != "/tmp":
        args.extend(["--dir", str(worktree_path)])

    # Confine the runner's filesystem writes to the workspace (see core.sandbox).
    # Without a backend the run proceeds unconfined — surface that once so it's
    # never a silent assumption.
    from . import sandbox
    if sandbox.enabled():
        args = sandbox.wrap(args, str(worktree_path), str(output_dir))
    elif not sandbox.available():
        logger.warning("no filesystem sandbox on this platform (%s) — worker runs unconfined",
                       platform.system())

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(worktree_path) if worktree_path and worktree_path != "/tmp" else None,
        )
    except FileNotFoundError:
        return AgentResult(
            status="error",
            error=f"Runner binary '{binary}' not found. Check runner config or install it.",
        )
    except Exception as e:
        return AgentResult(
            status="error",
            error=f"Failed to spawn '{binary}' process: {e}",
        )

    try:
        status, err, stdout, stderr = await _monitored_communicate(proc, timeout, idle_timeout, cancel_event)
    except Exception as e:
        try:
            proc.kill()
            await proc.wait()
        except Exception as e:
            logger.debug("Failed to kill process after communication error: %s", e)
            pass
        duration = (time.monotonic() - start_time) * 1000
        return AgentResult(status="error", duration_ms=duration, error=str(e))

    duration = (time.monotonic() - start_time) * 1000

    stdout_str = stdout.decode("utf-8", errors="replace") if stdout else ""
    stderr_str = stderr.decode("utf-8", errors="replace") if stderr else ""

    run_id = _derive_run_id(output_dir)
    step = step_id or output_path.name
    log_content = f"=== STDOUT ===\n{stdout_str}\n=== STDERR ===\n{stderr_str}"
    try:
        save_agent_log(run_id, step, log_content)
    except Exception as e:
        logger.debug("Failed to save agent log for run %s step %s: %s", run_id, step, e)
        pass

    # Token/cost usage is reported even on a timeout/cancel (work happened
    # before the cutoff), so parse it regardless of terminal status.
    from .usage import parse_usage
    usage = parse_usage(stdout_str)
    model = model_override if model_override else (runner.get("model", "") if isinstance(runner, dict) else "")

    if status == "cancelled":
        return AgentResult(status="cancelled", duration_ms=duration, usage=usage, model=model)
    if status == "timeout":
        return AgentResult(status="timeout", duration_ms=duration, error=err, usage=usage, model=model)

    # Success/failure here is meaningless for MCP-based workers — the caller
    # (core.worker) judges the outcome by whether the agent-MCP session was
    # closed via a real tool call, not by process exit code.
    return AgentResult(
        status="completed", duration_ms=duration,
        raw_stdout=stdout_str, raw_stderr=stderr_str,
        usage=usage, model=model,
    )
