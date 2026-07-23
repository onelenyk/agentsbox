"""Agent launcher — spawns opencode subprocess with timeout, cancel, validation."""
import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from .resources import save_agent_log
from .schemas import BaseResult

logger = logging.getLogger(__name__)

OUTPUT_SIZE_CAP = 10 * 1024 * 1024
GRACEFUL_KILL_TIMEOUT = 5


@dataclass
class AgentResult:
    status: str
    summary: str = ""
    verdict: Optional[str] = None
    risk: Optional[str] = None
    error: Optional[str] = None
    duration_ms: float = 0
    parsed_result: Optional[dict] = None
    raw_stdout: str = ""
    raw_stderr: str = ""


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
    timeout: Optional[int] = 300,
    idle_timeout: Optional[int] = None,
    cancel_event: Optional[asyncio.Event] = None,
    runner_name: Optional[str] = None,
    step_id: str = "",
) -> AgentResult:
    """Spawn AI runner subprocess, capture output, validate result.

    Handles total timeout and idle timeout (no output for N seconds) via
    SIGTERM → 5s → SIGKILL, cancellation via Event, output capping at 10MB,
    and schema validation against BaseResult.

    Runner selection: if *runner_name* is given, looks up that runner.
    Otherwise falls back to agent_def's "runner" field, then default runner,
    then hardcoded "opencode" as last resort.
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

    binary = runner["binary"]
    extra_args = json.loads(runner.get("extra_args", "[]") or "[]")

    start_time = time.monotonic()
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    env = {k: v for k, v in os.environ.items() if k != "WORKSPACE_ROOT"}
    env["SISYPHUS_OUTPUT_DIR"] = str(output_dir)
    env["SISYPHUS_WORKTREE"] = str(worktree_path)
    env["SISYPHUS_DB_READONLY"] = "1"
    env["SISYPHUS_AGENT_TOOL"] = str(
        Path(__file__).resolve().parent.parent / "tools" / "agent_cli.py"
    )
    if runner.get("model"):
        env["SISYPHUS_MODEL"] = runner["model"]

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
    if worktree_path and worktree_path != "/tmp":
        args.extend(["--dir", str(worktree_path)])

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
        except Exception:
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
    except Exception:
        pass

    if status == "cancelled":
        return AgentResult(status="cancelled", duration_ms=duration)
    if status == "timeout":
        return AgentResult(status="timeout", duration_ms=duration, error=err)

    result_json_path = output_path / "result.json"

    # The agent MUST produce result.json (via the finish tool) — no result
    # means a failed step. Statuses are never invented from the exit code.
    if not result_json_path.exists():
        return AgentResult(
            status="failed", duration_ms=duration,
            error=(
                f"agent_no_result: agent exited with code {proc.returncode} "
                f'without writing result.json — it must call "$SISYPHUS_AGENT_TOOL" finish'
            ),
            raw_stdout=stdout_str, raw_stderr=stderr_str,
        )

    try:
        raw_json = result_json_path.read_text(encoding="utf-8")
        parsed = json.loads(raw_json)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return AgentResult(
            status="failed", duration_ms=duration,
            error=f"schema_validation_failed: invalid JSON: {e}",
            raw_stdout=stdout_str, raw_stderr=stderr_str,
        )
    try:
        result = BaseResult(**parsed)
    except ValidationError as e:
        return AgentResult(
            status="failed", duration_ms=duration,
            error=f"schema_validation_failed: {e}",
            raw_stdout=stdout_str, raw_stderr=stderr_str,
        )

    if proc.returncode is not None and proc.returncode != 0:
        logger.warning(
            "Agent exited with code %s but result.json was valid — trusting result.json",
            proc.returncode,
        )

    return AgentResult(
        status=result.status,
        summary=result.summary,
        verdict=result.verdict,
        risk=result.risk,
        error=result.error,
        duration_ms=duration,
        parsed_result=result.model_dump(),
        raw_stdout=stdout_str,
        raw_stderr=stderr_str,
    )
