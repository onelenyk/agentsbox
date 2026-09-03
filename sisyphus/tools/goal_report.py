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
    """The ledger's own count — parsed by core.ledger, which is also what the
    board copilot reads, so the report and the board can never disagree about
    what is still owed."""
    from core.ledger import open_gaps as _parse
    return _parse(path)


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
            "gaps": open_gaps(),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------- the printout

def _bar(pct: float, width: int = 28) -> str:
    filled = min(width, int(round(width * pct / 100.0)))
    return "█" * filled + "·" * (width - filled)


def render(r: dict) -> str:
    sb, au, ev, rel, gaps = (r["self_build"], r["autonomy"], r["evidence"],
                             r["reliability"], r["gaps"])
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
        f"  known gaps   {gaps['open']} open, {gaps['closed']} closed",
    ]
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
