#!/usr/bin/env python3
"""Progress against the north star in docs/GOAL.md — "Sisyphus builds Sisyphus".

Every number is recounted from what the system already records: git history for
what landed on the trunk, sisyphus.db for how it got there, known-gaps.md for
what is still owed. Nothing is estimated, nothing is cached — the report has no
state of its own, so it cannot drift away from the truth it reports on.

    python3 tools/goal_report.py             # last 30 days
    python3 tools/goal_report.py --days 7
    python3 tools/goal_report.py --json
"""
import argparse
import json
import re
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SISYPHUS_DIR = os.path.dirname(HERE)
KNOWN_GAPS = os.path.join(SISYPHUS_DIR, "docs", "known-gaps.md")

sys.path.insert(0, SISYPHUS_DIR)

# core.db resolves DB_PATH at import time from WORKSPACE_ROOT, whose default is
# the daemon's /workspace. Run from a shell the daemon did not set up — which is
# every way a human runs this — that pointed at a file that does not exist. The
# repo's own workspace/ is what `sisyphus.py daemon` uses; an explicit env var
# still wins, so pointing the report at another install keeps working.
os.environ.setdefault("WORKSPACE_ROOT", os.path.join(SISYPHUS_DIR, "workspace"))

from core.db import DB_PATH, get_readonly_conn  # noqa: E402

# A merge that carries a worker's branch home. Sisyphus names every task branch
# sisyphus/task-<id>, so the trunk itself records which commits it authored.
MERGE_SUBJECT = "Merge sisyphus/task-"

TARGET_SELF_BUILD = 50   # north star: more than half of the trunk, agent-built

# The day the daemon started running the repo's own check and recording the
# verdict on the MR (fd2f86c). Merges older than this carry no check_status at
# all — not because the gate was skipped, but because nothing wrote the column
# yet — so the evidence rate is counted from here and anything older is reported
# separately as legacy. See evidence().
CHECK_GATE_SINCE = "2026-08-17"


def _rate(part: int, whole: int) -> float:
    """Percent, one decimal. An empty denominator is 0.0, never a crash."""
    return round(100.0 * part / whole, 1) if whole else 0.0


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _git(repo_root: str, *args: str) -> str:
    out = subprocess.run(["git", "-C", repo_root, *args],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {out.stderr.strip()}")
    return out.stdout.strip()


def repo_root(start: str = SISYPHUS_DIR) -> str:
    return _git(start, "rev-parse", "--show-toplevel")


# ---------------------------------------------------------------- the metrics

def self_build(root: str, days: int) -> dict:
    """Primary metric: the share of the trunk that arrived through a Sisyphus MR.

    Counted in commits, not merges — one merge may carry seven commits, and it
    is the code that matters, not the ceremony. Merge commits themselves are
    excluded from both sides so the ratio compares like with like.
    """
    since = f"--since={days} days ago"
    merges = [m for m in _git(root, "log", "--merges", "-F",
                              f"--grep={MERGE_SUBJECT}", since,
                              "--format=%H").splitlines() if m]
    delivered = 0
    for sha in merges:
        try:
            delivered += int(_git(root, "rev-list", "--count", "--no-merges",
                                  f"{sha}^1..{sha}^2"))
        except (RuntimeError, ValueError):
            continue          # a merge with one parent tells us nothing
    total = int(_git(root, "rev-list", "--count", "--no-merges", since, "HEAD"))
    return {"delivered": delivered, "total": total,
            "merges": len(merges), "rate": _rate(delivered, total)}


def autonomy(conn, days: int) -> dict:
    """Of the tasks that actually delivered an MR, how many needed no human turn.

    A task that opened an MR is one that did the work; the rest of the board is
    autopilot scans and review-of-MR passes, and counting those would flatter
    the number without anything having shipped.
    """
    cut = _cutoff(days)
    rows = conn.execute("""
        SELECT t.id,
               (SELECT COUNT(*) FROM messages m
                 WHERE m.task_id = t.id AND m.author = 'human')  AS human,
               (SELECT COUNT(*) FROM messages m
                 WHERE m.task_id = t.id AND m.kind = 'question') AS asked
          FROM tasks t
         WHERE t.created_at >= ?
           AND EXISTS (SELECT 1 FROM messages m
                        WHERE m.task_id = t.id AND m.kind = 'mr')
    """, (cut,)).fetchall()
    clean = sum(1 for r in rows if r["human"] == 0)
    return {"delivering": len(rows), "clean": clean,
            "questions": sum(r["asked"] for r in rows),
            "rate": _rate(clean, len(rows))}


def evidence(conn, days: int) -> dict:
    """Of the MRs merged, how many rest on a record rather than on a claim:
    a reviewer's approval AND a check the daemon ran and recorded itself.

    Counted only over merges the daemon could actually have checked. The check
    landed in fd2f86c; before it, no merge carries a check_status because the
    column had nothing writing to it. Counting those as unbacked measured the
    age of the feature rather than the behaviour of the system — with a 30-day
    window straddling the landing it read 21.4% while every merge after the gate
    existed was backed, which is a metric steering the north star away from the
    truth, in the direction that quietly devalues work already done.

    Pre-gate merges are still counted and reported, as `legacy`, because
    dropping them silently would be its own kind of flattery.
    """
    cut = _cutoff(days)
    rows = conn.execute("""
        SELECT review_status, check_status, created_at FROM merge_requests
         WHERE status = 'merged' AND created_at >= ?
    """, (cut,)).fetchall()
    gated = [r for r in rows if (r["created_at"] or "") >= CHECK_GATE_SINCE]
    legacy = len(rows) - len(gated)
    backed = sum(1 for r in gated
                 if r["review_status"] == "approved" and r["check_status"] == "passed")
    approved = sum(1 for r in gated if r["review_status"] == "approved")
    return {"merged": len(gated), "backed": backed, "approved": approved,
            "legacy": legacy, "since": CHECK_GATE_SINCE,
            "rate": _rate(backed, len(gated))}


def reliability(conn, days: int) -> dict:
    """Terminal tasks that failed, and how many failed the one way we keep
    seeing: a worker that ran and returned nothing at all."""
    cut = _cutoff(days)
    rows = conn.execute("""
        SELECT id, status FROM tasks
         WHERE created_at >= ? AND status IN ('done', 'failed')
    """, (cut,)).fetchall()
    failed = [r["id"] for r in rows if r["status"] == "failed"]
    silent = 0
    for tid in failed:
        hit = conn.execute("""
            SELECT 1 FROM messages
             WHERE task_id = ? AND (text LIKE '%ended without a result%'
                                 OR text LIKE '%agent_no_result%') LIMIT 1
        """, (tid,)).fetchone()
        if hit:
            silent += 1
    return {"terminal": len(rows), "failed": len(failed), "silent": silent,
            "rate": _rate(len(failed), len(rows))}


def open_gaps(path: str = KNOWN_GAPS) -> dict:
    """The ledger's own count — parsed by core.ledger, the one parser of
    docs/known-gaps.md."""
    from core.ledger import open_gaps as _parse
    return _parse(path)


def _wasted_counts(conn, cut: str) -> tuple:
    """(all worker_finished, those that ended without a result) since *cut*.

    A run that posted its deliverable and then stopped without its finishing
    tool is delivered, not wasted: core.worker's worker_end_message says so in
    its own words ("delivered its … but ended without calling its finishing
    tool") and it matches none of the wasted phrases below. The NOT clause
    keeps the two sides honest — it is the structured signal worker.py writes,
    so a wasted phrase can never grow into a delivered sentence and get
    double-counted."""
    wasted_texts = ["ended without reporting a result", "vanished", "failed to start",
                    "agent_no_result"]
    delivered_texts = ["but ended without calling its finishing tool"]
    all_finished = conn.execute("""
        SELECT COUNT(*) as cnt FROM messages
         WHERE created_at >= ? AND kind = 'worker_finished'
    """, (cut,)).fetchone()["cnt"]
    if not all_finished:
        return 0, 0
    wasted = conn.execute(f"""
        SELECT COUNT(*) as cnt FROM messages
         WHERE created_at >= ? AND kind = 'worker_finished'
           AND ({' OR '.join(['text LIKE ?' for _ in wasted_texts])})
           AND NOT ({' OR '.join(['text LIKE ?' for _ in delivered_texts])})
           AND text NOT LIKE '%was stopped before it reported%'
    """, (cut, *[f"%{t}%" for t in wasted_texts],
          *[f"%{t}%" for t in delivered_texts])).fetchone()["cnt"]
    return all_finished, wasted


def readiness(conn, days: int) -> dict:
    """Production readiness metrics: wasted runs, provider faults, duplicate
    workers, lost work, merge latency, stale-check blocks, human turns.

    These measure the health of the delivery pipeline — whether runs finish,
    merges land, and the system stays out of the human's way.
    """
    cut = _cutoff(days)

    # Wasted runs: worker_finished without a result. Counted over the report
    # window AND over the last 7 days, so the 30-day figure shows whether the
    # recent fixes (per-runner timeouts, queued-not-vanished, provider-fault
    # cooldown) are actually shrinking the rate — a 30-day window alone would
    # keep carrying history long after the cause is gone.
    all_finished, wasted_finished = _wasted_counts(conn, cut)
    wasted_rate = _rate(wasted_finished, all_finished)
    all_7d, wasted_7d = _wasted_counts(conn, _cutoff(7))
    wasted_rate_7d = _rate(wasted_7d, all_7d)

    # Provider faults: worker_finished with "set aside"
    provider_faults = {}
    faults = conn.execute("""
        SELECT text FROM messages
         WHERE created_at >= ? AND kind = 'worker_finished'
           AND text LIKE '%set aside%'
    """, (cut,)).fetchall()
    for fault_row in faults:
        # core.worker._set_aside_note writes "The <provider> runner is set aside".
        m = re.search(r"The (\S+) runner is set aside", fault_row["text"] or "")
        name = m.group(1) if m else "unknown"
        provider_faults[name] = provider_faults.get(name, 0) + 1

    # Duplicate workers: tasks with two worker_started with no worker_finished between
    duplicates = 0
    task_rows = conn.execute("""
        SELECT DISTINCT task_id FROM messages
         WHERE created_at >= ? AND kind IN ('worker_started', 'worker_finished')
    """, (cut,)).fetchall()
    for task_row in task_rows:
        task_id = task_row["task_id"]
        messages = conn.execute("""
            SELECT kind FROM messages
             WHERE task_id = ? AND created_at >= ?
             ORDER BY seq ASC
        """, (task_id, cut)).fetchall()
        prev_started_idx = -1
        for i, msg in enumerate(messages):
            if msg["kind"] == "worker_started":
                if prev_started_idx >= 0:
                    # Found two worker_started with no worker_finished between
                    duplicates += 1
                prev_started_idx = i
            elif msg["kind"] == "worker_finished":
                prev_started_idx = -1

    # Lost work: done tasks whose work never reached main. Two ways that
    # happens, all counted: commits with no MR at all (the original gap), and an
    # MR that was never merged (done before delivery — task-df5a3fcc: done while
    # auto-merge was blocked and the MR stayed open). An MR that DID merge is on
    # main even when a reviewer asked for changes — that is counted separately
    # below as merged over an objection, never as lost.
    lost = conn.execute("""
        SELECT DISTINCT t.id FROM tasks t
         WHERE t.created_at >= ? AND t.status = 'done'
           AND EXISTS (SELECT 1 FROM messages m
                        WHERE m.task_id = t.id AND m.kind = 'commit')
           AND (
                NOT EXISTS (SELECT 1 FROM merge_requests mr
                             WHERE mr.task_id = t.id)
                OR EXISTS (SELECT 1 FROM merge_requests mr
                            WHERE mr.task_id = t.id AND mr.status != 'merged')
           )
    """, (cut,)).fetchall()
    lost_count = len(lost)

    # Merged over an objection: a done task whose MR merged while a reviewer's
    # CHANGES REQUESTED on the head was left unanswered — no commit on the
    # branch after the verdict (the thread tracks heads as commit messages, so
    # a request with no commit after it still stands). The work DID reach main,
    # so this is not lost work; the merge overrode a standing objection.
    objected = conn.execute("""
        SELECT DISTINCT t.key FROM tasks t
         WHERE t.created_at >= ? AND t.status = 'done'
           AND EXISTS (SELECT 1 FROM merge_requests mr
                        WHERE mr.task_id = t.id AND mr.status = 'merged')
           AND EXISTS (
               SELECT 1 FROM messages m
                WHERE m.task_id = t.id
                  AND (
                      (m.kind = 'result' AND m.author LIKE '%review%'
                       AND m.text LIKE 'CHANGES REQUESTED%')
                      OR (m.kind = 'note'
                          AND m.text LIKE 'review verdict for %: CHANGES REQUESTED%')
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM messages m2
                       WHERE m2.task_id = t.id
                         AND m2.kind = 'commit'
                         AND m2.seq > m.seq
                  )
           )
    """, (cut,)).fetchall()
    objected_tasks = sorted(r["key"] for r in objected)
    objected_count = len(objected_tasks)

    # Merge latency: median hours from MR created_at to merged_at
    latencies = conn.execute("""
        SELECT (julianday(merged_at) - julianday(created_at)) * 24 as hours
         FROM merge_requests
         WHERE created_at >= ? AND status = 'merged' AND merged_at IS NOT NULL
    """, (cut,)).fetchall()
    median_latency = 0.0
    if latencies:
        latencies_sorted = sorted([l["hours"] for l in latencies])
        n = len(latencies_sorted)
        if n % 2 == 1:
            median_latency = latencies_sorted[n // 2]
        else:
            median_latency = (latencies_sorted[n // 2 - 1] + latencies_sorted[n // 2]) / 2.0

    # Stale-check blocks: the merge held because the recorded check was for an
    # older head or had not run (SIS-115, SIS-117). The daemon announces exactly
    # one such block per (head, reason), with the one text that means
    # check-stale — `_note_auto_merge_waiting` on `_stale_check_reason`. The
    # bare 'check-stale' substring matches prose (an engineer describing the
    # fix), and 'auto-merge waiting' matches every other block — review pending,
    # dirty checkout — which never were about the check. Both used to inflate
    # the count with notes that were never a stale-check block at all.
    #
    # A genuine block note is history, not a live problem, the moment it was
    # resolved automatically: the daemon re-checked the current head
    # (check_ran_at after the note) or auto-merged the MR. Only blocks no
    # automatic path cleared still count.
    stale_blocks = conn.execute("""
        SELECT COUNT(*) as cnt FROM messages m
         WHERE m.created_at >= ? AND m.kind = 'note'
           AND m.text LIKE 'auto-merge waiting: the check passed against%'
           AND NOT EXISTS (
               SELECT 1 FROM messages m2
                WHERE m2.task_id = m.task_id
                  AND m2.created_at > m.created_at
                  AND m2.text LIKE 'Auto-merged %'
           )
           AND NOT EXISTS (
               SELECT 1 FROM merge_requests mr
                WHERE mr.task_id = m.task_id
                  AND mr.check_ran_at IS NOT NULL
                  AND mr.check_ran_at > m.created_at
           )
    """, (cut,)).fetchone()["cnt"]

    # Human turns per delivered task: answer messages by human per task with MR
    delivering = conn.execute("""
        SELECT DISTINCT t.id FROM tasks t
         WHERE t.created_at >= ?
           AND EXISTS (SELECT 1 FROM merge_requests mr WHERE mr.task_id = t.id)
    """, (cut,)).fetchall()
    total_delivering = len(delivering)
    human_turns = 0
    if total_delivering > 0:
        human_answers = conn.execute("""
            SELECT t.id, COUNT(*) as cnt
              FROM messages m
              JOIN tasks t ON m.task_id = t.id
             WHERE m.created_at >= ? AND m.author = 'human' AND m.kind = 'answer'
               AND EXISTS (SELECT 1 FROM merge_requests mr WHERE mr.task_id = t.id)
             GROUP BY t.id
        """, (cut,)).fetchall()
        human_turns = sum(row["cnt"] for row in human_answers)

    human_per_delivered = (human_turns / total_delivering) if total_delivering > 0 else 0.0

    return {
        "wasted_finished": wasted_finished,
        "all_finished": all_finished,
        "wasted_rate": wasted_rate,
        "wasted_finished_7d": wasted_7d,
        "all_finished_7d": all_7d,
        "wasted_rate_7d": wasted_rate_7d,
        "provider_faults": provider_faults,
        "duplicates": duplicates,
        "lost_work": lost_count,
        "merged_over_objection": objected_count,
        "merged_over_objection_tasks": objected_tasks,
        "merged_count": len(latencies),
        "median_latency_hours": round(median_latency, 1),
        "stale_blocks": stale_blocks,
        "human_per_delivered": round(human_per_delivered, 2),
    }


def report(days: int = 30) -> dict:
    if not os.path.exists(DB_PATH):
        # Say which file is missing. The bare sqlite error ("unable to open
        # database file") names nothing and sends you reading source.
        raise SystemExit(f"no database at {DB_PATH} — set WORKSPACE_ROOT to the "
                         f"install you want to report on.")
    conn = get_readonly_conn()
    try:
        return {
            "days": days,
            "target": TARGET_SELF_BUILD,
            "self_build": self_build(repo_root(), days),
            "autonomy": autonomy(conn, days),
            "evidence": evidence(conn, days),
            "reliability": reliability(conn, days),
            "readiness": readiness(conn, days),
            "gaps": open_gaps(),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------- the printout

def _bar(pct: float, width: int = 28) -> str:
    filled = min(width, int(round(width * pct / 100.0)))
    return "█" * filled + "·" * (width - filled)


def render(r: dict) -> str:
    sb, au, ev, rel, rd, gaps = (r["self_build"], r["autonomy"], r["evidence"],
                                 r["reliability"], r["readiness"], r["gaps"])
    lines = [
        f"Sisyphus builds Sisyphus — last {r['days']} days",
        "",
        f"  SELF-BUILD   {sb['rate']:5.1f}%  {_bar(sb['rate'])}  target {r['target']}%",
        f"               {sb['delivered']} of {sb['total']} commits on the trunk, "
        f"through {sb['merges']} Sisyphus merges",
        "",
        f"  autonomy     {au['rate']:5.1f}%  {_bar(au['rate'])}",
        f"               {au['clean']} of {au['delivering']} delivering tasks "
        f"needed no human turn ({au['questions']} questions asked)",
        "",
        f"  evidence     {ev['rate']:5.1f}%  {_bar(ev['rate'])}",
        f"               {ev['backed']} of {ev['merged']} merges cite approval + a "
        f"recorded check ({ev['approved']} approved)",
        *([f"               ({ev['legacy']} older merges excluded — the check "
           f"did not exist before {ev['since']})"] if ev["legacy"] else []),
        "",
        f"  reliability  {100 - rel['rate']:5.1f}%  {_bar(100 - rel['rate'])}",
        f"               {rel['failed']} of {rel['terminal']} terminal tasks failed, "
        f"{rel['silent']} of them silently",
        "",
        "  READINESS",
        f"    wasted runs       {rd['wasted_rate']:5.1f}%  {_bar(100-rd['wasted_rate'])}",
        f"                      {rd['wasted_finished']} of {rd['all_finished']} "
        f"worker runs ended without a result",
        f"    wasted runs 7d    {rd['wasted_rate_7d']:5.1f}%  {_bar(100-rd['wasted_rate_7d'])}",
        f"                      {rd['wasted_finished_7d']} of {rd['all_finished_7d']} "
        f"worker runs ended without a result (last 7 days)",
    ]
    if rd['provider_faults']:
        faults_str = ", ".join(f"{k}: {v}" for k, v in rd['provider_faults'].items())
        lines.append(f"    provider faults    {faults_str}")
    else:
        lines.append("    provider faults    none")
    lines.extend([
        f"    duplicate workers  {rd['duplicates']} tasks with concurrent starts",
        f"    lost work          {rd['lost_work']} done tasks with commits, work not on main",
        f"    merged over obj.   {rd['merged_over_objection']} done tasks whose MR merged over an "
        f"unanswered CHANGES REQUESTED",
        *([f"                      {', '.join(rd['merged_over_objection_tasks'])}"]
          if rd['merged_over_objection_tasks'] else []),
        f"    merge latency      {rd['median_latency_hours']}h median from MR to merged "
        f"({rd['merged_count']} merges)",
        f"    stale-check blocks {rd['stale_blocks']} notes waiting on check status, "
        f"never auto-resolved",
        f"    human turns        {rd['human_per_delivered']} per delivered task",
        "",
        f"  known gaps   {gaps['open']} open, {gaps['closed']} closed",
    ])
    lines += [f"               · {t}" for t in gaps["titles"]]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30, help="window in days (default 30)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()
    try:
        r = report(args.days)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(json.dumps(r, indent=2) if args.json else render(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
