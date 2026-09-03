"""Runner smoke test — prove a runner can actually do the job, end to end.

core.preflight answers "does the binary resolve on disk". That is the shallow
question, and every runner failure this system has hit passed it:

  * an opencode agent generated without a model hung the CLI forever with no
    output at all — binary resolved fine
  * `claude` resolved to a wrapper script that then could not find the real
    binary under the daemon's launchd PATH — binary resolved fine
  * Claude Code's buffered json output only failed once a run got long enough
    for the idle timeout — binary resolved fine

Each surfaced to the operator as "worker ended without reporting a result", a
sentence that points nowhere. This module answers the question that actually
matters instead: spawn the CLI exactly the way core.launcher does, hand it a
throwaway MCP server, and require the model to call one tool through it. Every
stage that can fail gets a name, so a broken runner reports its cause rather
than its symptom.

Deliberately reuses core.runner_profiles and core.sandbox rather than
reimplementing the launch: a probe that takes a different path than production
proves nothing about production.
"""
import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_SMOKE_SERVER = str(Path(__file__).resolve().parent.parent / "tools" / "smoke_mcp_stdio.py")
_SERVER_NAME = "sisyphus_smoke"

# Long enough for a cold model call on a slow link, short enough that a hung
# CLI is reported rather than waited on. The hang cases this exists to catch
# produce no output at all, so they burn the whole budget.
DEFAULT_TIMEOUT = 120

# Ordered stages; a run reports the first one that failed.
STAGES = ("binary", "spawn", "output", "tool_call")


def _result(ok, stage, detail="", **extra):
    out = {"ok": ok, "stage": stage, "detail": detail}
    out.update(extra)
    return out


async def smoke_test(runner: dict, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Run one probe against *runner*.

    Returns {ok, stage, detail, duration_ms, tool_called}. On failure *stage* is
    the first thing that did not work — "binary", "spawn", "output" or
    "tool_call" — and *detail* carries the CLI's own words where it produced
    any, because that text is what a silent failure otherwise throws away.
    """
    import sys

    from . import runner_profiles, sandbox
    from .runner_detect import path_with_install_dirs, resolve as resolve_binary

    started = time.monotonic()

    try:
        rtype = runner_profiles.normalize_type(runner)
    except runner_profiles.UnsupportedRunner as exc:
        return _result(False, "binary", str(exc), duration_ms=0, tool_called=False)

    binary = resolve_binary(runner.get("binary", "")) or runner.get("binary", "")
    if not binary or not (os.path.isabs(binary) and os.path.exists(binary)):
        if not shutil.which(binary or ""):
            return _result(False, "binary",
                           f"binary {runner.get('binary')!r} not found in PATH or the usual install dirs",
                           duration_ms=0, tool_called=False)

    workdir = tempfile.mkdtemp(prefix="sisyphus-smoke-")
    result_path = os.path.join(workdir, "called.json")
    try:
        prefix = runner_profiles.tool_prefix(rtype, _SERVER_NAME)
        tool = f"{prefix}report"

        mcp_config = runner_profiles.write_mcp_config(
            rtype,
            worktree_path=workdir,
            output_dir=workdir,
            server_name=_SERVER_NAME,
            command=[sys.executable, _SMOKE_SERVER],
            env={"SMOKE_RESULT_PATH": result_path},
            agent_name="sisyphus-smoke",
            # Without a model an opencode agent hangs with no output — the exact
            # failure this probe exists to surface, so pass the runner's own.
            model=runner.get("model", "") or "",
            # The smoke test must run under the same isolation a real worker
            # gets, or it proves a tool call works in a roster no worker sees.
            binary=runner.get("binary", "") or "",
        )

        prompt = (
            f"Call the tool {tool} with status=\"ok\". "
            "Make exactly one tool call and then stop. Do not do anything else."
        )
        argv = runner_profiles.build_argv(
            rtype, binary, json.loads(runner.get("extra_args") or "[]"), prompt,
            worktree_path=workdir,
            agent="sisyphus-smoke",
            model=runner.get("model", "") or "",
            mcp_config=mcp_config,
        )

        env = dict(os.environ)
        from .config import worker_env
        env.update(worker_env() or {})
        env["PATH"] = path_with_install_dirs(env.get("PATH", ""))
        env.update(runner_profiles.build_env(rtype, runner))

        if sandbox.enabled():
            argv = sandbox.wrap(argv, workdir, workdir)

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
                env=env,
            )
        except Exception as exc:  # noqa: BLE001
            return _result(False, "spawn", f"could not start {binary!r}: {exc}",
                           duration_ms=_ms(started), tool_called=False)

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return _result(False, "output",
                           f"produced no result within {timeout}s — the CLI is hanging, "
                           f"not merely slow",
                           duration_ms=_ms(started), tool_called=False)

        out = (stdout or b"").decode("utf-8", errors="replace")
        err = (stderr or b"").decode("utf-8", errors="replace")

        if not out.strip():
            # The signature failure of every silent runner bug so far. stderr is
            # usually the only place the reason exists.
            return _result(False, "output",
                           (err.strip() or "the process exited without writing anything "
                            "to stdout or stderr")[:400],
                           duration_ms=_ms(started), tool_called=False,
                           exit_code=proc.returncode)

        if not os.path.exists(result_path):
            return _result(False, "tool_call",
                           f"the CLI ran but never called {tool} — its MCP server is not "
                           f"reachable, or the tool is not exposed under that name. "
                           f"{(err.strip() or out.strip())[:240]}",
                           duration_ms=_ms(started), tool_called=False,
                           exit_code=proc.returncode)

        return _result(True, "tool_call", f"called {tool}",
                       duration_ms=_ms(started), tool_called=True,
                       exit_code=proc.returncode)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


async def smoke_test_by_name(name: str = "", timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Smoke-test a runner by name, or the default runner when *name* is blank."""
    from .runners import resolve_runner
    runner = resolve_runner(name)
    if not runner:
        return _result(False, "binary", "no runner configured", duration_ms=0, tool_called=False)
    res = await smoke_test(runner, timeout=timeout)
    res["runner"] = runner.get("name", "")
    return res
