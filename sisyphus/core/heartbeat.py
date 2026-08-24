"""Watcher heartbeats — what each of the daemon's background loops last did.

In-memory for the same reason core.sessions is (read its docstring): a heartbeat
means something only while the process that beats it is alive. Persisted, it
would outlive its process and lie after a crash — the daemon would come back up
and show a watchdog that "last ran 30 seconds ago" when nothing has run at all.
An empty registry after a restart is the honest answer, and every loop
re-registers itself the moment it starts.

Two kinds, because the loops genuinely differ:

  periodic   ticks on its own clock (watchdog, autopilot, forge poll). Silence
             past its own period is evidence, so the state decays: live → late →
             stale.
  event      wakes only when something arrives — orchestrator.dispatch_forever
             blocks on queue.get() and has no tick at all. Quiet is its CORRECT
             state, so it never decays; what it reports instead is that it
             entered its loop, what it last handled, and how deep its queue is.

The distinction is the whole point of this module. A single "last seen" clock
applied to both would paint the dispatcher yellow every time the board was
calm, which is exactly when it is working as designed.
"""
from datetime import datetime, timezone
from typing import Optional

_WATCHERS: dict = {}

# A periodic watcher gets two of its own periods before it reads late, and six
# before it reads stale — one slow pass is not a fault, six is.
LIVE_PERIODS = 2
LATE_PERIODS = 6


def _now() -> datetime:
    return datetime.now(timezone.utc)


def register(name: str, kind: str = "periodic", every: int = 0, note: str = "") -> None:
    """Announce a watcher before its first beat, so the UI can show a loop that
    started but has not yet had anything to do. Re-registering keeps whatever
    beats have already landed — a restart of one loop is not a reason to forget
    its history within this process."""
    prev = _WATCHERS.get(name, {})
    _WATCHERS[name] = {
        **prev,
        "name": name,
        "kind": kind,
        "every": int(every or 0),
        "note": note or prev.get("note", ""),
        "last_beat": prev.get("last_beat"),
        "next_due": prev.get("next_due", ""),
        "beats": prev.get("beats", 0),
        "off_reason": None,
        "registered_at": prev.get("registered_at") or _now().isoformat(),
    }


def beat(name: str, note: str = "", next_due: str = "") -> None:
    """One pass happened. Safe to call from inside a loop's own try/except — an
    unregistered name registers itself rather than raising, because a heartbeat
    must never be able to break the loop it observes."""
    if name not in _WATCHERS:
        register(name)
    w = _WATCHERS[name]
    w["last_beat"] = _now().isoformat()
    w["beats"] = w.get("beats", 0) + 1
    w["off_reason"] = None
    if note:
        w["note"] = note
    w["next_due"] = next_due or ""


def mark_off(name: str, reason: str = "") -> None:
    """The loop is deliberately not running (disabled by config). Distinct from
    dead: nothing is wrong, and the UI must not raise an alarm about it."""
    if name not in _WATCHERS:
        register(name)
    _WATCHERS[name]["off_reason"] = reason or "disabled"


def _age(w: dict, now: datetime) -> Optional[float]:
    if not w.get("last_beat"):
        return None
    try:
        return (now - datetime.fromisoformat(w["last_beat"])).total_seconds()
    except ValueError:
        return None


def state(w: dict, now: Optional[datetime] = None) -> str:
    """One of: off, never, live, late, stale."""
    now = now or _now()
    if w.get("off_reason"):
        return "off"
    if w.get("kind") == "event":
        # It entered its loop and is waiting to be woken. There is no elapsed
        # time that makes that suspicious.
        return "live"
    age = _age(w, now)
    if age is None:
        return "never"
    every = w.get("every") or 0
    if every <= 0:
        return "live"          # no declared period — a beat is all we can ask
    if age <= every * LIVE_PERIODS:
        return "live"
    if age <= every * LATE_PERIODS:
        return "late"
    return "stale"


def list_all(now: Optional[datetime] = None) -> list:
    """Every registered watcher with its derived state, in registration order."""
    now = now or _now()
    out = []
    for w in _WATCHERS.values():
        age = _age(w, now)
        out.append({**w, "state": state(w, now),
                    "age_s": int(age) if age is not None else None})
    return sorted(out, key=lambda w: w.get("registered_at", ""))


def clear() -> None:
    """Test helper — forget every watcher."""
    _WATCHERS.clear()
