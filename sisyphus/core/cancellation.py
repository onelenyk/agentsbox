"""Cancellation registry — the missing wire for stopping a running worker.

``launcher._monitored_communicate`` already implements cancellation correctly:
it waits on a ``cancel_event``, terminates the subprocess, and returns status
"cancelled". The parameter is threaded through every layer down to it — and no
caller has ever filled it in, so it is always None. Stopping work therefore
meant finding the pid by hand and killing it, which leaves a ``worker_started``
with no ``worker_finished`` and makes the task unrunnable until a restart.

This module is the register half. A worker publishes its event here while it
runs; a human (or delete) sets it; the launcher's existing loop does the rest,
and spawn_worker's normal "ended without reporting" path closes the thread
marker. Nothing needs to kill a pid.

In memory on purpose, like sessions: an event only means anything to the
process that owns the subprocess. A daemon restart makes it moot for a fresh
worker — and for one orphaned by that restart, orchestrator.find_abandoned is
the recovery path.
"""
import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_EVENTS: dict = {}


def register(task_id: str, role: str = "") -> asyncio.Event:
    """The cancel event for a worker about to run, creating it if needed.

    Returns any event already registered for this (task, role) rather than a
    fresh one, so two callers share a single event. That is what lets the
    decision site claim it synchronously — before the background task is even
    created — while spawn_worker still asks for it by name later. Handing back a
    new event here would reopen the window: a Stop landing in between would set
    an event nobody is listening to.
    """
    existing = (_EVENTS.get(task_id) or {}).get(role)
    if existing is not None:
        return existing
    event = asyncio.Event()
    _EVENTS.setdefault(task_id, {})[role] = event
    return event


def unregister(task_id: str, role: str = "") -> None:
    roles = _EVENTS.get(task_id)
    if not roles:
        return
    roles.pop(role, None)
    if not roles:
        _EVENTS.pop(task_id, None)


def request(task_id: str) -> int:
    """Ask every worker on *task_id* to stop. Returns how many were signalled.

    Zero means nothing was running — the caller still has to park the task, or
    a "stop" on an idle task would silently do nothing.
    """
    roles = _EVENTS.get(task_id) or {}
    for role, event in roles.items():
        logger.info("cancellation: signalling %s worker on %s", role or "?", task_id)
        event.set()
    return len(roles)


def is_running(task_id: str) -> bool:
    return bool(_EVENTS.get(task_id))


def active_tasks() -> list:
    return sorted(_EVENTS)


def clear() -> None:
    _EVENTS.clear()
