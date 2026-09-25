"""Shared Seatbelt-sandbox detection for process-tier tests.

A reviewer runs the suite inside the worker's Seatbelt sandbox, where `ps` is
blocked (Operation not permitted — the same limitation documented in
tests/test_daemon_ctl.py). Signals still deliver to a child, but a killed
child's corpse is invisible to ps-based probes: `_alive` keeps answering True
for a process that is already dead, so kill-path assertions report "survived"
for a kill that worked. That is environmental, not a product bug — the daemon
itself runs unsandboxed and observes deaths normally.

The probe is behavioural, not a flag or a name: spawn a child, SIGKILL it,
keep the zombie (do not wait), and ask core.orphans._zombie whether it can see
the corpse. If it cannot, no test that depends on observing a killed process
can be trusted in this environment, and it must skip with an honest reason.
"""
import signal
import subprocess
import sys
import time

import pytest

from core import orphans

_SLEEPER = "import time,sys; time.sleep(float(sys.argv[1]))"


def kill_path_observable() -> bool:
    """Whether this environment can *observe* a killed child's death — probed,
    not assumed. Runs once per test process (at module import)."""
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
    not kill_path_observable(),
    reason="sandboxed: ps is blocked (Seatbelt), a killed child's death cannot be observed here",
)
