"""Self-update detection — compare the daemon's startup code against the current checkout.

When a merge completes, the daemon's code is updated on disk but the process continues
running old code. This module detects that situation, caches the result, and enables
a safe restart button that only triggers when no workers are active.

The restart itself never runs from inside the daemon. A worker mid-run dies with the
daemon, and stopping the process from its own request handler blocks the event loop
uvicorn's graceful shutdown (which pauses tasks with pause_kind "closed") needs. So a
restart is always delegated to a detached subprocess running the same ``daemon_ctl.restart``
path the CLI (``sisyphus.py daemon restart``) uses — the process that stops the daemon
is never the process being stopped.
"""
import asyncio
import logging
import pathlib
import subprocess
import sys
from typing import Optional
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Cache the last result so /runtime and Home don't shell out on every render
_cached_result: Optional[dict] = None
_cached_at: Optional[datetime] = None
_CACHE_SECONDS = 60  # refresh every minute

# A restart requested while a worker was running; the self-update loop performs it
# once the last one finishes. In-memory on purpose: it only means something while
# this process is alive, and after a restart the new daemon runs new code.
_PENDING_RESTART = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _git(args: list, cwd) -> Optional[str]:
    """One cheap git call; None on any failure, never raising."""
    try:
        r = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            logger.debug("self-update: %s failed: %s", args[0], r.stderr.strip())
            return None
        return r.stdout.strip()
    except Exception as e:  # noqa: BLE001 — any git failure means "unknown"
        logger.debug("self-update: %s raised: %s", args[0], e)
        return None


def daemon_repo_root() -> pathlib.Path:
    """The git toplevel of the checkout the daemon runs from — the directory
    git resolves from the source tree (the worktree root holding .git), which
    is where merges actually land. Falls back to the source dir when git is
    unavailable, so the check degrades to "unknown" rather than raising."""
    src = pathlib.Path(__file__).resolve().parent.parent
    toplevel = _git(["git", "-C", str(src), "rev-parse", "--show-toplevel"], src)
    return pathlib.Path(toplevel) if toplevel else src


def is_daemon_checkout(path: str) -> bool:
    """Is *path* (a merged repo) the daemon's own checkout?

    Both sides are resolved to their git toplevel, so a repo registered at the
    worktree root (one level above the source dir) and one registered at the
    source dir both match.
    """
    if not path:
        return False
    try:
        resolved = pathlib.Path(path)
        merged = _git(["git", "-C", str(resolved), "rev-parse", "--show-toplevel"], resolved)
        merged_root = pathlib.Path(merged) if merged else resolved.resolve()
        return merged_root == daemon_repo_root()
    except Exception:  # noqa: BLE001 — a bad path is not the daemon's checkout
        return False


def check_behind(start_commit: str, checkout_root: str) -> dict:
    """Check if the daemon's startup commit is behind the checkout's HEAD.

    Args:
        start_commit: Short SHA the daemon started with (from _GIT_COMMIT)
        checkout_root: Filesystem path to the git checkout the daemon runs from

    Returns:
        dict with keys:
        - behind (int): 0 if up to date, None if unknown (not a git repo, unknown commit, error)
        - behind_count (int): number of commits between start and HEAD (if behind is set)
        - current_commit (str): current HEAD short SHA (if behind is set)
        - branch (str): current branch name (if behind is set)
        - message (str): human-readable status line, or "up to date", or None if unknown
        - behind_message (str): e.g. "Running 21ec7c5 — main is 324884a, 3 merges behind"

    Returns None for beyond, behind_count, current_commit, branch on error.
    """
    if not start_commit or not checkout_root:
        return {"behind": None, "message": None, "behind_message": None}

    checkout_path = pathlib.Path(checkout_root)
    if not checkout_path.is_dir():
        return {"behind": None, "message": None, "behind_message": None}

    current_commit = _git(["git", "rev-parse", "--short", "HEAD"], checkout_path)
    if not current_commit:
        return {"behind": None, "message": None, "behind_message": None}

    # If we're on the same commit, nothing to do
    if current_commit == start_commit:
        return {"behind": 0, "message": "up to date", "behind_message": None}

    # Count commits between start and HEAD
    raw = _git(["git", "rev-list", "--count", f"{start_commit}..HEAD"], checkout_path)
    try:
        behind_count = int(raw) if raw is not None else None
    except (ValueError, TypeError):
        behind_count = None
    if behind_count is None or behind_count <= 0:
        # start_commit exists but is not an ancestor of HEAD (diverged or newer)
        return {"behind": None, "message": None, "behind_message": None}

    branch = _git(["git", "symbolic-ref", "--short", "HEAD"], checkout_path) or "HEAD"

    message = f"Running {start_commit} — {branch} is {current_commit}, {behind_count} commits behind"
    return {
        "behind": behind_count,
        "behind_count": behind_count,
        "current_commit": current_commit,
        "branch": branch,
        "message": message,
        "behind_message": message,
    }


def get_cached_status(start_commit: str, checkout_root: str) -> dict:
    """Get cached behind-status or refresh if cache is stale.

    Returns the same dict as check_behind().
    """
    global _cached_result, _cached_at
    now = _now()
    if (
        _cached_result is not None
        and _cached_at is not None
        and (now - _cached_at).total_seconds() < _CACHE_SECONDS
    ):
        return _cached_result

    _cached_result = check_behind(start_commit, checkout_root)
    _cached_at = now
    return _cached_result


def clear_cache() -> None:
    """Drop the cached result — called after a merge into the daemon's own
    checkout so the next render (or loop pass) sees the new HEAD immediately."""
    global _cached_result, _cached_at
    _cached_result = None
    _cached_at = None


# ── Restart: requested while busy, performed by the loop once idle ─────────

def request_restart() -> None:
    """Record that the operator asked for a restart. The self-update loop
    performs it when the last worker finishes."""
    global _PENDING_RESTART
    _PENDING_RESTART = True


def pending_restart() -> bool:
    return _PENDING_RESTART


def clear_pending_restart() -> None:
    global _PENDING_RESTART
    _PENDING_RESTART = False


def perform_restart() -> dict:
    """Restart the daemon from a detached subprocess, reusing the exact path
    ``sisyphus.py daemon restart`` runs (``daemon_ctl.restart``).

    The subprocess waits a moment before stopping this daemon so the HTTP
    response that triggered the restart has already flushed. It runs in its own
    session, so it survives the daemon it stops, and ``daemon_ctl.restart``
    refuses while a worker is mid-run (a race that appeared since the caller
    checked is not a licence to kill a worker).

    Returns {"spawned": True} when the process was started.
    """
    from core import daemon_ctl
    code = (
        "import sys, time\n"
        "time.sleep(2)\n"
        "from core import daemon_ctl\n"
        "daemon_ctl.restart(force=False)\n"
    )
    log = None
    try:
        daemon_ctl.LOG_DIR.mkdir(exist_ok=True)
        log = open(daemon_ctl.LOG_DIR / "self_update.restart.log", "a")
    except Exception:  # noqa: BLE001 — logging must never block the restart
        pass
    try:
        subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=str(daemon_ctl.REPO_ROOT),
            start_new_session=True,
            stdout=log or subprocess.DEVNULL,
            stderr=log or subprocess.DEVNULL,
        )
        return {"spawned": True}
    except Exception:  # noqa: BLE001
        logger.exception("self-update: could not spawn restart process")
        return {"spawned": False}


async def restart_forever(start_commit: str, checkout_root: str, interval: float = 5.0) -> None:
    """Daemon loop: perform a requested (or auto) restart once no worker runs.

    Checks far more often than it acts: when a worker is running the pass is a
    single cheap in-process look at ``sessions.running()``, and only the moment
    the last worker finishes does it shell out to spawn the restart."""
    from core import heartbeat
    from core.config import get_config
    interval = float(get_config("self_update.check_seconds", 5) or 5)
    heartbeat.register("self-update", every=int(interval),
                       note="restarting when idle after self-update")
    while True:
        try:
            await asyncio.to_thread(_restart_pass, start_commit, checkout_root)
        except Exception:  # noqa: BLE001 — a broken pass must not kill the loop
            logger.exception("self-update: pass failed")
        await asyncio.sleep(interval)


def _restart_pass(start_commit: str, checkout_root: str) -> None:
    """One decision: should a restart happen now? Runs on the thread pool (git
    and config reads are blocking)."""
    from core import sessions, heartbeat
    from core.config import get_config

    status = get_cached_status(start_commit, checkout_root)
    behind = status.get("behind") or 0
    auto = bool(get_config("self_update.auto_restart_when_idle", False))
    if not (_PENDING_RESTART or (auto and behind > 0)):
        return

    busy = sessions.running()
    if busy:
        heartbeat.beat("self-update",
                       note=f"restart pending — {len(busy)} worker(s) running")
        return

    clear_pending_restart()
    spawned = perform_restart().get("spawned")
    heartbeat.beat("self-update",
                   note="restarting daemon (idle)" if spawned else "restart spawn failed")


def reset() -> None:
    """Test helper — clear the cache and any pending restart."""
    clear_cache()
    clear_pending_restart()
