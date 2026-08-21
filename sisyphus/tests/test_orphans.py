"""Reaping worker subprocesses that outlived the daemon (core.orphans).

These tests spawn real child processes and read their real argv — the whole
mechanism is about what the operating system will tell us about a process we
no longer own, so faking that away would test nothing.
"""
import json
import os
import pathlib
import platform
import signal
import subprocess
import sys
import time

import pytest

from core import orphans, sessions

# A child that sits still until killed. Its extra argv elements are what the
# fingerprint is computed over.
_SLEEPER = "import time,sys; time.sleep(float(sys.argv[1]))"


def _kill_path_observable() -> bool:
    """Whether this environment can *observe* a killed child's death — probed,
    not assumed.

    A reviewer runs the suite inside the worker's Seatbelt sandbox, where `ps`
    is blocked (the same limitation behind test_daemon_ctl's known skip).
    Signals still deliver, but the corpse is invisible: `_zombie` cannot see
    the state, so `_alive` keeps answering True and `_terminate` reports
    "survived" for a process that is already dead. The two kill-path tests
    then fail for environmental reasons and every review ends with a human
    adjudicating failures that have nothing to do with the diff. Probe once
    per run: kill a child, keep the zombie (no wait), ask _zombie about it."""
    proc = subprocess.Popen([sys.executable, "-c", _SLEEPER, "30"])
    try:
        proc.send_signal(signal.SIGKILL)
        deadline = time.time() + 3
        while time.time() < deadline:      # do NOT wait() — the zombie IS the probe
            if orphans._zombie(proc.pid):
                return True
            time.sleep(0.1)
        return False
    finally:
        try:
            proc.wait(timeout=3)
        except Exception:
            pass


needs_kill = pytest.mark.skipif(
    not _kill_path_observable(),
    reason="sandboxed: ps is blocked (Seatbelt), a killed child's death cannot be observed here",
)


@pytest.fixture
def reaper_env(tmp_path, monkeypatch):
    """Isolated WORKSPACE_ROOT, empty ownership/session state, and a killer for
    every child a test spawns."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("WORKSPACE_ROOT", str(ws))
    orphans._OWNED.clear()
    sessions.clear()
    spawned = []

    class Env:
        root = ws

        def spawn(self, marker_path, seconds=60):
            """A live process whose argv contains *marker_path*, plus the argv
            list it was spawned with (argv[0] excluded from fingerprinting, so
            the caller can hand this straight to orphans.record)."""
            argv = [sys.executable, "-c", _SLEEPER, str(seconds), str(marker_path)]
            p = subprocess.Popen(argv)
            spawned.append(p)
            # argv is only readable once the exec has happened.
            deadline = time.time() + 5
            while time.time() < deadline:
                if orphans.read_argv(p.pid)[0] is not None:
                    break
                time.sleep(0.05)
            return p, argv

    yield Env()

    for p in spawned:
        try:
            p.kill()
            p.wait(timeout=5)
        except Exception:
            pass
    orphans._OWNED.clear()
    sessions.clear()


def _records(env):
    return sorted(p.name for p in (env.root / "worker-pids").glob("*.json"))


def _wait_dead(proc, timeout=10):
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


# ── the case this module exists for ─────────────────────────────────────

@needs_kill
def test_reap_kills_a_worker_no_session_accounts_for(reaper_env):
    """The 2026-08-12 failure: the daemon restarted, the session dict died with
    it, the subprocess kept running."""
    worktree = reaper_env.root / "repos" / "wt-1"
    proc, argv = reaper_env.spawn(worktree)
    orphans.record(proc.pid, argv, worktree=str(worktree),
                   output_dir=str(reaper_env.root / "runs" / "t1"), step_id="t1-engineer")
    assert _records(reaper_env) == [f"{proc.pid}.json"]

    orphans._OWNED.clear()   # a restart: nothing in memory survives

    summary = orphans.reap()

    assert summary["killed"] == [proc.pid]
    assert summary["unverified"] == [] and summary["failed"] == []
    assert _wait_dead(proc), "orphaned worker was reported killed but is still running"
    assert _records(reaper_env) == []


def test_reap_spares_a_worker_a_live_session_backs(reaper_env):
    worktree = reaper_env.root / "repos" / "wt-2"
    proc, argv = reaper_env.spawn(worktree)
    orphans.record(proc.pid, argv, worktree=str(worktree))
    orphans._OWNED.clear()
    sessions.mint("task-2", "engineer", worktree=str(worktree))

    summary = orphans.reap()

    assert summary["killed"] == [] and summary["session"] == 1
    assert proc.poll() is None, "killed a worker that a live session accounts for"
    assert _records(reaper_env) == [f"{proc.pid}.json"]


def test_reap_spares_a_worker_this_daemon_is_still_running(reaper_env):
    """No restart happened: the launcher is monitoring this pid right now, and
    its session may already have been closed by the agent's own finish() call."""
    worktree = reaper_env.root / "repos" / "wt-3"
    proc, argv = reaper_env.spawn(worktree)
    orphans.record(proc.pid, argv, worktree=str(worktree))   # leaves pid in _OWNED

    summary = orphans.reap()

    assert summary["killed"] == [] and summary["owned"] == 1
    assert proc.poll() is None, "killed a worker this process is still monitoring"


# ── conservatism: never kill what we cannot prove is ours ───────────────

def test_recycled_pid_is_not_killed(reaper_env):
    """The record's pid is alive, but running something else — the operating
    system reused the pid after our worker exited. Killing it would take out a
    stranger's process."""
    victim = reaper_env.root / "not-ours"
    proc, argv = reaper_env.spawn(victim)
    orphans.record(proc.pid, argv, worktree=str(reaper_env.root / "repos" / "wt-4"))
    # Rewrite the fingerprint: same pid, different command line.
    rec_path = reaper_env.root / "worker-pids" / f"{proc.pid}.json"
    rec = json.loads(rec_path.read_text())
    rec["argv_sha256"] = orphans.fingerprint(["x", "--something-else"])
    rec_path.write_text(json.dumps(rec))
    orphans._OWNED.clear()

    summary = orphans.reap()

    assert summary["killed"] == []
    assert proc.poll() is None, "killed a process whose argv is not the one we spawned"
    assert summary["stale"] == 1
    assert _records(reaper_env) == []   # our worker is gone; the record is dead weight


def test_unreadable_argv_is_reported_loudly_and_not_killed(reaper_env, monkeypatch, caplog):
    """"We could not determine" must never look like a clean sweep."""
    worktree = reaper_env.root / "repos" / "wt-5"
    proc, argv = reaper_env.spawn(worktree)
    orphans.record(proc.pid, argv, worktree=str(worktree))
    orphans._OWNED.clear()
    monkeypatch.setattr(orphans, "read_argv",
                        lambda pid: (None, "sysctl KERN_PROCARGS2 failed (errno 1)"))

    with caplog.at_level("WARNING"):
        summary = orphans.reap()

    assert summary["killed"] == []
    assert summary["unverified"] == [proc.pid]
    assert proc.poll() is None
    assert _records(reaper_env) == [f"{proc.pid}.json"]   # still reapable next boot
    assert "NOT confirmed ours" in caplog.text, "an unverifiable live worker was not reported"


def test_process_with_no_arguments_never_matches(reaper_env):
    """A record and a process that both have an empty argv[1:] share the hash of
    the empty string. The "argv must name our own paths" check is what stops
    that from licensing a kill."""
    p = subprocess.Popen(["/bin/sh"], stdin=subprocess.PIPE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(0.3)
        orphans.record(p.pid, ["/bin/sh"], worktree=str(reaper_env.root / "repos" / "wt-6"))
        orphans._OWNED.clear()

        summary = orphans.reap()

        assert summary["killed"] == []
        assert summary["unverified"] == [p.pid]
        assert p.poll() is None, "killed an argument-less process on an empty-string hash match"
    finally:
        p.kill()
        p.wait(timeout=5)


@needs_kill
def test_release_of_a_process_that_would_not_die_keeps_it_reapable(reaper_env):
    """The launcher's own SIGTERM→SIGKILL can fail. Releasing such a pid must
    leave the record on disk — that is the only remaining trace of it."""
    worktree = reaper_env.root / "repos" / "wt-leak"
    proc, argv = reaper_env.spawn(worktree)
    orphans.record(proc.pid, argv, worktree=str(worktree))
    orphans.release(proc.pid, still_running=True)

    assert proc.pid not in orphans._OWNED
    assert _records(reaper_env) == [f"{proc.pid}.json"]
    assert orphans.reap()["killed"] == [proc.pid]
    assert _wait_dead(proc)


def test_record_for_an_already_dead_pid_is_dropped(reaper_env):
    proc, argv = reaper_env.spawn(reaper_env.root / "repos" / "wt-7", seconds=60)
    orphans.record(proc.pid, argv, worktree=str(reaper_env.root / "repos" / "wt-7"))
    proc.kill()
    proc.wait(timeout=5)
    orphans._OWNED.clear()

    summary = orphans.reap()

    assert summary["killed"] == [] and summary["stale"] == 1
    assert _records(reaper_env) == []


def test_unreadable_record_is_dropped_not_acted_on(reaper_env, caplog):
    d = reaper_env.root / "worker-pids"
    d.mkdir(parents=True)
    (d / "junk.json").write_text("{not json")
    with caplog.at_level("WARNING"):
        summary = orphans.reap()
    assert summary["records"] == 0 and summary["killed"] == []
    assert _records(reaper_env) == []


def test_reap_on_a_clean_machine_is_a_no_op(reaper_env):
    summary = orphans.reap()
    assert summary == {"records": 0, "killed": [], "failed": [], "unverified": [],
                       "owned": 0, "session": 0, "stale": 0}


# ── the OS-level primitive the whole design rests on ────────────────────

@pytest.mark.skipif(platform.system() not in ("Darwin", "Linux"),
                    reason="argv of another process is read per-platform")
def test_read_argv_returns_exactly_what_we_spawned(reaper_env):
    marker = reaper_env.root / "repos" / "wt-argv"
    proc, argv = reaper_env.spawn(marker)
    live, reason = orphans.read_argv(proc.pid)
    assert live is not None, reason
    # argv[0] is excluded from the fingerprint precisely because it is not
    # stable: /usr/bin/python3 re-execs itself as …/Python.app/…/Python.
    assert live[1:] == argv[1:]
    assert orphans.fingerprint(live) == orphans.fingerprint(argv)


def test_read_argv_reports_a_reason_for_a_dead_pid(reaper_env):
    proc, _ = reaper_env.spawn(reaper_env.root / "gone")
    proc.kill()
    proc.wait(timeout=5)
    live, reason = orphans.read_argv(proc.pid)
    assert live is None and reason, "a failed read must say why"


# ── launcher bookkeeping ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_launcher_records_the_post_exec_argv_and_releases_it(db_path, monkeypatch):
    """The launcher must fingerprint the runner's own argv, not the sandbox-exec
    wrapper it actually spawns (sandbox-exec replaces itself with the target),
    and must leave no record behind for a process that finished."""
    from core import sandbox
    from core.launcher import launch_agent
    from core.runners import init_runners_table, add_runner

    monkeypatch.setenv("SISYPHUS_SANDBOX", "0")
    # Stand in for sandbox-exec with another exec-replacing wrapper: `env`
    # behaves the same way (the live process's argv is the target's), and
    # unlike sandbox-exec it can nest inside the worker sandbox this suite may
    # itself be running in — see the note in tests/test_launcher.py.
    monkeypatch.setattr(sandbox, "enabled", lambda: True)
    monkeypatch.setattr(sandbox, "wrap", lambda args, wt, od: ["/usr/bin/env", *args])
    orphans._OWNED.clear()

    ws = pathlib.Path(os.environ["WORKSPACE_ROOT"])
    worktree = ws / "worktree-orphans"
    worktree.mkdir(parents=True, exist_ok=True)
    # `python3 run …` — the shim swallows the launcher's own flags.
    # Alive long enough for the reaper's own argv read to see it running.
    (worktree / "run").write_text("import sys, time\nprint('{}')\nsys.stdout.flush()\ntime.sleep(2)\n")
    output_dir = ws / "runs" / "run-orphans" / "resources" / "step"
    output_dir.mkdir(parents=True, exist_ok=True)

    seen = {}
    real_record = orphans.record

    def spy(pid, argv, **kw):
        seen["argv"] = list(argv)
        seen["kw"] = kw
        want = orphans.fingerprint(argv)
        deadline = time.time() + 5
        while time.time() < deadline:      # wait out the wrapper's exec
            live, _ = orphans.read_argv(pid)
            if live and orphans.fingerprint(live) == want:
                seen["live"] = live
                break
            time.sleep(0.05)
        return real_record(pid, argv, **kw)

    monkeypatch.setattr(orphans, "record", spy)

    init_runners_table()
    add_runner("mock-orphans", "python3")
    result = await launch_agent({"name": "T", "runner": "mock-orphans"},
                                "a prompt", str(worktree), str(output_dir),
                                timeout=30, step_id="step")

    assert result.status == "completed", result.error
    assert seen, "launcher spawned a worker without recording its pid"
    assert "/usr/bin/env" not in seen["argv"], \
        "recorded the wrapper's argv, which no live process ever has"
    assert seen["argv"][0].endswith("python3") and seen["argv"][1] == "run"
    assert "a prompt" in seen["argv"]
    # The point of the whole exercise: what we wrote down is what the OS
    # reports for the process that is actually running.
    assert seen.get("live"), "the recorded fingerprint never matched the live process's argv"
    assert seen["kw"]["worktree"] == str(worktree)
    assert _records_in(ws) == [], "a finished worker left a spawn record behind"


def _records_in(ws):
    return sorted(p.name for p in (pathlib.Path(ws) / "worker-pids").glob("*.json"))
