"""Local Merge Requests — a first-class, mergeable change for a local repo.

A forge (GitHub/GitLab) hosts merge requests for us. A local repo has none, so
Sisyphus is the forge: when an engineer opens a change on a local repo, we record
a Merge Request with a lifecycle (open → merged | closed) and real actions —
choose a target branch, check mergeability, land the branch, or merge it — that
operate on the user's actual repo, on demand and reversibly.

The engineer's work already lives on `source_branch` (sisyphus/<task_id>) in the
per-task bare clone (`bare_path`); `source_path` is the user's real repo that the
Land/Merge actions touch.
"""
from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4

from .db import get_conn

MR_STATUSES = ("open", "merged", "closed")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_mr_table() -> None:
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS merge_requests (
            id            TEXT PRIMARY KEY,
            task_id       TEXT NOT NULL,
            repo          TEXT NOT NULL DEFAULT '',
            source_branch TEXT NOT NULL,
            target_branch TEXT NOT NULL,
            title         TEXT NOT NULL DEFAULT '',
            description   TEXT NOT NULL DEFAULT '',
            status        TEXT NOT NULL DEFAULT 'open',
            bare_path     TEXT NOT NULL DEFAULT '',
            base_branch   TEXT NOT NULL DEFAULT '',
            source_path   TEXT NOT NULL DEFAULT '',
            merge_sha     TEXT,
            merged_at     TEXT,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mr_task ON merge_requests(task_id)")
    conn.commit()
    conn.close()


def _dict(row) -> Optional[dict]:
    return dict(row) if row is not None else None


def create_mr(task_id: str, repo: str, source_branch: str, target_branch: str,
              title: str = "", description: str = "", bare_path: str = "",
              base_branch: str = "", source_path: str = "") -> dict:
    mr_id = f"mr-{uuid4().hex[:8]}"
    now = _now()
    conn = get_conn()
    conn.execute(
        """INSERT INTO merge_requests (id, task_id, repo, source_branch, target_branch,
           title, description, status, bare_path, base_branch, source_path,
           created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)""",
        (mr_id, task_id, repo, source_branch, target_branch, title, description,
         bare_path, base_branch, source_path, now, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM merge_requests WHERE id = ?", (mr_id,)).fetchone()
    conn.close()
    return _dict(row)


def get_mr(mr_id: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM merge_requests WHERE id = ?", (mr_id,)).fetchone()
    conn.close()
    return _dict(row)


def latest_for_task(task_id: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM merge_requests WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
        (task_id,)).fetchone()
    conn.close()
    return _dict(row)


def list_mrs(status: Optional[str] = None) -> List[dict]:
    conn = get_conn()
    if status:
        rows = conn.execute(
            "SELECT * FROM merge_requests WHERE status = ? ORDER BY created_at DESC", (status,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM merge_requests ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_target(mr_id: str, target_branch: str) -> Optional[dict]:
    conn = get_conn()
    conn.execute("UPDATE merge_requests SET target_branch = ?, updated_at = ? WHERE id = ?",
                 (target_branch, _now(), mr_id))
    conn.commit()
    conn.close()
    return get_mr(mr_id)


def set_status(mr_id: str, status: str) -> None:
    if status not in MR_STATUSES:
        raise ValueError(f"invalid MR status {status!r}; must be one of {MR_STATUSES}")
    conn = get_conn()
    conn.execute("UPDATE merge_requests SET status = ?, updated_at = ? WHERE id = ?",
                 (status, _now(), mr_id))
    conn.commit()
    conn.close()


def mark_merged(mr_id: str, merge_sha: str) -> None:
    now = _now()
    conn = get_conn()
    conn.execute(
        "UPDATE merge_requests SET status = 'merged', merge_sha = ?, merged_at = ?, updated_at = ? WHERE id = ?",
        (merge_sha, now, now, mr_id))
    conn.commit()
    conn.close()
