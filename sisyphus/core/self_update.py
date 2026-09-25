"""Self-update detection — compare the daemon's startup code against the current checkout.

When a merge completes, the daemon's code is updated on disk but the process continues
running old code. This module detects that situation, caches the result, and enables
a safe restart button that only triggers when no workers are active.
"""
import logging
import pathlib
import subprocess
from typing import Optional
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Cache the last result so /runtime and Home don't shell out on every render
_cached_result: Optional[dict] = None
_cached_at: Optional[datetime] = None
_CACHE_SECONDS = 60  # refresh every minute


def _now() -> datetime:
    return datetime.now(timezone.utc)


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

    try:
        # Get current HEAD commit
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(checkout_path), capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            logger.debug("self-update: git rev-parse failed: %s", r.stderr.strip())
            return {"behind": None, "message": None, "behind_message": None}
        current_commit = r.stdout.strip()
        if not current_commit:
            return {"behind": None, "message": None, "behind_message": None}

        # If we're on the same commit, nothing to do
        if current_commit == start_commit:
            return {"behind": 0, "message": "up to date", "behind_message": None}

        # Count commits between start and HEAD
        r = subprocess.run(
            ["git", "rev-list", "--count", f"{start_commit}..HEAD"],
            cwd=str(checkout_path), capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            logger.debug("self-update: git rev-list failed: %s", r.stderr.strip())
            return {"behind": None, "message": None, "behind_message": None}
        try:
            behind_count = int(r.stdout.strip())
        except (ValueError, TypeError):
            return {"behind": None, "message": None, "behind_message": None}

        if behind_count <= 0:
            # start_commit exists but is not an ancestor of HEAD (diverged or newer)
            return {"behind": None, "message": None, "behind_message": None}

        # Get current branch name
        r = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=str(checkout_path), capture_output=True, text=True, timeout=5,
        )
        branch = r.stdout.strip() if r.returncode == 0 else "HEAD"

        message = f"Running {start_commit} — {branch} is {current_commit}, {behind_count} commits behind"
        return {
            "behind": behind_count,
            "behind_count": behind_count,
            "current_commit": current_commit,
            "branch": branch,
            "message": message,
            "behind_message": message,
        }

    except Exception as e:
        logger.debug("self-update: unexpected error: %s", e)
        return {"behind": None, "message": None, "behind_message": None}


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
    """Test helper — clear the cached result."""
    global _cached_result, _cached_at
    _cached_result = None
    _cached_at = None
