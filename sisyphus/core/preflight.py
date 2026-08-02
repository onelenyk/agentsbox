"""Startup preflight — can the daemon actually run work?

Checks the two things whose absence would otherwise make *every* task fail
silently: the database is reachable, and the default runner's binary resolves on
disk. Logged loudly at startup and exposed on /health so a broken setup is
visible instead of mysterious.
"""
import logging

logger = logging.getLogger(__name__)


def check() -> dict:
    """Return {ok, db, runner, binary, reason}. ok is True only when the daemon
    is able to run a worker."""
    result = {"ok": False, "db": False, "runner": None, "binary": None, "reason": ""}

    try:
        from core.db import get_conn
        c = get_conn()
        c.execute("SELECT 1")
        c.close()
        result["db"] = True
    except Exception as exc:  # noqa: BLE001
        result["reason"] = f"database unreachable: {exc}"
        return result

    try:
        from core.runners import get_default_runner
        r = get_default_runner()
    except Exception:  # noqa: BLE001
        r = None
    if not r:
        result["reason"] = "no enabled default runner configured"
        return result

    result["runner"] = r["name"]
    from core.runner_detect import resolve
    binp = (r.get("binary") or "").strip()
    resolved = resolve(binp)
    result["binary"] = resolved or binp
    if not resolved:
        result["reason"] = f"default runner binary not found: {binp!r}"
        return result

    result["ok"] = True
    return result
