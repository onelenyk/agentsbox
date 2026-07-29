"""Per-run token/cost usage — captured from opencode's JSON output.

opencode `run --format json` streams NDJSON events; each assistant step emits a
`step-finish` event carrying `tokens` (input/output/reasoning/cache) and `cost`.
We sum those across a run and store one row per agent run, so the Statistics
page can report real token spend and $ cost — no estimation.
"""
import json
from datetime import datetime, timezone
from typing import Optional

from .db import get_conn


def init_usage_table() -> None:
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id          TEXT,
            agent            TEXT,
            model            TEXT,
            input_tokens     INTEGER NOT NULL DEFAULT 0,
            output_tokens    INTEGER NOT NULL DEFAULT 0,
            reasoning_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read       INTEGER NOT NULL DEFAULT 0,
            cache_write      INTEGER NOT NULL DEFAULT 0,
            total_tokens     INTEGER NOT NULL DEFAULT 0,
            cost             REAL NOT NULL DEFAULT 0,
            steps            INTEGER NOT NULL DEFAULT 0,
            duration_ms      REAL NOT NULL DEFAULT 0,
            created_at       TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_task ON usage(task_id)")
    conn.commit()
    conn.close()


def parse_usage(stdout: str) -> dict:
    """Sum token/cost across a run's opencode NDJSON output. Returns zeros if the
    output carries no usage (a different runner, or a crash before any step)."""
    agg = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
           "cache_read": 0, "cache_write": 0, "cost": 0.0, "steps": 0}
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line or '"cost"' not in line or '"tokens"' not in line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        # usage sits on a step-finish event, at the root or under "part"
        part = obj.get("part") if isinstance(obj, dict) else None
        node = part if isinstance(part, dict) and "cost" in part else obj
        if not (isinstance(node, dict) and "tokens" in node and "cost" in node):
            continue
        t = node.get("tokens") or {}
        cache = t.get("cache") or {}
        agg["input_tokens"] += int(t.get("input") or 0)
        agg["output_tokens"] += int(t.get("output") or 0)
        agg["reasoning_tokens"] += int(t.get("reasoning") or 0)
        agg["cache_read"] += int(cache.get("read") or 0)
        agg["cache_write"] += int(cache.get("write") or 0)
        agg["cost"] += float(node.get("cost") or 0)
        agg["steps"] += 1
    agg["total_tokens"] = agg["input_tokens"] + agg["output_tokens"] + agg["reasoning_tokens"]
    return agg


def record_run(task_id: str, agent: str, model: str, usage: dict, duration_ms: float = 0) -> None:
    """Persist one agent run's usage. No-op if nothing was reported (steps == 0)
    — don't clutter stats with empty rows from crashed/usage-less runs."""
    if not usage or usage.get("steps", 0) == 0:
        return
    conn = get_conn()
    conn.execute(
        """INSERT INTO usage (task_id, agent, model, input_tokens, output_tokens,
           reasoning_tokens, cache_read, cache_write, total_tokens, cost, steps,
           duration_ms, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (task_id, agent, model, usage["input_tokens"], usage["output_tokens"],
         usage["reasoning_tokens"], usage["cache_read"], usage["cache_write"],
         usage["total_tokens"], usage["cost"], usage["steps"], duration_ms,
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


# ── Aggregations (for the Statistics page) ──────────────────────────────


def totals() -> dict:
    conn = get_conn()
    row = conn.execute(
        """SELECT COALESCE(SUM(cost),0) cost, COALESCE(SUM(total_tokens),0) tokens,
                  COALESCE(SUM(input_tokens),0) input, COALESCE(SUM(output_tokens),0) output,
                  COALESCE(SUM(reasoning_tokens),0) reasoning, COALESCE(SUM(cache_read),0) cache_read,
                  COUNT(*) runs FROM usage"""
    ).fetchone()
    conn.close()
    return dict(row)


def by_agent() -> list:
    conn = get_conn()
    rows = conn.execute(
        """SELECT agent, COUNT(*) runs, COALESCE(SUM(total_tokens),0) tokens,
                  COALESCE(SUM(cost),0) cost
           FROM usage GROUP BY agent ORDER BY cost DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def by_day(days: int = 14) -> list:
    conn = get_conn()
    rows = conn.execute(
        """SELECT substr(created_at,1,10) day, COALESCE(SUM(cost),0) cost,
                  COALESCE(SUM(total_tokens),0) tokens, COUNT(*) runs
           FROM usage GROUP BY day ORDER BY day DESC LIMIT ?""",
        (days,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]
