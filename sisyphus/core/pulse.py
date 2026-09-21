"""One cheap question the whole UI can ask: has anything changed?

Every live surface used to fetch itself on a timer — the board every 7
seconds, a task every 3, the dashboard every 5 — whether or not anything had
happened. On this daemon the board fragment is ~135 KB, so an open tab pulled
about 70 MB an hour to render, almost always, exactly what was already on the
screen. The waste is the least of it: every one of those refreshes replaced
live DOM, which is what made the page feel restless.

`version()` is three counted reads on one connection plus the in-memory
session table, and the client only re-fetches a surface when the string
changes. While a worker is actually running the version deliberately ticks
on its own (see below), because that is the one time a page should keep
redrawing without anything being written to the database.
"""
import time

from core.db import get_conn

# How often the version moves while a worker is live. It matches the interval
# the detail page used to poll at: the "last output Ns ago" line and elapsed
# minutes are clock-driven, not database-driven, and they are only worth
# redrawing while somebody is watching a run.
LIVE_TICK_SECONDS = 3


def version() -> str:
    """A fingerprint of everything the live surfaces render."""
    from core import sessions

    conn = get_conn()
    try:
        tasks = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(MAX(updated_at), '') AS t FROM tasks").fetchone()
        msgs = conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM messages").fetchone()
        mrs = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(MAX(updated_at), '') AS t FROM merge_requests").fetchone()
    finally:
        conn.close()

    live = sessions.list_active()
    # A clock component ONLY while something is running, so a live run keeps
    # the page moving and an idle daemon lets it rest completely.
    tick = int(time.time() // LIVE_TICK_SECONDS) if live else 0
    return f"{tasks['n']}.{tasks['t']}.{msgs['n']}.{mrs['n']}.{mrs['t']}.{len(live)}.{tick}"
