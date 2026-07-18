"""Crash recovery — handles orchestrator restart scenarios."""
from datetime import datetime, timezone
from .db import get_conn

_TERMINAL_STEPS = {"completed", "failed", "timeout", "cancelled", "error"}


def recover_orphaned_steps():
    """On startup: mark orphaned 'running' steps as 'error', then sync parent run/card status.

    Only touches steps whose run was started more than 2 minutes ago to avoid
    race conditions with active pipelines sharing the same SQLite database.
    """
    conn = get_conn()
    now = datetime.now(timezone.utc).isoformat()

    cursor = conn.execute("""
        SELECT s.id FROM steps s
        WHERE s.status = 'running'
        AND (s.started_at IS NULL OR datetime(s.started_at) < datetime('now', '-2 minutes'))
    """)
    orphaned_steps = cursor.fetchall()
    for row in orphaned_steps:
        conn.execute(
            "UPDATE steps SET status = 'error', error = 'orchestrator_crash', completed_at = ? WHERE id = ?",
            (now, row["id"]),
        )

    runs_to_sync = conn.execute("""
        SELECT r.id, r.status FROM runs r
        WHERE r.status IN ('running', 'pending')
    """).fetchall()

    for run_row in runs_to_sync:
        run_id = run_row["id"]
        steps = conn.execute(
            "SELECT status FROM steps WHERE run_id = ?", (run_id,)
        ).fetchall()

        if not steps or not all(s["status"] in _TERMINAL_STEPS for s in steps):
            continue

        has_failed = any(s["status"] in ("failed", "error", "timeout") for s in steps)
        has_cancelled = any(s["status"] == "cancelled" for s in steps)

        if has_cancelled:
            run_status, run_error = "failed", "cancelled"
        elif has_failed:
            run_status, run_error = "failed", "orchestrator_crash"
        else:
            run_status, run_error = "completed", ""

        conn.execute(
            "UPDATE runs SET status = ?, finished_at = ?, error = ? WHERE id = ?",
            (run_status, now, run_error, run_id),
        )

    cards_to_sync = conn.execute("""
        SELECT c.id, c.status FROM cards c
        WHERE c.status IN ('backlog', 'created', 'running')
    """).fetchall()

    for card_row in cards_to_sync:
        card_id = card_row["id"]
        runs = conn.execute(
            "SELECT status FROM runs WHERE card_id = ?", (card_id,)
        ).fetchall()

        if not runs:
            continue

        non_terminal = [r for r in runs if r["status"] not in ("completed", "failed")]
        if non_terminal:
            continue

        card_status = "failed" if any(r["status"] == "failed" for r in runs) else "completed"

        conn.execute(
            "UPDATE cards SET status = ?, updated_at = ? WHERE id = ?",
            (card_status, now, card_id),
        )

    conn.commit()
    conn.close()
    return len(orphaned_steps)
