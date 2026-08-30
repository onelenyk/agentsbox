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


# ── the refusals: what the reaper does when it is NOT sure ───────────────

class TestTheReaperNeverCountsAnUnkilledProcessAsKilled:
    """`unverified` and `failed` are the module's honest "we could not
    determine" buckets, and the docstring promises they are never folded into
    `killed`. Nothing exercised either bucket's failure path — so the promise
    was the one part of the reaper with no test behind it."""

    def test_a_dry_run_reports_the_orphan_and_kills_nothing(self, reaper_env):
        worktree = reaper_env.root / "repos" / "dry"
        proc, argv = reaper_env.spawn(worktree)
        orphans.record(proc.pid, argv, worktree=str(worktree))
        orphans._OWNED.clear()
        assert {r["pid"]: r["verdict"] for r in orphans.scan()}[proc.pid] == "orphan", \
            "the dry run must be refusing to kill a real orphan, not a spared record"

        summary = orphans.reap(kill=False)

        assert summary["killed"] == []
        assert summary["unverified"] == [proc.pid]
        assert proc.poll() is None, "a dry run killed the process anyway"
        assert _records(reaper_env) == [f"{proc.pid}.json"], \
            "a dry run dropped the record it did not act on"

    def test_a_process_that_survives_the_kill_lands_in_failed(self, reaper_env, monkeypatch):
        worktree = reaper_env.root / "repos" / "stubborn"
        proc, argv = reaper_env.spawn(worktree)
        orphans.record(proc.pid, argv, worktree=str(worktree))
        orphans._OWNED.clear()
        monkeypatch.setattr(orphans, "_terminate", lambda pid, **kw: "survived")

        summary = orphans.reap()

        assert summary["failed"] == [proc.pid]
        assert summary["killed"] == []
        assert _records(reaper_env) == [f"{proc.pid}.json"], \
            "dropped the record of a process still running — the next boot could not see it"

    def test_a_kill_we_are_not_allowed_to_make_lands_in_failed(self, reaper_env, monkeypatch):
        worktree = reaper_env.root / "repos" / "notours"
        proc, argv = reaper_env.spawn(worktree)
        orphans.record(proc.pid, argv, worktree=str(worktree))
        orphans._OWNED.clear()
        monkeypatch.setattr(orphans, "_terminate", lambda pid, **kw: "denied")

        summary = orphans.reap()

        assert summary["failed"] == [proc.pid] and summary["killed"] == []

    def test_a_platform_we_cannot_read_argv_on_kills_nothing(self, reaper_env, monkeypatch):
        """Point 4 of the module docstring is the whole defence against pid
        reuse. Where argv cannot be read at all, every record is unverifiable
        — and unverifiable must mean left alone, not killed on the strength of
        the other four conditions."""
        worktree = reaper_env.root / "repos" / "plan9"
        proc, argv = reaper_env.spawn(worktree)
        orphans.record(proc.pid, argv, worktree=str(worktree))
        orphans._OWNED.clear()
        monkeypatch.setattr(orphans.platform, "system", lambda: "Plan9")

        verdicts = {r["pid"]: r["verdict"] for r in orphans.scan()}
        assert verdicts[proc.pid] == "unverified"

        summary = orphans.reap()
        assert summary["unverified"] == [proc.pid] and summary["killed"] == []
        assert proc.poll() is None


class TestTerminateReportsWhatActuallyHappened:
    """_terminate's return value is what decides `killed` vs `failed`, so each
    outcome is pinned rather than inferred from the happy path."""

    def test_a_process_already_gone_reads_as_gone(self, monkeypatch):
        def _lookup_error(pid, sig):
            raise ProcessLookupError()
        monkeypatch.setattr(orphans.os, "kill", _lookup_error)
        assert orphans._terminate(4242, grace=0) == "gone"

    def test_a_process_we_may_not_signal_reads_as_denied(self, monkeypatch):
        def _denied(pid, sig):
            raise PermissionError()
        monkeypatch.setattr(orphans.os, "kill", _denied)
        assert orphans._terminate(4242, grace=0) == "denied"

    def test_a_process_that_outlives_sigkill_reads_as_survived(self, monkeypatch):
        monkeypatch.setattr(orphans.os, "kill", lambda pid, sig: None)
        monkeypatch.setattr(orphans, "_alive", lambda pid: True)
        monkeypatch.setattr(orphans, "KILL_GRACE_SECONDS", 0)
        assert orphans._terminate(4242, grace=0) == "survived"


# ── reading another process's argv ───────────────────────────────────────

class TestReadingArgvIsNeverALicenceToKill:

    def test_an_unsupported_platform_says_so_by_name(self, monkeypatch):
        monkeypatch.setattr(orphans.platform, "system", lambda: "Plan9")
        argv, reason = orphans.read_argv(1234)
        assert argv is None and "Plan9" in reason

    def test_a_transient_failure_is_retried(self, monkeypatch):
        """KERN_PROCARGS2 returns EIO on roughly one read in three of a freshly
        spawned child (measured — see the comment above _ARGV_ATTEMPTS). A
        single attempt would file live orphans under "unverified" for a reason
        that isn't real."""
        calls = []

        def _flaky(pid):
            calls.append(pid)
            if len(calls) < 3:
                return None, "sysctl KERN_PROCARGS2 failed (errno 5)"
            return ["python3", "-c", "..."], ""

        monkeypatch.setattr(orphans.platform, "system", lambda: "Linux")
        monkeypatch.setattr(orphans, "_argv_linux", _flaky)
        monkeypatch.setattr(orphans, "_exists", lambda pid: True)
        monkeypatch.setattr(orphans, "_ARGV_RETRY_PAUSE", 0)

        argv, reason = orphans.read_argv(999)
        assert argv == ["python3", "-c", "..."] and reason == ""
        assert len(calls) == 3

    def test_exhausting_the_retries_names_the_last_reason_and_the_count(self, monkeypatch):
        monkeypatch.setattr(orphans.platform, "system", lambda: "Linux")
        monkeypatch.setattr(orphans, "_argv_linux", lambda pid: (None, "errno 5"))
        monkeypatch.setattr(orphans, "_exists", lambda pid: True)
        monkeypatch.setattr(orphans, "_ARGV_RETRY_PAUSE", 0)

        argv, reason = orphans.read_argv(999)
        assert argv is None
        assert "errno 5" in reason and str(orphans._ARGV_ATTEMPTS) in reason

    def test_a_reader_that_raises_is_a_reason_not_a_crash(self, monkeypatch):
        def _boom(pid):
            raise OSError("no such thing")
        monkeypatch.setattr(orphans.platform, "system", lambda: "Linux")
        monkeypatch.setattr(orphans, "_argv_linux", _boom)
        monkeypatch.setattr(orphans, "_exists", lambda pid: True)
        monkeypatch.setattr(orphans, "_ARGV_RETRY_PAUSE", 0)

        argv, reason = orphans.read_argv(999)
        assert argv is None and "OSError" in reason

    def test_a_process_that_exits_mid_read_is_gone_not_unverified(self, monkeypatch):
        """The distinction matters: "gone" drops the record, "unverified"
        keeps warning about a process that no longer exists."""
        monkeypatch.setattr(orphans.platform, "system", lambda: "Linux")
        monkeypatch.setattr(orphans, "_argv_linux", lambda pid: (None, "errno 5"))
        monkeypatch.setattr(orphans, "_exists", lambda pid: False)

        argv, reason = orphans.read_argv(999)
        assert argv is None and "exited" in reason


class TestTheLinuxArgvReader:
    """CI is macos-latest only, so /proc parsing has never run anywhere — the
    same blind spot the sandbox backend had. A fake /proc exercises it on any
    host."""

    @pytest.fixture
    def fake_proc(self, tmp_path, monkeypatch):
        root = tmp_path / "fakeroot"

        def _redirect(p):
            return root / str(p).lstrip("/")
        monkeypatch.setattr(orphans, "Path", _redirect)

        def write(pid, raw: bytes):
            d = root / "proc" / str(pid)
            d.mkdir(parents=True, exist_ok=True)
            (d / "cmdline").write_bytes(raw)
        return write

    def test_a_nul_separated_cmdline_becomes_argv(self, fake_proc):
        fake_proc(11, b"python3\x00-c\x00print(1)\x00")
        assert orphans._argv_linux(11) == (["python3", "-c", "print(1)"], "")

    def test_a_cmdline_without_a_trailing_nul_is_read_the_same(self, fake_proc):
        fake_proc(12, b"python3\x00-c")
        assert orphans._argv_linux(12) == (["python3", "-c"], "")

    def test_an_empty_cmdline_is_a_reason_not_an_empty_argv(self, fake_proc):
        """A kernel thread reads as empty. Returning [] would fingerprint to
        the hash of nothing, which every other argument-less process matches."""
        fake_proc(13, b"")
        argv, reason = orphans._argv_linux(13)
        assert argv is None and "empty" in reason

    def test_a_missing_process_names_the_path(self, fake_proc):
        argv, reason = orphans._argv_linux(9999)
        assert argv is None and "/proc/9999/cmdline" in reason


class TestAnUnreadableRecordIsDroppedNotCarried:
    """An unreadable record can never identify a process, so keeping it would
    only reproduce the same warning on every boot."""

    def test_garbage_and_pidless_records_are_deleted_and_the_good_one_kept(
            self, reaper_env, caplog):
        import logging
        d = reaper_env.root / "worker-pids"
        d.mkdir(parents=True, exist_ok=True)
        (d / "1.json").write_text("{not json")
        (d / "2.json").write_text(json.dumps({"binary": "opencode"}))   # no pid
        (d / "3.json").write_text(json.dumps({"pid": 3, "argv_sha256": "x"}))

        with caplog.at_level(logging.WARNING, logger="core.orphans"):
            recs = orphans._load_records()

        assert [r["pid"] for r in recs] == [3]
        assert _records(reaper_env) == ["3.json"]
        assert caplog.text.count("dropping unreadable worker record") == 2
