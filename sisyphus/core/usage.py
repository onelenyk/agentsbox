"""Per-run token/cost usage — captured from opencode's JSON output.

opencode `run --format json` streams NDJSON events; each assistant step emits a
`step-finish` event carrying `tokens` (input/output/reasoning/cache) and `cost`.
We sum those across a run and store one row per agent run, so the Statistics
page can report real token spend and $ cost — no estimation.
"""
import json
from datetime import datetime, timezone
from typing import Optional
import logging

logger = logging.getLogger(__name__)

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


def _find_claude_result(stdout: str) -> Optional[dict]:
    """The terminal ``"type":"result"`` event from Claude Code's output.

    Handles both shapes the CLI can emit: the stream-json event stream the
    worker actually runs (see core.runner_profiles, which uses it so the
    launcher's idle timer keeps ticking), where the result is the last of many
    lines; and a lone json object, which is what `--output-format json`
    produces and what a hand-configured runner might still yield. Returns None
    for anything else, so opencode's NDJSON path can handle it instead.
    """
    last = None
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        # Keep scanning: only the final result event carries whole-run totals,
        # and a resumed/retried run can emit more than one.
        if isinstance(obj, dict) and obj.get("type") == "result":
            last = obj
    return last


def _parse_claude_usage(stdout: str) -> Optional[dict]:
    """Usage from Claude Code's run output, or None if this isn't that runner.

    One cumulative ``usage`` block per run plus ``total_cost_usd`` — unlike
    opencode, which reports per-step and has to be summed.
    """
    obj = _find_claude_result(stdout)
    if obj is None:
        return None
    u = obj.get("usage")
    if not isinstance(u, dict):
        return None
    details = u.get("output_tokens_details") or {}
    # Claude Code reports which model actually served the run. A claude-cli
    # runner row usually names no model (the CLI uses its own configured
    # default), so without this the Statistics page shows a blank model for
    # every Claude run — the one thing that row is for.
    #
    # One run can use several: the main model does the work and a small one
    # handles side jobs. Taking the FIRST key named the side model — a run on
    # this board that Opus did for $0.448 was recorded as Haiku, which spent
    # $0.002 of it. The model that served the run is the one that cost most.
    model_usage = obj.get("modelUsage")
    served = ""
    if isinstance(model_usage, dict) and model_usage:
        served = max(model_usage, key=lambda m: (
            float((model_usage[m] or {}).get("costUSD") or 0),
            int((model_usage[m] or {}).get("outputTokens") or 0)))
    return {
        "model": served,
        "input_tokens": int(u.get("input_tokens") or 0),
        "output_tokens": int(u.get("output_tokens") or 0),
        "reasoning_tokens": int(details.get("thinking_tokens") or 0),
        "cache_read": int(u.get("cache_read_input_tokens") or 0),
        "cache_write": int(u.get("cache_creation_input_tokens") or 0),
        "cost": float(obj.get("total_cost_usd") or 0),
        # One row per run, not per model turn. record_run drops rows with
        # steps == 0, so a run that reported real tokens must count at least 1
        # even when num_turns is absent or zero.
        "steps": max(1, int(obj.get("num_turns") or 0)),
        "total_tokens": (
            int(u.get("input_tokens") or 0)
            + int(u.get("output_tokens") or 0)
            + int(details.get("thinking_tokens") or 0)
        ),
    }


def parse_usage(stdout: str) -> dict:
    """Token/cost for one run, from whichever runner produced *stdout*.

    Returns zeros if the output carries no usage (an unrecognised runner, or a
    crash before any step)."""
    claude = _parse_claude_usage(stdout)
    if claude is not None:
        return claude

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


def roster() -> dict:
    """What each agent has actually done, and who it works with.

    `agents`: per agent — runs, cost, tasks it touched, last run.
    `handoffs`: (from, to) -> how many times, within one task, one worker's
    run was followed by another's. That is the real shape of the team —
    engineer to reviewer, reviewer back to engineer on changes — read from
    what happened rather than drawn from what was intended. The orchestrator
    is left out of it: it sits between every pair.
    """
    conn = get_conn()
    rows = conn.execute("SELECT task_id, agent, cost, created_at FROM usage ORDER BY id").fetchall()
    conn.close()
    agents, per_task = {}, {}
    for r in rows:
        a = agents.setdefault(r["agent"], {"runs": 0, "cost": 0.0, "tasks": set(), "last": ""})
        a["runs"] += 1
        a["cost"] += r["cost"] or 0
        a["tasks"].add(r["task_id"])
        a["last"] = max(a["last"], r["created_at"] or "")
        per_task.setdefault(r["task_id"], []).append(r["agent"])
    handoffs = {}
    for seq in per_task.values():
        workers = [a for a in seq if a != "orchestrator"]
        for x, y in zip(workers, workers[1:]):
            if x != y:
                handoffs[(x, y)] = handoffs.get((x, y), 0) + 1
    for a in agents.values():
        a["tasks"] = len(a["tasks"])
    return {"agents": agents, "handoffs": handoffs}


def task_cost(task_id: str) -> float:
    """Total $ spent on a single task's runs — for the per-task budget guard."""
    conn = get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(cost),0) c FROM usage WHERE task_id = ?", (task_id,)
    ).fetchone()
    conn.close()
    return float(row["c"])


def task_usage(task_id: str) -> dict:
    """Per-task token/cost/run rollup — for the task-detail Usage panel."""
    conn = get_conn()
    row = conn.execute(
        """SELECT COALESCE(SUM(cost),0) cost, COALESCE(SUM(total_tokens),0) tokens,
                  COALESCE(SUM(input_tokens),0) input, COALESCE(SUM(output_tokens),0) output,
                  COALESCE(SUM(cache_read),0) cache_read, COUNT(*) runs
           FROM usage WHERE task_id = ?""",
        (task_id,),
    ).fetchone()
    conn.close()
    return dict(row)


def today_cost() -> float:
    """Total $ spent today (UTC) across all runs — for the daily budget guard."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(cost),0) c FROM usage WHERE substr(created_at,1,10) = ?",
        (today,),
    ).fetchone()
    conn.close()
    return float(row["c"])


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
