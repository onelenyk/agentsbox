"""Repository health — does the repo build, do its tests pass, what is covered.

Every project builds differently, so what to run is a per-repo *profile* in
config.yaml, discovered by the repo-profiler agent and confirmed by the human:

    repos:
      - name: kmp-driver
        health:
          build: make build
          test: make test
          coverage: ./gradlew koverXmlReport          # optional
          junit: app/build/test-results/**/*.xml      # JUnit XML glob(s), comma-separated
          coverage_report: app/build/reports/kover/report.xml
          timeout: 1800                               # per stage, seconds

A run clones the repo's branch into a throwaway directory (never the human's
checkout), applies the repo's setup hook — the gitignored files a build needs —
and runs the stages in order. Numbers come from the reports the tools write
(JUnit XML, JaCoCo/Kover or Cobertura XML), never from anything an agent said.
Runs are kept in repo_health_runs; the latest one is the card on the repo page.

Runs happen on the human's button and daily after that (health_forever): a repo
that was never run is never started by the loop, so a newly confirmed profile
does not kick off a long build at an arbitrary moment.
"""
import asyncio
import glob
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import List, Optional

logger = logging.getLogger(__name__)

STAGES = ("build", "test", "coverage")
PROFILE_KEYS = ("build", "test", "coverage", "junit", "coverage_report")
DEFAULT_TIMEOUT = 1800
OUTPUT_TAIL = 4000
FAILED_NAMES_MAX = 20
PROFILER_AGENT = "repo-profiler"

OK, FAIL, SKIPPED, UNKNOWN = "ok", "fail", "skipped", "unknown"

# One run at a time, across repos: a Gradle build and a pytest run on the same
# laptop fight for the same cores, and both would read slower and flakier.
_lock = threading.Lock()
_running: dict = {}          # repo -> started (monotonic), while a run is in flight


# ── storage ──────────────────────────────────────────────────────────────────

def init_health_table() -> None:
    from .db import get_conn
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS repo_health_runs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            repo         TEXT NOT NULL,
            head         TEXT NOT NULL DEFAULT '',
            trigger      TEXT NOT NULL DEFAULT 'manual',   -- manual | daily
            status       TEXT NOT NULL,                     -- ok | fail | unknown
            summary      TEXT NOT NULL DEFAULT '',
            stages       TEXT NOT NULL DEFAULT '[]',        -- JSON list of stage dicts
            tests_total  INTEGER,
            tests_failed INTEGER,
            coverage_pct REAL,
            duration_ms  INTEGER NOT NULL DEFAULT 0,
            ran_at       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_health_repo ON repo_health_runs(repo, id);
        -- What the repo-profiler agent found, waiting for the human to confirm.
        CREATE TABLE IF NOT EXISTS repo_health_proposals (
            repo        TEXT PRIMARY KEY,
            profile     TEXT NOT NULL,                      -- JSON {build, test, …}
            notes       TEXT NOT NULL DEFAULT '',
            task_id     TEXT,
            proposed_at TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(r) -> dict:
    d = dict(r)
    d["stages"] = json.loads(d.get("stages") or "[]")
    return d


def record_run(res: dict) -> dict:
    from .db import get_conn
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO repo_health_runs (repo, head, trigger, status, summary, stages, "
            "tests_total, tests_failed, coverage_pct, duration_ms, ran_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (res["repo"], res.get("head", ""), res.get("trigger", "manual"), res["status"],
             res.get("summary", ""), json.dumps(res.get("stages", [])),
             res.get("tests_total"), res.get("tests_failed"), res.get("coverage_pct"),
             res.get("duration_ms", 0), res.get("ran_at") or _now()))
        conn.commit()
        return _row(conn.execute("SELECT * FROM repo_health_runs WHERE id = ?",
                                 (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


def runs(repo: str, limit: int = 10) -> List[dict]:
    """The newest *limit* runs of *repo*, newest first."""
    from .db import get_conn
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM repo_health_runs WHERE repo = ? "
                            "ORDER BY id DESC LIMIT ?", (repo, limit)).fetchall()
        return [_row(r) for r in rows]
    finally:
        conn.close()


def last_run(repo: str) -> Optional[dict]:
    r = runs(repo, 1)
    return r[0] if r else None


def save_proposal(repo: str, profile: dict, notes: str = "", task_id: str = "") -> None:
    from .db import get_conn
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO repo_health_proposals (repo, profile, notes, task_id, proposed_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(repo) DO UPDATE SET profile = excluded.profile, "
            "notes = excluded.notes, task_id = excluded.task_id, proposed_at = excluded.proposed_at",
            (repo, json.dumps(profile), notes or "", task_id or None, _now()))
        conn.commit()
    finally:
        conn.close()


def get_proposal(repo: str) -> Optional[dict]:
    from .db import get_conn
    conn = get_conn()
    try:
        r = conn.execute("SELECT * FROM repo_health_proposals WHERE repo = ?", (repo,)).fetchone()
    finally:
        conn.close()
    if not r:
        return None
    d = dict(r)
    d["profile"] = json.loads(d["profile"] or "{}")
    return d


def clear_proposal(repo: str) -> None:
    from .db import get_conn
    conn = get_conn()
    try:
        conn.execute("DELETE FROM repo_health_proposals WHERE repo = ?", (repo,))
        conn.commit()
    finally:
        conn.close()


# ── profile ──────────────────────────────────────────────────────────────────

def clean_profile(raw: Optional[dict]) -> dict:
    """Only the known keys, stripped, empties dropped; timeout an int if given."""
    raw = raw or {}
    out = {k: str(raw.get(k) or "").strip() for k in PROFILE_KEYS}
    out = {k: v for k, v in out.items() if v}
    try:
        t = int(raw.get("timeout") or 0)
    except (TypeError, ValueError):
        t = 0
    if t > 0:
        out["timeout"] = t
    return out


def profile(repo_name: str) -> dict:
    """The confirmed profile of *repo_name* — {} when it has none."""
    from .repos import get_repo
    return clean_profile((get_repo(repo_name) or {}).get("health"))


# ── report parsing ───────────────────────────────────────────────────────────

def _paths(root: str, patterns: str) -> List[str]:
    found: List[str] = []
    for pat in (p.strip() for p in (patterns or "").split(",")):
        if pat:
            found += glob.glob(os.path.join(root, pat), recursive=True)
    return sorted(set(p for p in found if os.path.isfile(p)))


def parse_junit(paths: List[str]) -> Optional[dict]:
    """Totals across JUnit XML files, or None when none parsed. Counts
    <testcase> elements rather than trusting suite attributes, which some tools
    omit or aggregate differently."""
    total = failed = skipped = 0
    names: List[str] = []
    parsed = False
    for p in paths:
        try:
            root = ET.parse(p).getroot()
        except (ET.ParseError, OSError):
            continue
        parsed = True
        for case in root.iter("testcase"):
            total += 1
            if case.find("failure") is not None or case.find("error") is not None:
                failed += 1
                if len(names) < FAILED_NAMES_MAX:
                    cls = case.get("classname") or ""
                    names.append(f"{cls}.{case.get('name')}" if cls else (case.get("name") or "?"))
            elif case.find("skipped") is not None:
                skipped += 1
    if not parsed:
        return None
    return {"total": total, "failed": failed, "skipped": skipped,
            "passed": total - failed - skipped, "failed_names": names}


def parse_coverage(path: str) -> Optional[float]:
    """Line coverage in percent from a JaCoCo/Kover report (<report> with a
    report-level <counter type="LINE">) or a Cobertura one (<coverage
    line-rate>, which coverage.py also writes). None when unreadable."""
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None
    if root.tag == "coverage" and root.get("line-rate") is not None:
        try:
            return round(float(root.get("line-rate")) * 100, 1)
        except ValueError:
            return None
    if root.tag == "report":
        for c in root.findall("counter"):
            if c.get("type") == "LINE":
                missed, covered = int(c.get("missed", 0)), int(c.get("covered", 0))
                return round(100.0 * covered / (missed + covered), 1) if missed + covered else None
    return None


# ── a run ────────────────────────────────────────────────────────────────────

def _stage(name, status, detail="", output="", command="", secs=0.0) -> dict:
    return {"name": name, "status": status, "detail": detail, "command": command,
            "output": (output or "")[-OUTPUT_TAIL:], "secs": round(secs, 1)}


def _error_excerpt(output: str, lines: int = 12) -> str:
    """The lines a person looks for first: error/failure lines if the tool
    printed any, else the tail."""
    rows = [ln for ln in (output or "").splitlines() if ln.strip()]
    marked = [ln for ln in rows if any(k in ln.lower() for k in
              ("error", "failed", "failure", "exception", "e: ", "what went wrong"))]
    return "\n".join((marked or rows)[-lines:])


def _exec(cmd: str, cwd: str, timeout: int) -> tuple:
    """(exit_code or None on timeout, combined output, seconds)."""
    from .verify import check_env
    started = time.monotonic()
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, env=check_env(),
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip(), time.monotonic() - started
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))
        return None, (out + f"\n… killed after {timeout}s").strip(), time.monotonic() - started


def _clone(source: str, branch: str, dest: str) -> tuple:
    """Shallow clone of *branch*; (ok, head, error). A local path goes through
    file:// so --depth applies instead of being ignored with a warning."""
    src = source if ("://" in source or source.startswith("git@")) else \
        "file://" + os.path.abspath(os.path.expanduser(source))
    p = subprocess.run(["git", "clone", "--quiet", "--depth", "1", "--no-tags",
                        "--branch", branch, src, dest], capture_output=True, text=True, timeout=600)
    if p.returncode != 0:
        return False, "", (p.stderr or p.stdout).strip()
    head = subprocess.run(["git", "-C", dest, "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    return True, head, ""


def run(repo_name: str, trigger: str = "manual", record: bool = True) -> dict:
    """Run *repo_name*'s health profile once and (by default) record it.

    Returns {repo, trigger, status, summary, head, stages, tests_total,
    tests_failed, coverage_pct, duration_ms}. Never raises for a failing build or
    test — that is the answer, not an error."""
    from .repos import get_repo
    from . import setup_hook

    started = time.monotonic()
    repo = get_repo(repo_name) or {}
    prof = profile(repo_name)
    res = {"repo": repo_name, "trigger": trigger, "status": UNKNOWN, "summary": "",
           "head": "", "stages": [], "tests_total": None, "tests_failed": None,
           "coverage_pct": None, "duration_ms": 0}

    def done(status, summary):
        res.update(status=status, summary=summary, duration_ms=int((time.monotonic() - started) * 1000))
        return record_run(res) if record else res

    if not repo:
        return done(UNKNOWN, f"unknown repo {repo_name!r}")
    if not (prof.get("build") or prof.get("test")):
        return done(UNKNOWN, "no health profile — nothing to run")
    timeout = int(prof.get("timeout") or DEFAULT_TIMEOUT)

    tmp = tempfile.mkdtemp(prefix="sisyphus-health-")
    wt = os.path.join(tmp, "src")
    try:
        ok, head, err = _clone(repo.get("source", ""), repo.get("branch") or "main", wt)
        if not ok:
            res["stages"].append(_stage("checkout", FAIL, "could not clone the branch", err))
            return done(UNKNOWN, f"could not check out {repo.get('branch') or 'main'}: {err[:200]}")
        res["head"] = head
        try:
            setup_hook.apply_setup(repo_name, wt)
        except setup_hook.SetupHookError as e:
            res["stages"].append(_stage("setup", FAIL, str(e)))
            return done(FAIL, f"setup hook failed: {e}")

        failed_before = False
        for name in STAGES:
            cmd = prof.get(name, "")
            if not cmd:
                continue
            if failed_before:
                res["stages"].append(_stage(name, SKIPPED, "an earlier stage failed", command=cmd))
                continue
            code, out, secs = _exec(cmd, wt, timeout)
            if name == "test":
                st = _test_stage(cmd, code, out, secs, wt, prof, timeout)
                res["tests_total"] = st.pop("_total", None)
                res["tests_failed"] = st.pop("_failed", None)
                res["tests_passed"] = st.pop("_passed", None)
            elif code == 0:
                st = _stage(name, OK, "", out, cmd, secs)
            elif code is None:
                st = _stage(name, FAIL, f"timed out after {timeout}s", out, cmd, secs)
            else:
                st = _stage(name, FAIL, _error_excerpt(out), out, cmd, secs)
            if name == "coverage" and st["status"] == OK:
                pct = _coverage_pct(wt, prof)
                if pct is not None:
                    res["coverage_pct"] = pct
                    st["detail"] = f"{pct}% of lines"
                else:
                    st.update(status=UNKNOWN, detail="ran, but no coverage report at " +
                              (prof.get("coverage_report") or "(coverage_report not set)"))
            res["stages"].append(st)
            # Coverage failing is not the build being broken: it does not stop
            # anything after it, and on its own it does not turn the card red.
            if st["status"] == FAIL and name != "coverage":
                failed_before = True

        # No separate coverage command: the test run itself may write the report
        # (pytest --cov-report=xml, a Gradle test task finalized by Kover). The
        # checkout is fresh, so a report found here is this run's — failing
        # tests included.
        if not prof.get("coverage") and prof.get("coverage_report"):
            res["coverage_pct"] = _coverage_pct(wt, prof)

        bad = [s for s in res["stages"] if s["status"] == FAIL and s["name"] != "coverage"]
        return done(FAIL if bad else OK, _summary(res))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_stage(cmd, code, out, secs, wt, prof, timeout) -> dict:
    counts = parse_junit(_paths(wt, prof.get("junit", ""))) if prof.get("junit") else None
    if code is None:
        st = _stage("test", FAIL, f"timed out after {timeout}s", out, cmd, secs)
    elif counts is None:
        st = _stage("test", OK if code == 0 else FAIL,
                    ("exit 0 — no JUnit report found, counts unknown" if code == 0
                     else _error_excerpt(out)), out, cmd, secs)
    else:
        failed = counts["failed"]
        detail = f"{counts['passed']} passed" + (f" · {failed} failed" if failed else "") + \
                 (f" · {counts['skipped']} skipped" if counts["skipped"] else "")
        if failed:
            detail += "\n" + "\n".join(counts["failed_names"])
        elif code != 0:
            detail += f" — but the command exited {code}\n" + _error_excerpt(out, 6)
        st = _stage("test", OK if (code == 0 and not failed) else FAIL, detail, out, cmd, secs)
        st["_total"], st["_failed"], st["_passed"] = counts["total"], failed, counts["passed"]
    return st


def _coverage_pct(wt: str, prof: dict) -> Optional[float]:
    for p in _paths(wt, prof.get("coverage_report", "")):
        pct = parse_coverage(p)
        if pct is not None:
            return pct
    return None


def _summary(res: dict) -> str:
    parts = []
    for s in res["stages"]:
        if s["name"] == "build":
            parts.append("builds" if s["status"] == OK else "build broken")
        elif s["name"] == "test" and s["status"] != SKIPPED:
            if res.get("tests_total") is not None:
                total, failed = res["tests_total"], res.get("tests_failed") or 0
                passed = res.get("tests_passed") or 0
                skipped = total - passed - failed
                parts.append(f"{failed} of {total} tests fail" if failed else
                             f"{passed} tests pass" + (f" · {skipped} skipped" if skipped else ""))
            else:
                parts.append("tests pass" if s["status"] == OK else "tests fail")
    if res.get("coverage_pct") is not None:
        parts.append(f"{res['coverage_pct']}% coverage")
    return " · ".join(parts)


# ── background + schedule ────────────────────────────────────────────────────

def running() -> dict:
    """{repo: seconds running} for the run in flight, if any."""
    return {r: int(time.monotonic() - t) for r, t in _running.items()}


def start(repo_name: str, trigger: str = "manual") -> bool:
    """Run in a background thread. False when a run is already in flight."""
    if not _lock.acquire(blocking=False):
        return False
    _running[repo_name] = time.monotonic()

    def _go():
        try:
            run(repo_name, trigger)
        except Exception:  # noqa: BLE001 — a crashed run must still release the lock
            logger.exception("health run for %s crashed", repo_name)
        finally:
            _running.pop(repo_name, None)
            _lock.release()

    threading.Thread(target=_go, name=f"health-{repo_name}", daemon=True).start()
    return True


def interval_hours() -> float:
    from .config import get_config
    try:
        return max(1.0, float(get_config("health_interval_hours", 24) or 24))
    except (TypeError, ValueError):
        return 24.0


def due(now: Optional[datetime] = None) -> List[str]:
    """Repos whose last run is older than the interval. A repo never run is not
    due: its first run is the human's, on the button."""
    from .repos import load_repos
    now = now or datetime.now(timezone.utc)
    limit = timedelta(hours=interval_hours())
    out = []
    for r in load_repos():
        name = r.get("name", "")
        if not profile(name):
            continue
        last = last_run(name)
        if last and now - datetime.fromisoformat(last["ran_at"]) >= limit:
            out.append(name)
    return out


async def health_forever(check_every: int = 3600) -> None:
    """Hourly: run every repo that is due, one at a time. No exception escapes."""
    from . import heartbeat
    heartbeat.register("health", every=check_every, note="daily build/test runs")
    while True:
        try:
            names = due()
            for name in names:
                await asyncio.to_thread(_run_locked, name)
            heartbeat.beat("health", note=(f"ran {', '.join(names)}" if names else "nothing due"))
        except Exception:  # noqa: BLE001
            logger.exception("health loop pass failed")
        await asyncio.sleep(check_every)


def _run_locked(name: str) -> None:
    with _lock:
        _running[name] = time.monotonic()
        try:
            run(name, "daily")
        finally:
            _running.pop(name, None)


# ── discovery ────────────────────────────────────────────────────────────────

def start_profiling(repo_name: str) -> dict:
    """A task pinned to the repo-profiler agent: it reads the repo and proposes
    a profile (propose_health_profile); the human confirms it on the repo page."""
    from . import store
    from .orchestrator import emit
    task = store.create_task(
        f"Work out how to check the health of repo {repo_name}: the commands that build it, "
        "run its unit tests with JUnit XML output, and measure coverage if the project "
        "already has a coverage tool. Propose them with propose_health_profile, then finish.",
        repo=repo_name, agent=PROFILER_AGENT, title=f"Health profile · {repo_name}")
    # Without the event the dispatcher never hears of the task: it would sit
    # "active" with an empty thread until the watchdog happened to notice.
    emit(task["id"], "created")
    return task


def profiling_task(repo_name: str) -> Optional[dict]:
    """The profiler task still in flight for *repo_name*, if any."""
    from . import store
    for t in store.list_tasks():
        if t.get("repo") == repo_name and t.get("agent") == PROFILER_AGENT \
                and t.get("status") in ("active", "waiting", "paused"):
            return t
    return None
