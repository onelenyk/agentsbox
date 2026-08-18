"""Reap worker subprocesses that outlived the daemon that spawned them.

core.sessions records the failure this module exists for: on 2026-08-12 an
opencode subprocess survived a daemon restart, was reparented to PID 1, and
kept running for minutes. Its session token died with the in-memory session
dict, so it could no longer reach the agent-MCP — invisible in the UI, still
spending the API key, and nothing in the daemon could either see it or stop it.

Killing the wrong process is far worse than leaving an orphan running, so a
process is terminated only when ALL of these hold:

  1. we hold a spawn record for that pid, written by core.launcher at the
     moment it created the subprocess — only pids we started are ever
     candidates, there is no scanning of the process table by binary name;
  2. this daemon process does not currently own that pid (see ``_OWNED``),
     i.e. no launcher call is monitoring it right now;
  3. no live session in core.sessions names the same worktree — that is the
     "not backed by a live session" test; after a restart every session is
     gone, which is exactly the case this module was written for;
  4. the live process's argv still hashes to the fingerprint recorded at
     spawn. This is what defeats pid reuse, and it is strong evidence rather
     than a heuristic: a worker's argv carries the whole generated prompt plus
     the run's own worktree path, which nothing else on the machine
     reproduces;
  5. some argv element lives under the run's own worktree/output dir. Point 4
     already implies it, so this is an assertion, not a test — but it has
     teeth in one real degenerate case: a process spawned with no arguments
     fingerprints the hash of the empty string, which every other
     argument-less process would match too.

Anything that cannot be verified is reported at WARNING and left alone: never
killed, and never counted as reaped.

Why a fingerprint on disk and not an environment marker (the obvious design):
on macOS a non-root process cannot read another process's environment. `ps -E`
and the KERN_PROCARGS2 sysctl behind it return argv only — measured on this
machine, a `SISYPHUS_TEST_MARKER=… sleep 30` child of the very shell asking
came back as 29 bytes: argc=2, "/bin/sleep", ["sleep", "30"], no environment
at all. An env marker would be invisible to precisely the code that needs to
read it. argv *is* readable for same-uid processes, and sandbox-exec exec()s
its target (verified: its own `-f profile` flags do not appear in the live
argv), so what the reaper sees is the runner's own command line.

argv[0] is deliberately excluded from the fingerprint: some binaries re-exec
themselves under another path — /usr/bin/python3 shows up live as
…/Python.app/Contents/MacOS/Python — so argv[0] is not stable across the exec
while argv[1:] is exactly what we passed.
"""
import ctypes
import hashlib
import json
import logging
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# Mirrors core.launcher.GRACEFUL_KILL_TIMEOUT: SIGTERM, then SIGKILL.
TERM_GRACE_SECONDS = 5.0
KILL_GRACE_SECONDS = 2.0

# pids this process spawned and is still monitoring. A record whose pid is in
# here belongs to a live launcher call, so it is not an orphan no matter what
# the session table says (the session can be closed by the agent's own
# `finish` tool call seconds before the process finishes exiting).
# Empty on a fresh daemon — which is why a restart makes every record on disk
# a candidate.
_OWNED: set = set()


def records_dir() -> Path:
    """Where spawn records live. Resolved per call, not at import: tests and
    the daemon point WORKSPACE_ROOT at different trees, and core.launcher's
    import-time copy of it has already caused wrong-directory bugs."""
    ws = os.environ.get("WORKSPACE_ROOT", "/workspace")
    return Path(ws) / "worker-pids"


def fingerprint(argv: List[str]) -> str:
    """Hash of argv[1:] — see the module docstring for why argv[0] is out."""
    joined = "\0".join(argv[1:])
    return hashlib.sha256(joined.encode("utf-8", "surrogatepass")).hexdigest()


# ── spawn records ───────────────────────────────────────────────────────

def record(pid: int, argv: List[str], *, worktree: str = "",
           output_dir: str = "", step_id: str = "", runner: str = "") -> None:
    """Note that *pid* is running *argv* on our behalf.

    *argv* must be the command line the process will have AFTER exec — i.e.
    the runner's own argv, not the sandbox-exec wrapper the launcher actually
    spawns, because that wrapper replaces itself with the target.

    Never raises: a bookkeeping failure must not fail a launch that otherwise
    works. It only costs us the ability to reap that one process later, which
    is the status quo this module improves on.
    """
    try:
        _OWNED.add(pid)
        rec = {
            "pid": pid,
            "argv_sha256": fingerprint(argv),
            # No prompt: argv[1] onward is multi-KB of generated instruction
            # text, and the hash is what verification needs. Keep only what a
            # human reading this file needs to recognise the run.
            "binary": argv[0] if argv else "",
            "argc": len(argv),
            "worktree": worktree,
            "output_dir": output_dir,
            "step_id": step_id,
            "runner": runner,
            "daemon_pid": os.getpid(),
            "spawned_at": time.time(),
        }
        d = records_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{pid}.json"
        tmp = d / f"{pid}.json.tmp"
        tmp.write_text(json.dumps(rec, indent=2))
        os.replace(tmp, path)   # atomic: a crash mid-write never leaves half a record
    except Exception:  # noqa: BLE001
        logger.exception("could not record worker pid %s — it will not be reapable", pid)


def release(pid: int, still_running: bool = False) -> None:
    """This daemon is done monitoring *pid*.

    *still_running* keeps the record on disk: the launcher failed to kill the
    process it gave up on, so the next boot should reap it. Dropping it from
    ``_OWNED`` either way means a reap during this daemon's life sees it as
    the leak it is.
    """
    _OWNED.discard(pid)
    if still_running:
        logger.warning("worker pid %s outlived its launcher call — record kept for reaping", pid)
        return
    try:
        (records_dir() / f"{pid}.json").unlink()
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        logger.exception("could not drop worker record for pid %s", pid)


def _load_records() -> List[dict]:
    d = records_dir()
    if not d.is_dir():
        return []
    out = []
    for path in sorted(d.glob("*.json")):
        try:
            rec = json.loads(path.read_text())
            if not isinstance(rec, dict) or not isinstance(rec.get("pid"), int):
                raise ValueError("record has no pid")
            rec["_path"] = str(path)
            out.append(rec)
        except Exception as e:  # noqa: BLE001
            # Unreadable means unusable — it can never identify a process, so
            # keeping it would only reproduce this warning on every boot.
            logger.warning("dropping unreadable worker record %s: %s", path, e)
            try:
                path.unlink()
            except OSError:
                pass
    return out


# ── reading a live process ──────────────────────────────────────────────

def _exists(pid: int) -> bool:
    """Is there a process table entry for this pid? PermissionError means there
    is one and it belongs to someone else — the argv check will refuse it."""
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _zombie(pid: int) -> bool:
    """A process that has exited but has not been waited on by its parent still
    answers kill(pid, 0) and still holds its pid. A true orphan is reparented
    to launchd/init, which reaps it instantly, so this is mostly the daemon's
    own not-yet-collected children — but counting a corpse as alive makes the
    reaper report "SIGKILL survived", which is a lie in the most alarming
    direction possible.

    `ps` rather than a struct-parsing sysctl: the kinfo_proc layout is private
    and version-specific, and this runs at most a handful of times per boot.
    """
    ps = shutil.which("ps") or "/bin/ps"
    try:
        out = subprocess.run([ps, "-o", "state=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
    except Exception:  # noqa: BLE001 — an unreadable state is not evidence of death
        return False
    return out.stdout.strip().upper().startswith("Z")


def _alive(pid: int) -> bool:
    return _exists(pid) and not _zombie(pid)


def _argv_darwin(pid: int) -> Tuple[Optional[List[str]], str]:
    """argv of *pid* from the KERN_PROCARGS2 sysctl — the same source `ps`
    uses, allowed for same-uid processes without root."""
    libc = ctypes.CDLL("libc.dylib", use_errno=True)
    CTL_KERN, KERN_ARGMAX, KERN_PROCARGS2 = 1, 8, 49

    argmax = ctypes.c_int(0)
    size = ctypes.c_size_t(ctypes.sizeof(argmax))
    mib2 = (ctypes.c_int * 2)(CTL_KERN, KERN_ARGMAX)
    if libc.sysctl(mib2, 2, ctypes.byref(argmax), ctypes.byref(size), None, 0) != 0:
        argmax.value = 262144
    buf = ctypes.create_string_buffer(argmax.value)
    size = ctypes.c_size_t(argmax.value)
    mib3 = (ctypes.c_int * 3)(CTL_KERN, KERN_PROCARGS2, pid)
    if libc.sysctl(mib3, 3, buf, ctypes.byref(size), None, 0) != 0:
        return None, f"sysctl KERN_PROCARGS2 failed (errno {ctypes.get_errno()})"

    raw = buf.raw[: size.value]
    if len(raw) < 4:
        return None, "sysctl returned no argument block"
    # Layout: int32 argc | exec path (NUL-terminated) | NUL padding | argv…
    argc = int.from_bytes(raw[:4], sys.byteorder)
    _exec_path, _, rest = raw[4:].partition(b"\0")
    parts = rest.lstrip(b"\0").split(b"\0")
    if argc <= 0 or len(parts) < argc:
        return None, f"argument block held {len(parts)} strings for argc={argc}"
    return [p.decode("utf-8", "surrogateescape") for p in parts[:argc]], ""


def _argv_linux(pid: int) -> Tuple[Optional[List[str]], str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError as e:
        return None, f"/proc/{pid}/cmdline: {e}"
    parts = [p for p in raw.split(b"\0")]
    if parts and parts[-1] == b"":
        parts.pop()
    if not parts:
        return None, "empty /proc cmdline (kernel thread or exiting process)"
    return [p.decode("utf-8", "surrogateescape") for p in parts], ""


# KERN_PROCARGS2 fails transiently: the kernel copies the argument region out
# of the target's address space, and that copy can momentarily fail with EIO on
# a process that has only just started. Measured while writing tests/test_orphans
# on this machine: roughly one read in three of a freshly spawned child returned
# errno 5, and every single retry 50 ms later succeeded. A single attempt would
# therefore file live orphans under "unverified" — left running, for a reason
# that isn't real.
_ARGV_ATTEMPTS = 5
_ARGV_RETRY_PAUSE = 0.05


def read_argv(pid: int) -> Tuple[Optional[List[str]], str]:
    """(argv, "") for a live process, or (None, reason). A reason is never a
    licence to kill — callers must treat it as "unverified"."""
    system = platform.system()
    if system not in ("Darwin", "Linux"):
        return None, f"no way to read another process's argv on {system}"
    reader = _argv_darwin if system == "Darwin" else _argv_linux
    reason = ""
    for attempt in range(_ARGV_ATTEMPTS):
        try:
            argv, reason = reader(pid)
        except Exception as e:  # noqa: BLE001
            argv, reason = None, f"{type(e).__name__}: {e}"
        if argv is not None:
            return argv, ""
        if not _exists(pid):
            return None, "process exited while its argv was being read"
        if attempt + 1 < _ARGV_ATTEMPTS:
            time.sleep(_ARGV_RETRY_PAUSE)
    return None, f"{reason} (after {_ARGV_ATTEMPTS} attempts)"


# ── verdicts ────────────────────────────────────────────────────────────

def _session_worktrees() -> set:
    from . import sessions
    return {os.path.realpath(s["worktree"])
            for s in sessions.list_active() if s.get("worktree")}


def _names_our_paths(argv: List[str], rec: dict) -> bool:
    """Does argv point at this run's own directories? See point 5 in the
    module docstring — the guard that stops an argument-less process from
    matching an argument-less record by the hash of the empty string."""
    ours = [p for p in (rec.get("worktree"), rec.get("output_dir")) if p]
    if not ours:
        return False
    return any(any(p in arg for p in ours) for arg in argv[1:])


def scan() -> List[dict]:
    """Classify every spawn record. Verdicts:

      owned       a launcher call in this process is monitoring it
      session     a live session names its worktree — a running, visible worker
      gone        the process has exited; the record is stale and is deleted
      recycled    the pid is alive but running something else, so our worker
                  exited and the pid was reused; the record is deleted
      unverified  alive, but we could not confirm it is ours — left running
      orphan      ours, alive, nothing backing it: kill it
    """
    live_worktrees = _session_worktrees()
    out = []
    for rec in _load_records():
        pid = rec["pid"]
        verdict, why = None, ""

        if pid in _OWNED:
            verdict = "owned"
        elif not _alive(pid):
            verdict, why = "gone", "process no longer exists"
        elif rec.get("worktree") and os.path.realpath(rec["worktree"]) in live_worktrees:
            verdict, why = "session", "a live session names this worktree"
        else:
            argv, reason = read_argv(pid)
            if argv is None:
                # Re-check: a process that exits between the liveness probe
                # and this read reads as unverifiable, but is merely gone.
                verdict, why = ("gone", "process exited during the check") if not _alive(pid) \
                    else ("unverified", reason)
            elif fingerprint(argv) != rec.get("argv_sha256"):
                verdict, why = "recycled", "pid is alive but its argv is not the one we spawned"
            elif not _names_our_paths(argv, rec):
                verdict, why = "unverified", "argv matched but names none of the run's own paths"
            else:
                verdict, why = "orphan", "no live session and no launcher owns it"

        rec["verdict"], rec["why"] = verdict, why
        out.append(rec)
    return out


def _drop(rec: dict) -> None:
    try:
        Path(rec["_path"]).unlink()
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        logger.exception("could not drop worker record %s", rec.get("_path"))


def _terminate(pid: int, grace: float = TERM_GRACE_SECONDS) -> str:
    """SIGTERM, then SIGKILL. Returns "terminated" / "killed" / "gone" /
    "survived" / "denied" — the last two are failures, not successes.

    Only this one pid is signalled. Killing the process group would also reach
    the runner's own children, but the worker is spawned in the daemon's
    process group, so a group kill here would take the daemon down with it.
    """
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, KILL_GRACE_SECONDS)):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return "gone" if sig == signal.SIGTERM else "terminated"
        except PermissionError:
            return "denied"
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not _alive(pid):
                return "terminated" if sig == signal.SIGTERM else "killed"
            time.sleep(0.2)
    return "survived"


def reap(kill: bool = True) -> dict:
    """Find worker subprocesses no longer backed by a live session and kill
    them. Call at daemon startup, before anything re-drives a task: a
    re-driven task spawns a fresh worker on the same worktree, and two runners
    writing one git worktree is worse than the orphan alone.

    Returns a summary dict; ``unverified`` and ``failed`` are the honest
    "we could not determine" buckets and are never folded into ``killed``.
    """
    records = scan()
    summary = {"records": len(records), "killed": [], "failed": [],
               "unverified": [], "owned": 0, "session": 0, "stale": 0}
    if not records:
        logger.debug("orphan reaper: no worker spawn records under %s", records_dir())
        return summary

    for rec in records:
        pid, verdict = rec["pid"], rec["verdict"]
        label = f"pid {pid} ({rec.get('step_id') or rec.get('binary') or '?'})"

        if verdict == "owned":
            summary["owned"] += 1
        elif verdict == "session":
            summary["session"] += 1
        elif verdict in ("gone", "recycled"):
            summary["stale"] += 1
            logger.debug("orphan reaper: dropping stale record for %s — %s", label, rec["why"])
            _drop(rec)
        elif verdict == "unverified":
            # Loud on purpose: an unverifiable process is one we may be paying
            # for and cannot stop. Silence here would read as "all clean".
            summary["unverified"].append(pid)
            logger.warning(
                "orphan reaper: %s is alive but NOT confirmed ours (%s) — left running; "
                "check it by hand (spawned %s by daemon pid %s for %s)",
                label, rec["why"], rec.get("spawned_at"), rec.get("daemon_pid"),
                rec.get("worktree") or "?",
            )
        elif verdict == "orphan":
            if not kill:
                summary["unverified"].append(pid)
                logger.warning("orphan reaper: %s is an orphan (dry run — not killed)", label)
                continue
            outcome = _terminate(pid)
            if outcome in ("terminated", "killed", "gone"):
                summary["killed"].append(pid)
                logger.warning(
                    "orphan reaper: %s %s — orphaned worker from daemon pid %s "
                    "(runner=%s worktree=%s), no session could reach it",
                    label, outcome, rec.get("daemon_pid"), rec.get("runner") or "?",
                    rec.get("worktree") or "?",
                )
                _drop(rec)
            else:
                summary["failed"].append(pid)
                logger.error("orphan reaper: %s could not be stopped (%s) — still running",
                             label, outcome)

    logger.info(
        "orphan reaper: %d record(s) — killed %d, unverified %d, failed %d, "
        "owned %d, session-backed %d, stale %d",
        summary["records"], len(summary["killed"]), len(summary["unverified"]),
        len(summary["failed"]), summary["owned"], summary["session"], summary["stale"],
    )
    return summary
