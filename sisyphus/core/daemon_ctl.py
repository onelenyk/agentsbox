"""Control the Sisyphus daemon — manual (spawn/kill uvicorn) or supervised
(launchd LaunchAgent). Backs the ``sisyphus daemon`` CLI.

Supervised mode renders ``deploy/com.sisyphus.daemon.plist.template`` with this
repo's path + the current interpreter and loads it, so the daemon starts at
login and is restarted on crash. Secrets come from ``.env`` (loaded by the app),
never the plist.
"""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

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
    except Exception:
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


def stop() -> dict:
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


def restart() -> dict:
    stop()
    for _ in range(25):
        if not is_running():
            break
        time.sleep(0.2)
    return start()


def status() -> dict:
    h = health()
    return {
        "running": h is not None,
        "version": h.get("version") if h else None,
        "supervised": is_installed(),
        "url": HEALTH_URL,
    }
