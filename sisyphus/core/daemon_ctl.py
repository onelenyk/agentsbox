"""Control the Sisyphus daemon — manual (spawn/kill uvicorn) or supervised
(launchd LaunchAgent). Backs the ``sisyphus daemon`` CLI.

Supervised mode renders ``deploy/com.sisyphus.daemon.plist.template`` with this
repo's path + the current interpreter and loads it, so the daemon starts at
login and is restarted on crash. Secrets come from ``.env`` (loaded by the app),
never the plist.
"""
import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

PORT = 7677
HEALTH_URL = f"http://127.0.0.1:{PORT}/api/v1/health"
LABEL = "com.sisyphus.daemon"

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "deploy" / "com.sisyphus.daemon.plist.template"
PLIST_DEST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_DIR = REPO_ROOT / "logs"


def health():
    """Return the daemon's /health JSON, or None if unreachable."""
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=2) as r:
            return json.loads(r.read())
    except Exception as e:
        logger.debug("daemon health check failed: %s", e)
        return None


def is_running() -> bool:
    return health() is not None


def is_installed() -> bool:
    return PLIST_DEST.exists()


def render_plist(python=None, workdir=None, logdir=None) -> str:
    return (
        TEMPLATE.read_text()
        .replace("__PYTHON__", str(python or sys.executable))
        .replace("__WORKDIR__", str(workdir or REPO_ROOT))
        .replace("__LOGDIR__", str(logdir or LOG_DIR))
    )


def _launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _pids_on_port():
    r = subprocess.run(["lsof", "-ti", f":{PORT}"], capture_output=True, text=True)
    return [int(x) for x in r.stdout.split() if x.strip()]


def _port_listen_pid():
    """Return the PID that actually owns the TCP LISTEN port, or None."""
    r = subprocess.run(
        ["lsof", "-tnP", f"-iTCP:{PORT}", "-sTCP:LISTEN"],
        capture_output=True, text=True,
    )
    pids = [int(x) for x in r.stdout.split() if x.strip()]
    return pids[0] if pids else None


def _managed_pid():
    """Return the PID daemon_ctl believes it manages via launchd, or None."""
    if not is_installed():
        return None
    r = subprocess.run(["launchctl", "list", LABEL], capture_output=True, text=True)
    output = r.stdout.strip()
    if not output:
        return None
    if not output.startswith("{"):
        parts = output.split()
        if parts and parts[0] != "-" and parts[0].isdigit():
            return int(parts[0])
        return None
    for line in output.splitlines():
        line = line.strip()
        if '"PID"' in line:
            val = line.split("=")[-1].strip().rstrip(";")
            try:
                return int(val)
            except ValueError:
                return None
    return None


def _foreign_pid_on_port():
    """Return PID if port is owned by an unmanaged (foreign) process, else None."""
    managed = _managed_pid()
    port_pid = _port_listen_pid()
    if port_pid is None:
        return None
    if managed is None:
        return None
    if port_pid == managed:
        return None
    return port_pid


def install() -> dict:
    LOG_DIR.mkdir(exist_ok=True)
    PLIST_DEST.parent.mkdir(parents=True, exist_ok=True)
    PLIST_DEST.write_text(render_plist())
    _launchctl("unload", str(PLIST_DEST))  # idempotent: clear any prior load
    r = _launchctl("load", "-w", str(PLIST_DEST))
    return {"plist": str(PLIST_DEST), "ok": r.returncode == 0, "err": r.stderr.strip()}


def uninstall() -> dict:
    if PLIST_DEST.exists():
        _launchctl("unload", "-w", str(PLIST_DEST))
        PLIST_DEST.unlink()
        return {"removed": True}
    return {"removed": False}


def start() -> dict:
    # Refuse whenever the port is held by a process we do not manage — even
    # when launchd has no record (managed None): that is exactly the stray-
    # daemon case, and binding would fail anyway.
    port_pid = _port_listen_pid()
    if port_pid is not None and port_pid != _managed_pid():
        msg = f"Port {PORT} is already in use by PID {port_pid} (not managed by daemon_ctl). Kill it first: kill {port_pid}"
        print(msg, file=sys.stderr)
        return {"error": msg, "foreign_pid": port_pid}
    if is_running():
        return {"already_running": True}
    if is_installed():
        _launchctl("load", "-w", str(PLIST_DEST))
        return {"via": "launchd"}
    LOG_DIR.mkdir(exist_ok=True)
    logf = open(LOG_DIR / "daemon.err.log", "a")
    subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "web.app:app",
         "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(REPO_ROOT), stdout=logf, stderr=logf, start_new_session=True,
    )
    return {"via": "manual"}


def running_workers() -> list:
    """Worker runs in flight right now, from the daemon's own health endpoint.

    The session registry lives in the daemon's memory and this process is not
    the daemon, so asking over HTTP is the only honest answer. A daemon that is
    down or too old to report has no workers we could name — an empty list, and
    the caller must not read that as proof that nothing is running.
    """
    h = health()
    return list((h or {}).get("workers") or [])


def describe_workers(workers: list) -> str:
    """One line per run, for a human about to kill them."""
    return "\n".join(f"  {w.get('role') or 'worker'} on {w.get('task_id')} "
                      f"({w.get('elapsed_mins', 0)}m in)" for w in workers)


def stop(force: bool = False) -> dict:
    """Stop the daemon. Refuses while a worker is mid-run unless *force*.

    Killing the daemon kills its workers: the run dies, its worktree is left
    behind, and the task only recovers on the next boot's reconcile — which
    costs the whole run again. That happened here on a live task and nothing
    warned anybody, so the guard is a refusal, not a message printed after the
    fact.
    """
    busy = running_workers()
    if busy and not force:
        return {"blocked": busy}
    if is_installed():
        # Unload stops it and disables KeepAlive respawn; RunAtLoad brings it
        # back at next login (or `daemon start`).
        _launchctl("unload", str(PLIST_DEST))
        return {"via": "launchd"}
    killed = _pids_on_port()
    for pid in killed:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    return {"via": "manual", "killed": killed}


def restart(force: bool = False) -> dict:
    busy = running_workers()
    if busy and not force:
        return {"blocked": busy}
    stop(force=True)      # already decided above; do not re-ask
    for _ in range(25):
        if not is_running():
            break
        time.sleep(0.2)
    return start()


def status() -> dict:
    h = health()
    managed = _managed_pid()
    port_pid = _port_listen_pid()
    result = {
        "running": h is not None,
        "version": h.get("version") if h else None,
        "supervised": is_installed(),
        "url": HEALTH_URL,
    }
    if port_pid is not None and managed is None and result["supervised"]:
        # Installed but launchd shows no PID while something serves the port:
        # an unmanaged (stray) process — the exact incident this guard is for.
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(port_pid)],
                           capture_output=True, text=True)
        print(f"\n  WARNING: port {PORT} is served by PID {port_pid}, but launchd")
        print(f"  reports no managed process. Likely a stray daemon (started {r.stdout.strip()}).")
        print(f"  Kill it and restart: kill {port_pid}\n")
        result["foreign_pid"] = port_pid
    if managed is not None and port_pid is not None and managed != port_pid:
        r = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(port_pid)],
            capture_output=True, text=True,
        )
        start_time = r.stdout.strip()
        print(f"\n  FOREIGN PROCESS on port {PORT}!")
        print(f"  Managed PID (launchd): {managed}")
        print(f"  Actual PID on port:    {port_pid}")
        print(f"  Foreign process started: {start_time}")
        print(f"  Kill it: kill {port_pid}\n")
        result["foreign_pid"] = port_pid
        result["managed_pid"] = managed
        result["foreign_start"] = start_time
    return result
