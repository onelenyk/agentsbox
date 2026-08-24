"""Tests for core/heartbeat.py — the state a watcher led is derived from.

The state machine is the part that can lie, so it is tested directly with an
injected clock rather than through the page that renders it.
"""
from datetime import datetime, timedelta, timezone

import pytest

from core import heartbeat


@pytest.fixture(autouse=True)
def _clean():
    heartbeat.clear()
    yield
    heartbeat.clear()


def _only(now=None):
    rows = heartbeat.list_all(now=now)
    assert len(rows) == 1
    return rows[0]


def _now():
    return datetime.now(timezone.utc)


def test_a_registered_watcher_that_never_ran_says_so():
    heartbeat.register("watchdog", every=60)
    w = _only()
    assert w["state"] == "never" and w["age_s"] is None and w["beats"] == 0


def test_a_periodic_watcher_decays_live_late_stale():
    heartbeat.register("watchdog", every=60)
    heartbeat.beat("watchdog", note="nothing stalled")
    t0 = _now()
    assert _only(t0)["state"] == "live"
    assert _only(t0 + timedelta(seconds=90))["state"] == "live"    # one slow pass
    assert _only(t0 + timedelta(seconds=200))["state"] == "late"
    assert _only(t0 + timedelta(seconds=1000))["state"] == "stale"


def test_an_event_driven_watcher_stays_live_while_idle():
    """The regression that matters: dispatch_forever blocks on its queue and has
    no tick, so quiet is its correct state. An hour of silence is not a fault."""
    heartbeat.register("dispatcher", kind="event")
    heartbeat.beat("dispatcher", note="last: created · task-1")
    assert _only(_now() + timedelta(hours=1))["state"] == "live"


def test_an_event_driven_watcher_is_live_before_its_first_event():
    heartbeat.register("dispatcher", kind="event")
    assert _only()["state"] == "live"      # it entered the loop; nothing arrived


def test_off_is_not_dead():
    heartbeat.register("forge poll", every=300)
    heartbeat.mark_off("forge poll", "forge_poll.enabled is false")
    w = _only(_now() + timedelta(days=1))
    assert w["state"] == "off"
    assert w["off_reason"] == "forge_poll.enabled is false"


def test_a_beat_ends_the_off_state():
    heartbeat.register("forge poll", every=300)
    heartbeat.mark_off("forge poll", "disabled")
    heartbeat.beat("forge poll", note="checked 3")
    w = _only()
    assert w["state"] == "live" and w["off_reason"] is None and w["note"] == "checked 3"


def test_a_beat_from_an_unregistered_loop_never_raises():
    """A heartbeat must not be able to break the loop it observes."""
    heartbeat.beat("mystery", note="ran")
    assert _only()["state"] == "live"


def test_reregistering_keeps_the_beats_already_recorded():
    heartbeat.register("watchdog", every=60)
    heartbeat.beat("watchdog")
    heartbeat.register("watchdog", every=120)
    w = _only()
    assert w["beats"] == 1 and w["every"] == 120


def test_a_watcher_with_no_declared_period_is_live_once_it_beats():
    heartbeat.register("odd", every=0)
    heartbeat.beat("odd")
    assert _only(_now() + timedelta(days=7))["state"] == "live"


def test_next_due_and_note_survive_on_the_row():
    heartbeat.register("autopilot", every=30)
    heartbeat.beat("autopilot", note="fired 1", next_due="2026-08-24T03:00:00+00:00")
    w = _only()
    assert w["note"] == "fired 1" and w["next_due"].startswith("2026-08-24")


def test_list_all_is_ordered_by_registration():
    for name in ("dispatcher", "watchdog", "autopilot"):
        heartbeat.register(name)
    assert [w["name"] for w in heartbeat.list_all()] == ["dispatcher", "watchdog", "autopilot"]
