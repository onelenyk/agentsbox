import json
from datetime import datetime, timezone
from typing import Optional, List
from uuid import uuid4

from .db import get_conn

# FSM: valid transitions for step status
_VALID_TRANSITIONS = {
    "pending": {"running"},
    "running": {"completed", "failed", "timeout", "cancelled", "error"},
    "completed": set(),
    "failed": set(),
    "timeout": set(),
    "cancelled": set(),
    "error": {"running"},  # retry from error allowed
}

_TERMINAL_STATES = {"completed", "failed", "timeout", "cancelled"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dict_from_row(row) -> dict:
    return dict(row)


# ── Card CRUD ─────────────────────────────────────────────────────────


def create_card(
    pipeline_name: str,
    platforms: list,
    description: str,
    jira_url: Optional[str] = None,
) -> dict:
    card_id = f"card-{uuid4().hex[:8]}"
    now = _now()
    platforms_json = json.dumps(platforms)
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO cards (id, pipeline_name, status, platforms, description, jira_url, created_at, updated_at)
        VALUES (?, ?, 'backlog', ?, ?, ?, ?, ?)
        """,
        (card_id, pipeline_name, platforms_json, description, jira_url, now, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    conn.close()
    return _dict_from_row(row)


def get_card(card_id: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    conn.close()
    return _dict_from_row(row) if row else None


def list_cards(status_filter: Optional[str] = None) -> List[dict]:
    conn = get_conn()
    if status_filter:
        rows = conn.execute(
            "SELECT * FROM cards WHERE status = ? ORDER BY created_at DESC",
            (status_filter,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM cards ORDER BY created_at DESC"
        ).fetchall()
    conn.close()
    return [_dict_from_row(r) for r in rows]


# ── Run CRUD ──────────────────────────────────────────────────────────


def create_run(card_id: str) -> dict:
    run_id = f"run-{uuid4().hex[:8]}"
    now = _now()
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO runs (id, card_id, status, loop_count, started_at)
        VALUES (?, ?, 'running', 0, ?)
        """,
        (run_id, card_id, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    conn.close()
    return _dict_from_row(row)


def get_run(run_id: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    conn.close()
    return _dict_from_row(row) if row else None


# ── Step CRUD ─────────────────────────────────────────────────────────


def create_step(run_id: str, agent_name: str, step_order: int) -> dict:
    step_id = f"step-{uuid4().hex[:8]}"
    now = _now()
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO steps (id, run_id, agent_name, step_order, status, loop_count)
        VALUES (?, ?, ?, ?, 'pending', 0)
        """,
        (step_id, run_id, agent_name, step_order),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM steps WHERE id = ?", (step_id,)).fetchone()
    conn.close()
    return _dict_from_row(row)


def update_step_status(
    step_id: str,
    status: str,
    result_json: Optional[str] = None,
    error: Optional[str] = None,
) -> dict:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM steps WHERE id = ?", (step_id,)).fetchone()
        if not row:
            raise ValueError(f"Step {step_id} not found")

        current_status = row["status"]
        allowed = _VALID_TRANSITIONS.get(current_status, set())
        if status not in allowed:
            raise ValueError(
                f"Cannot transition step {step_id} from '{current_status}' to '{status}'"
            )

        now = _now()
        updates = {"status": status}

        if status == "running":
            updates["started_at"] = now
        elif status in _TERMINAL_STATES or status == "error":
            updates["completed_at"] = now

        if result_json is not None:
            updates["result_json"] = result_json
        if error is not None:
            updates["error"] = error

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [step_id]

        conn.execute(f"UPDATE steps SET {set_clause} WHERE id = ?", values)
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM steps WHERE id = ?", (step_id,)
        ).fetchone()
        return _dict_from_row(updated)
    finally:
        conn.close()


def get_step(step_id: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM steps WHERE id = ?", (step_id,)).fetchone()
    conn.close()
    return _dict_from_row(row) if row else None


def get_steps(run_id: str) -> List[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM steps WHERE run_id = ? ORDER BY step_order ASC", (run_id,)
    ).fetchall()
    conn.close()
    return [_dict_from_row(r) for r in rows]


def update_card_status(card_id: str, status: str):
    conn = get_conn()
    now = _now()
    conn.execute("UPDATE cards SET status = ?, updated_at = ? WHERE id = ?", (status, now, card_id))
    conn.commit()
    conn.close()

def update_run_status(run_id: str, status: str, error: str = None):
    conn = get_conn()
    now = _now()
    if status in ("completed", "failed"):
        conn.execute("UPDATE runs SET status = ?, finished_at = ?, error = ? WHERE id = ?", (status, now, error or "", run_id))
    else:
        conn.execute("UPDATE runs SET status = ?, error = ? WHERE id = ?", (status, error or "", run_id))
    conn.commit()
    conn.close()


# ── Resource CRUD ─────────────────────────────────────────────────────


def register_resource(
    run_id: str,
    step_id: str,
    filename: str,
    rtype: str = "agent_output",
    step_order: int = 0,
) -> dict:
    now = _now()
    conn = get_conn()
    cur = conn.execute(
        """
        INSERT INTO resources (run_id, step_id, filename, resource_type, step_order, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (run_id, step_id, filename, rtype, step_order, now),
    )
    resource_id = cur.lastrowid
    conn.commit()
    row = conn.execute(
        "SELECT * FROM resources WHERE id = ?", (resource_id,)
    ).fetchone()
    conn.close()
    return _dict_from_row(row)


def get_resources_for_run(
    run_id: str,
    max_step_order: Optional[int] = None,
    exclude_step_id: Optional[str] = None,
) -> List[dict]:
    conn = get_conn()
    clauses = ["run_id = ?"]
    params: list = [run_id]

    if max_step_order is not None:
        clauses.append("step_order <= ?")
        params.append(max_step_order)
    if exclude_step_id is not None:
        clauses.append("step_id != ?")
        params.append(exclude_step_id)

    where = " AND ".join(clauses)
    rows = conn.execute(
        f"SELECT * FROM resources WHERE {where} ORDER BY step_order ASC, created_at ASC",
        params,
    ).fetchall()
    conn.close()
    return [_dict_from_row(r) for r in rows]
