"""Scoped worker sessions — short-lived tokens binding an agent-MCP connection
to one (task, role).

In-memory on purpose: a session only matters while its worker subprocess runs,
inside the same daemon process that both mints and resolves it. Durable state
lives on the task thread; if the daemon restarts, its workers die with it and
the liveness check re-drives the task from the thread (no session to persist).
"""
import secrets
from datetime import datetime, timezone

_SESSIONS: dict = {}


def mint(task_id: str, role: str, worktree: str = "") -> str:
    token = secrets.token_urlsafe(24)
    _SESSIONS[token] = {
        "token": token,
        "task_id": task_id,
        "role": role,
        "worktree": worktree,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return token


def resolve(token: str):
    """Return the session dict for *token*, or None if unknown/revoked."""
    return _SESSIONS.get(token)


def revoke(token: str) -> None:
    _SESSIONS.pop(token, None)


def revoke_task(task_id: str) -> None:
    """Revoke every session for a task — used when a task is deleted so a
    still-running worker can't write to a thread that no longer exists."""
    for token in [t for t, s in _SESSIONS.items() if s.get("task_id") == task_id]:
        _SESSIONS.pop(token, None)


def clear() -> None:
    """Test helper — drop all sessions."""
    _SESSIONS.clear()
