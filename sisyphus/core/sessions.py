"""Scoped worker sessions — short-lived tokens binding an agent-MCP connection
to one (task, role).

In-memory on purpose: a session only matters while its worker subprocess runs,
inside the same daemon process that both mints and resolves it. Durable state
lives on the task thread, and reconcile_on_boot re-drives an interrupted task
from that thread, so there is nothing to persist here.

Correction (2026-08-12): a worker does NOT reliably die with the daemon. An
opencode subprocess survived a restart, was reparented to PID 1, and kept
running for minutes. It can no longer reach the agent-MCP (its token died with
this dict), so it is invisible in the UI while still spending the API key. The
absence of a session is therefore a useful signal in the other direction too:
a task whose thread says a worker is in flight, with no session backing it, has
a worker that will never report — see orchestrator.find_stalled.

That signal is also what core.orphans acts on: it holds a spawn record per
worker pid on disk (this dict cannot survive the restart, so the evidence
cannot live here) and, at boot, kills every recorded process that no live
session accounts for. list_active() is the "backed by a live session" side of
that test — its ``worktree`` is what a record is matched against.
"""
import secrets
from datetime import datetime, timezone

_SESSIONS: dict = {}


def mint(task_id: str, role: str, worktree: str = "", instruction: str = "",
         model: str = "", runner: str = "", kind: str = "worker") -> str:
    token = secrets.token_urlsafe(24)
    _SESSIONS[token] = {
        "token": token,
        "task_id": task_id,
        "role": role,
        "worktree": worktree,
        "instruction": instruction,
        "model": model,
        "runner": runner,
        "kind": kind,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return token


def note_output(task_id: str) -> None:
    """The runner for *task_id* just produced something on stdout/stderr.

    This is the only signal that separates a worker which is thinking from one
    that has died. The thread stays empty either way — a run posts to it only
    when the agent chooses to call `comment` — so "13 minutes and no message"
    reads identically for a run doing careful work and a run that is already a
    corpse. The daemon has always known the difference (it stamps the same
    activity to enforce the idle timeout); it simply never told anyone.
    """
    now = datetime.now(timezone.utc).isoformat()
    for s in _SESSIONS.values():
        if s.get("task_id") == task_id:
            s["last_output_at"] = now


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


def _redact(session: dict) -> dict:
    """Return a copy of *session* without the token key."""
    return {k: v for k, v in session.items() if k != "token"}


def list_active() -> list:
    """All sessions sorted by created_at ascending, each WITHOUT the token."""
    return sorted((_redact(s) for s in _SESSIONS.values()),
                  key=lambda s: s.get("created_at", ""))


def running(now=None) -> list:
    """The live worker runs, as plain data: {task_id, role, elapsed_mins}.

    Exists so a caller outside the web layer can ask "is anyone working right
    now" — the CLI's `daemon stop`/`restart` used to kill running workers
    without a word, because this registry lives in the daemon's memory and the
    CLI is a different process. It now asks over /api/v1/health.
    """
    from datetime import datetime, timezone

    now = now or datetime.now(timezone.utc)
    out = []
    for s in list_active():
        try:
            mins = int((now - datetime.fromisoformat(s["created_at"])).total_seconds() // 60)
        except Exception:  # noqa: BLE001 — an unreadable stamp is not a reason to hide the run
            mins = 0
        out.append({"task_id": s.get("task_id", ""), "role": s.get("role", ""),
                    "elapsed_mins": mins,
                    "last_output_at": s.get("last_output_at", "")})
    return out


def list_for_task(task_id: str) -> list:
    """Same as list_active(), filtered to one task."""
    return [s for s in list_active() if s.get("task_id") == task_id]


def clear() -> None:
    """Test helper — drop all sessions."""
    _SESSIONS.clear()
