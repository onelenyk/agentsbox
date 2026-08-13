"""The layer that actually kills a worker, exercised against real processes.

``launcher._monitored_communicate`` is what terminates `opencode` when a human
presses Stop, and what enforces the total and idle timeouts. Everything above it
— the registry, the endpoint, the guards — is unit-tested; this was the one part
confirmed only by watching `pgrep` go empty by hand, which is no use when it
breaks at 3am.

These spawn real subprocesses (`sleep`, a chatty python loop) because a mock
cannot show that the process is gone. They stay under a couple of seconds.
"""
import asyncio
import os
import signal

import pytest

from core.launcher import _monitored_communicate


async def _spawn(*args):
    return await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class TestCancellation:
    @pytest.mark.asyncio
    async def test_setting_the_event_kills_the_process(self):
        proc = await _spawn("sleep", "60")
        pid = proc.pid
        cancel = asyncio.Event()

        async def _stop_it():
            await asyncio.sleep(0.2)
            cancel.set()

        asyncio.create_task(_stop_it())
        status, err, out, errout = await _monitored_communicate(
            proc, timeout=30, idle_timeout=30, cancel_event=cancel)

        assert status == "cancelled"
        await asyncio.sleep(0.1)
        assert not _alive(pid), "the subprocess outlived its cancellation"

    @pytest.mark.asyncio
    async def test_output_produced_before_the_stop_is_kept(self):
        """A cancelled run is not a lost one — whatever the agent managed to say
        still reaches the caller, and from there the thread."""
        proc = await _spawn(
            "python3", "-u", "-c",
            "print('partial work'); import time; time.sleep(60)")
        cancel = asyncio.Event()

        async def _stop_it():
            await asyncio.sleep(0.4)
            cancel.set()

        asyncio.create_task(_stop_it())
        status, err, out, _ = await _monitored_communicate(
            proc, timeout=30, idle_timeout=30, cancel_event=cancel)

        assert status == "cancelled"
        assert b"partial work" in out

    @pytest.mark.asyncio
    async def test_an_unset_event_does_not_disturb_a_normal_run(self):
        proc = await _spawn("python3", "-u", "-c", "print('done')")
        cancel = asyncio.Event()          # never set
        status, err, out, _ = await _monitored_communicate(
            proc, timeout=30, idle_timeout=30, cancel_event=cancel)
        assert status == "completed"
        assert b"done" in out

    @pytest.mark.asyncio
    async def test_no_event_at_all_still_works(self):
        """The pre-cancellation call shape — every caller passed None for years."""
        proc = await _spawn("python3", "-u", "-c", "print('hi')")
        status, _, out, _ = await _monitored_communicate(
            proc, timeout=30, idle_timeout=30, cancel_event=None)
        assert status == "completed" and b"hi" in out


class TestTimeouts:
    @pytest.mark.asyncio
    async def test_total_timeout_kills_the_process(self):
        proc = await _spawn("sleep", "60")
        pid = proc.pid
        status, err, _, _ = await _monitored_communicate(
            proc, timeout=1, idle_timeout=30, cancel_event=None)

        assert status == "timeout"
        assert "1s timeout" in (err or "")
        await asyncio.sleep(0.1)
        assert not _alive(pid)

    @pytest.mark.asyncio
    async def test_idle_timeout_fires_on_a_silent_process(self):
        proc = await _spawn("sleep", "60")
        pid = proc.pid
        status, err, _, _ = await _monitored_communicate(
            proc, timeout=30, idle_timeout=1, cancel_event=None)

        assert status == "timeout"
        assert "idle" in (err or "")
        await asyncio.sleep(0.1)
        assert not _alive(pid)

    @pytest.mark.asyncio
    async def test_a_chatty_process_is_not_killed_by_the_idle_timeout(self):
        """The false positive that matters for real work: a Gradle build talks
        for minutes without finishing, and must not be mistaken for a hang."""
        proc = await _spawn(
            "python3", "-u", "-c",
            "import time\n"
            "for _ in range(12):\n"
            "    print('still working'); time.sleep(0.1)\n")
        status, _, out, _ = await _monitored_communicate(
            proc, timeout=30, idle_timeout=1, cancel_event=None)

        assert status == "completed", "output should have kept the idle timer alive"
        assert out.count(b"still working") == 12
