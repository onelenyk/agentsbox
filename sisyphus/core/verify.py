"""Run a repo's own check command against a merge request — machine-verified.

The review gate is the only thing between an agent and a target branch, and
today an approval rests on numbers the reviewing agent typed itself. That is not
hypothetical: docs/known-gaps.md records a reviewer approving a clean branch on
the strength of dismissing three test failures that did not exist. The persona
makes lying harder; nothing makes it impossible, because nothing else ever ran
the tests.

This runs them. The command comes from the repo's own configuration, executes in
a throwaway worktree on the MR's source branch, and its exit code and output are
recorded on the MR. A reviewer then cites a number the daemon produced.

Configure per repo in config.yaml:

    repos:
      - name: myrepo
        source: /path/to/repo
        check: python3 -m pytest -q      # omitted → nothing runs, nothing claimed

Deliberately not run through an agent: the point is a result no agent authored.
"""
import logging
import os
import shutil
import subprocess
import tempfile
import time
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 900          # 15 minutes; a suite slower than this needs its own runner
OUTPUT_TAIL = 8000             # what gets stored — the end is where the verdict is


def check_command(repo_name: str) -> Optional[str]:
    """The repo's configured check command, or None when it has none."""
    from core.repos import get_repo
    repo = get_repo(repo_name) or {}
    cmd = (repo.get("check") or "").strip()
    return cmd or None


def run_check(mr: dict, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Run the repo's check command against *mr*'s source branch.

    Returns {ran, ok, exit_code, command, output, duration_ms, reason}. ``ran``
    is False when the repo configures no command — which is reported plainly
    rather than being allowed to look like a pass, because "no evidence" and
    "evidence of correctness" are the two things this exists to keep apart.
    """
    started = time.monotonic()
    cmd = check_command(mr.get("repo", ""))
    if not cmd:
        return {"ran": False, "ok": None, "exit_code": None, "command": "",
                "output": "", "duration_ms": 0,
                "reason": f"repo {mr.get('repo','')!r} configures no check command"}

    bare, branch = mr.get("bare_path", ""), mr.get("source_branch", "")
    if not bare or not branch:
        return {"ran": False, "ok": None, "exit_code": None, "command": cmd,
                "output": "", "duration_ms": 0, "reason": "merge request has no branch to check"}

    wt = tempfile.mkdtemp(prefix="sisyphus-check-")
    try:
        # Detached: the branch may be checked out by a worker's worktree, and a
        # check must never contend with running work for it.
        add = subprocess.run(["git", "-C", bare, "worktree", "add", "--detach", wt, branch],
                             capture_output=True, text=True)
        if add.returncode != 0:
            return {"ran": False, "ok": None, "exit_code": None, "command": cmd,
                    "output": add.stderr.strip()[:OUTPUT_TAIL], "duration_ms": _ms(started),
                    "reason": f"could not check out {branch!r}"}

        env = dict(os.environ)
        from core.config import worker_env
        env.update(worker_env() or {})
        from core.runner_detect import path_with_install_dirs
        env["PATH"] = path_with_install_dirs(env.get("PATH", ""))
        # The check reads the repo; it has no business reaching the daemon's DB.
        env["SISYPHUS_DB_READONLY"] = "1"

        try:
            proc = subprocess.run(cmd, shell=True, cwd=wt, env=env,
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"ran": True, "ok": False, "exit_code": None, "command": cmd,
                    "output": f"check exceeded {timeout}s and was killed",
                    "duration_ms": _ms(started), "reason": "timeout"}

        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return {"ran": True, "ok": proc.returncode == 0, "exit_code": proc.returncode,
                "command": cmd, "output": out[-OUTPUT_TAIL:],
                "duration_ms": _ms(started), "reason": ""}
    finally:
        subprocess.run(["git", "-C", bare, "worktree", "remove", "--force", wt],
                       capture_output=True, text=True)
        shutil.rmtree(wt, ignore_errors=True)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def summarise(res: dict) -> str:
    """One line for a thread, phrased so a reviewer cannot read a missing check
    as a passing one."""
    if not res.get("ran"):
        return f"No automated check ran — {res.get('reason', 'not configured')}."
    secs = res.get("duration_ms", 0) / 1000
    verdict = "PASSED" if res.get("ok") else "FAILED"
    return (f"Automated check {verdict} (exit {res.get('exit_code')}, {secs:.0f}s): "
            f"`{res.get('command','')}`")


def run_and_record(mr: dict, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Run the check, store the verdict on the MR, and post it to the origin
    task's thread so it is visible where the review happens."""
    from core import mergerequests, store

    res = run_check(mr, timeout=timeout)
    try:
        mergerequests.set_check_result(
            mr["id"],
            status=("passed" if res.get("ok") else "failed") if res.get("ran") else "not_run",
            output=(res.get("output") or res.get("reason") or "")[:OUTPUT_TAIL],
            command=res.get("command", ""),
        )
    except Exception:  # noqa: BLE001 — a bookkeeping failure must not lose the result
        logger.exception("could not record check result for %s", mr.get("id"))

    task_id = mr.get("task_id", "")
    if task_id:
        tail = (res.get("output") or "").strip().splitlines()[-12:]
        body = summarise(res)
        if tail:
            body += "\n\n" + "\n".join(tail)
        try:
            store.post_message(task_id, author="system", kind="note", text=body)
        except Exception:  # noqa: BLE001
            logger.exception("could not post check result for %s", mr.get("id"))
    return res
