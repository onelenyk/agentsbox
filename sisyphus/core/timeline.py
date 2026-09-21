"""A task's thread, read as what happened — for the task detail page.

The thread is an append-only log written for agents: a worker's start carries
its whole instruction, a merge is a system note, a failure is an orchestrator
note, and a single engineering task on this board runs to 76 messages. Shown
as-is it reads as a chat log with the answer somewhere in the middle. These
functions read it once and return the four things a person opens a task for:

  outcome   what the task produced — or why it did not, or what it is asking
  steps     how it got there: runs, commits, checks, verdicts, decisions
  history   when its status changed, and why, with the pauses between
  files     what it left behind: patches, reports, recovered work, run logs

Pure functions over data the caller already has. Nothing here queries the
database or writes anything, so the page and its tests see the same thing.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

WORKERS = ("engineer", "explorer")            # roles whose result IS the answer
REVIEWERS = ("reviewer", "mr-reviewer")

_STARTED = re.compile(r"^spawning (\S+?):\s*(.*)", re.S)
_ENDED_EMPTY = re.compile(r"^(\S+) ended without reporting a result \(status=(\w+)\):?\s*(.*)", re.S)
_FAILED_START = re.compile(r"^(\S+) failed to start:\s*(.*)", re.S)
_FINISHED = re.compile(r"^(\S+) finished\b")
_VANISHED = re.compile(r"^worker vanished without reporting")
_CHECK = re.compile(r"^Automated check (PASSED|FAILED) \(exit (-?\d+), (\d+)s\)", re.I)
_VERDICT = re.compile(r"^review verdict for (mr-\S+?):\s*(APPROVED|CHANGES REQUESTED|\S+)", re.I)
_MERGED = re.compile(r"^Merged (\S+) into (\S+) \(([0-9a-f]+)\)")
_PASSED_N = re.compile(r"(\d+) passed")
_FAILED_TEST = re.compile(r"FAILED (\S+::\S+)")


def _at(iso: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None


def minutes_between(a: str, b: str) -> Optional[int]:
    x, y = _at(a), _at(b)
    if not x or not y:
        return None
    return max(0, int((y - x).total_seconds() // 60))


def span(mins: Optional[int]) -> str:
    """A duration the way a person says it: 7m, 3h, 2d."""
    if mins is None:
        return ""
    if mins < 60:
        return f"{mins}m"
    if mins < 60 * 24:
        h, m = divmod(mins, 60)
        return f"{h}h" if m < 5 else f"{h}h{m:02d}m"
    return f"{mins // (60 * 24)}d"


def clause(text: str, limit: int = 72) -> str:
    """The first clause of *text*, cut at a word and marked when it was cut —
    never "(1302 pas"."""
    t = re.split(r"(?<=[.;!?])\s", (text or "").strip(), maxsplit=1)[0]
    if len(t) <= limit:
        return t
    return t[:limit].rsplit(" ", 1)[0].rstrip(",;:—-") + "…"


# ── outcome ──────────────────────────────────────────────────────────────────

def outcome(task: dict, messages: list) -> dict:
    """What the task produced, in one of four honest shapes.

    `answer`   the last result from a WORKER — the reviewer's verdict on it is
               a fact about the answer, not the answer. The old card showed the
               last result by anyone, which on 31 tasks here meant the verdict
               replaced the work. A task handled by a reviewer alone (a review,
               a scan) has no worker, so its own last result is the answer.
    `failure`  why it ended badly: the orchestrator's [fail] note, sharpened by
               the last worker's own reason (a timeout, an idle kill). None of
               the 13 failed tasks here had a result message, so the old card
               never rendered for them at all.
    `question` what a waiting task is asking you.
    `none`     nothing yet.
    """
    results = [m for m in messages if m["kind"] == "result" and m.get("text")]
    status = task.get("status")

    if status == "waiting":
        asked = [m for m in messages if m["kind"] == "question"]
        if asked:
            q = asked[-1]
            return {"kind": "question", "author": q["author"], "at": q["created_at"], "text": q["text"]}

    if status == "failed":
        fail = next((m for m in reversed(messages)
                     if m["author"] == "orchestrator" and (m.get("text") or "").startswith("[fail]")), None)
        why = next((m for m in reversed(messages) if m["kind"] == "worker_finished"
                    and (_ENDED_EMPTY.match(m["text"] or "") or _FAILED_START.match(m["text"] or "")
                         or _VANISHED.match(m["text"] or ""))), None)
        headline = (fail["text"][len("[fail]"):].strip() if fail else "It ended without saying why.")
        detail = why["text"] if why else ""
        return {"kind": "failure", "author": "orchestrator",
                "at": (fail or why or {"created_at": task.get("updated_at")})["created_at"],
                "text": headline, "detail": detail}

    by_workers = [m for m in results if m["author"] in WORKERS]
    chosen = (by_workers or results)[-1] if (by_workers or results) else None
    if chosen:
        return {"kind": "answer", "author": chosen["author"], "at": chosen["created_at"],
                "text": _strip_verdict_word(chosen["text"], task)}
    return {"kind": "none"}


def _strip_verdict_word(text: str, task: dict) -> str:
    """A scan run by the reviewer role opens its report with the role's own
    verdict word ("APPROVED: Scan complete…") — 41 results on repo-less tasks
    here. On a task with no repository there is nothing to approve; the word is
    noise in front of the answer."""
    if task.get("repo"):
        return text
    return re.sub(r"^\s*APPROVED\s*[:—-]\s*", "", text, count=1)


def verdicts(messages: list) -> list:
    """Every review verdict recorded on the thread, oldest first."""
    out = []
    for m in messages:
        v = _VERDICT.match(m.get("text") or "") if m["author"] == "system" else None
        if v:
            word = v.group(2).upper()
            out.append({"mr": v.group(1), "approved": word.startswith("APPROVE"), "at": m["created_at"]})
    return out


# ── steps ────────────────────────────────────────────────────────────────────

def steps(messages: list) -> list:
    """How the task got to its outcome, as the steps a person would name.

    One step per worker RUN (its start and its end folded together, with its
    commits and notes inside), and one per check, verdict, decision, question,
    reply and merge. Service traffic — queued, sandbox notices, spawn chatter —
    is not a step; a spawn's reason rides along on the run it started. Runs of
    the same role that all ended without a result collapse into one step, so
    five dead attempts read as one fact instead of ten rows.
    """
    out: list = []
    # role -> runs in progress, oldest first. Two workers of one role can run
    # at once (the orchestrator re-spawned an engineer a minute after the
    # first), so a role maps to a queue, and an end closes the oldest.
    open_runs: dict = {}
    pending_reason: Optional[str] = None

    def close(role: Optional[str], m: dict, how: str, detail: str = ""):
        queue = open_runs.get(role) if role else None
        run = queue.pop(0) if queue else None
        if run is None and not role:
            # "worker vanished" does not say whose; the oldest open run it is,
            # or failing that, the role that ran last
            oldest = min((q[0] for q in open_runs.values() if q), key=lambda r: r["started"] or "", default=None)
            if oldest is not None:
                open_runs[oldest["role"]].remove(oldest)
                run = oldest
            else:
                last = next((x for x in reversed(out) if x["type"] == "run"), None)
                role = last["role"] if last else "worker"
        if run is None:
            run = {"type": "run", "role": role or "worker", "started": None, "commits": [],
                   "notes": [], "instruction": "", "why": ""}
            out.append(run)
        run.update({"ended": m["created_at"], "how": how, "detail": detail,
                    "mins": minutes_between(run["started"], m["created_at"]) if run["started"] else None})

    def current(role: str) -> Optional[dict]:
        queue = open_runs.get(role)
        return queue[-1] if queue else None

    for m in messages:
        text = m.get("text") or ""
        kind, author = m["kind"], m["author"]

        if kind == "worker_started":
            s = _STARTED.match(text)
            role = s.group(1) if s else "worker"
            run = {"type": "run", "role": role, "started": m["created_at"], "ended": None,
                   "how": "running", "detail": "", "mins": None, "commits": [], "notes": [],
                   "instruction": (s.group(2) if s else text).strip(), "why": pending_reason or ""}
            pending_reason = None
            open_runs.setdefault(role, []).append(run)
            out.append(run)
            continue
        if kind == "worker_finished":
            if f := _FINISHED.match(text):
                close(f.group(1), m, "ok")
            elif e := _ENDED_EMPTY.match(text):
                close(e.group(1), m, e.group(2), e.group(3).strip())
            elif fs := _FAILED_START.match(text):
                close(fs.group(1), m, "failed to start", fs.group(2).strip())
            elif _VANISHED.match(text):
                close(None, m, "vanished")
            continue
        if kind == "commit":
            run = current(author) or _last_run(out, author)
            if run is not None:
                run["commits"].append(text)
            else:
                out.append({"type": "commit", "role": author, "at": m["created_at"], "text": text})
            continue
        if kind == "result" and current(author):
            current(author)["reported"] = True
            continue
        if kind == "question":
            out.append({"type": "question", "role": author, "at": m["created_at"], "text": text})
            continue
        if author == "human" or kind == "answer":
            out.append({"type": "you", "at": m["created_at"], "text": text})
            continue
        if kind == "external":
            out.append({"type": "external", "at": m["created_at"], "text": text})
            continue
        if author == "system":
            if c := _CHECK.match(text):
                passed = c.group(1).upper() == "PASSED"
                n = _PASSED_N.search(text)
                broke = _FAILED_TEST.search(text)
                out.append({"type": "check", "at": m["created_at"], "passed": passed,
                            "tests": int(n.group(1)) if n else None,
                            "secs": int(c.group(3)), "broke": broke.group(1) if (broke and not passed) else "",
                            "text": text})
            elif v := _VERDICT.match(text):
                out.append({"type": "verdict", "at": m["created_at"], "mr": v.group(1),
                            "approved": v.group(2).upper().startswith("APPROVE"), "text": text})
            elif g := _MERGED.match(text):
                out.append({"type": "merge", "at": m["created_at"], "target": g.group(2),
                            "sha": g.group(3), "text": text})
            elif text.startswith(("reopened by the human", "stopped by the human")):
                out.append({"type": "control", "at": m["created_at"], "text": text})
            continue
        if author == "orchestrator" and kind == "note":
            if text.startswith("[spawn_worker]"):
                pending_reason = text[len("[spawn_worker]"):].strip()
            elif text.startswith(("[done]", "[fail]")):
                out.append({"type": "decision", "at": m["created_at"],
                            "done": text.startswith("[done]"), "text": text.split("]", 1)[1].strip()})
            continue
        if author in WORKERS + REVIEWERS and kind == "note":
            run = current(author) or _last_run(out, author)
            if run is not None:
                run["notes"].append(text)
            continue

    return _collapse_dead_runs(out)


def _last_run(out: list, role: str) -> Optional[dict]:
    return next((s for s in reversed(out) if s["type"] == "run" and s["role"] == role), None)


def _collapse_dead_runs(out: list) -> list:
    """Consecutive runs of one role that each ended with nothing become a
    single step that counts them and says how each died."""
    collapsed: list = []
    for s in out:
        dead = s["type"] == "run" and s["how"] not in ("ok", "running") and not s["commits"]
        prev = collapsed[-1] if collapsed else None
        if dead and prev and prev["type"] == "dead-runs" and prev["role"] == s["role"]:
            prev["runs"].append(s)
            prev["ended"] = s["ended"]
            continue
        if dead:
            collapsed.append({"type": "dead-runs", "role": s["role"], "runs": [s],
                              "started": s["started"], "ended": s["ended"]})
            continue
        collapsed.append(s)
    for s in collapsed:
        if s["type"] == "dead-runs":
            hows: dict = {}
            for r in s["runs"]:
                hows[r["how"]] = hows.get(r["how"], 0) + 1
            s["hows"] = hows
    return collapsed


# ── history ──────────────────────────────────────────────────────────────────

def history(task: dict, messages: list, mr: Optional[dict], events: list) -> list:
    """The task's status over time, each change with its cause and the pause
    that preceded it.

    Recorded events (task_events, kept since 2026-09-22) are the truth when a
    task has them. For everything older the history is rebuilt from the thread,
    because that is where every status change left a mark — and each rebuilt
    entry says so, so a reader knows it is inferred.
    """
    entries = [{"status": "created", "at": task["created_at"], "why": "", "inferred": False}]
    if events:
        for e in events:
            entries.append({"status": e["to_status"], "at": e["at"], "why": e.get("cause") or "",
                            "inferred": False})
    else:
        entries += _rebuild(messages)
        final = task.get("status")
        if final and entries[-1]["status"] != final:
            entries.append({"status": final, "at": task.get("updated_at") or task["created_at"],
                            "why": "", "inferred": True})

    if mr and mr.get("status") == "merged" and mr.get("merged_at"):
        entries.append({"status": "shipped", "at": mr["merged_at"],
                        "why": f"merged into {mr.get('target_branch') or 'main'}", "inferred": False})
    elif mr and mr.get("status") == "closed":
        entries.append({"status": "not merged", "at": mr.get("updated_at") or task["updated_at"],
                        "why": "the merge request was closed", "inferred": False})

    entries.sort(key=lambda e: e["at"] or "")
    squashed: list = []
    for e in entries:
        if squashed and squashed[-1]["status"] == e["status"]:
            continue                      # a status restated is not a change
        squashed.append(e)
    for prev, e in zip(squashed, squashed[1:]):
        e["after"] = span(minutes_between(prev["at"], e["at"]))
        e["from"] = prev["status"]
    return squashed


def _rebuild(messages: list) -> list:
    out = []
    for m in messages:
        text = m.get("text") or ""
        closed = bool(out) and out[-1]["status"] in ("done", "failed")
        # Once a task is done or failed only a person brings it back: a
        # re-review that runs after [done] is follow-up work, not the task
        # returning to active.
        if m["kind"] in ("worker_queued", "worker_started") and not closed \
                and (not out or out[-1]["status"] != "active"):
            out.append({"status": "active", "at": m["created_at"], "why": "work began", "inferred": True})
        elif m["kind"] == "question":
            out.append({"status": "waiting", "at": m["created_at"],
                        "why": f"{m['author']} asked you", "inferred": True})
        elif m["kind"] == "answer" or m["author"] == "human":
            out.append({"status": "active", "at": m["created_at"], "why": "you answered", "inferred": True})
        elif m["author"] == "system" and text.startswith("reopened by the human"):
            out.append({"status": "active", "at": m["created_at"], "why": "you reopened it", "inferred": True})
        elif m["author"] == "system" and text.startswith("stopped by the human"):
            out.append({"status": "waiting", "at": m["created_at"], "why": "you stopped it", "inferred": True})
        elif m["author"] == "orchestrator" and text.startswith("[done]"):
            out.append({"status": "done", "at": m["created_at"], "why": clause(text[6:]), "inferred": True})
        elif m["author"] == "orchestrator" and text.startswith("[fail]"):
            out.append({"status": "failed", "at": m["created_at"], "why": clause(text[6:]), "inferred": True})
    return out


# ── files ────────────────────────────────────────────────────────────────────

def files(task_id: str, messages: list, workspace: Optional[str] = None) -> dict:
    """What the task left on disk, grouped the way a person looks for it.

    Only 75 of the 146 attachment files on this board were ever linked from a
    message; the rest — mostly work recovered at cleanup — could not be reached
    from the UI at all. This lists the directory itself.
    """
    ws = Path(workspace or os.environ.get("WORKSPACE_ROOT", "/workspace"))
    posted = {m["attachment"]: m for m in messages if m.get("attachment")}
    produced, recovered, logs = [], [], []

    adir = ws / "tasks" / task_id / "attachments"
    if adir.is_dir():
        for f in sorted(adir.iterdir()):
            if not f.is_file() or f.name.startswith("."):
                continue
            by = posted.get(f.name, {}).get("author", "")
            if f.name.startswith("salvaged-"):
                role = f.stem.rsplit("_", 1)[-1] if "_" in f.stem else ""
                recovered.append(_file(f, "salvaged", by or role))
            else:
                kind = "patch" if f.suffix in (".patch", ".diff") else ("report" if f.suffix == ".md" else "file")
                produced.append(_file(f, kind, by))

    ldir = ws / "runs" / task_id / "logs"
    if ldir.is_dir():
        for f in sorted(ldir.iterdir()):
            if f.is_file() and f.suffix == ".log":
                role = f.stem[len(task_id) + 1:] if f.stem.startswith(task_id) else f.stem
                logs.append(_file(f, "log", role))

    return {"produced": produced, "recovered": recovered, "logs": logs,
            "count": len(produced) + len(recovered) + len(logs)}


def _file(path: Path, kind: str, by: str) -> dict:
    st = path.stat()
    return {"name": path.name, "kind": kind, "by": by, "size": st.st_size,
            "size_h": human_size(st.st_size),
            "at": datetime.fromtimestamp(st.st_mtime).isoformat()}


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"
