"""Keeping the runner's session store to a size — core/runner_store.py.

The store is opencode's, not ours, so the tests build a database with its
shape (the parts that matter: the cascades, and the event log that hangs off
event_sequence rather than off session) and prove the pruner walks them.
"""
import sqlite3
import time

import pytest

from core import runner_store

SCHEMA = """
CREATE TABLE session (
  id text PRIMARY KEY, title text NOT NULL,
  time_created integer NOT NULL, time_updated integer NOT NULL);
CREATE TABLE message (
  id text PRIMARY KEY, session_id text NOT NULL, data text NOT NULL,
  CONSTRAINT fk_m FOREIGN KEY (session_id) REFERENCES session(id) ON DELETE CASCADE);
CREATE TABLE part (
  id text PRIMARY KEY, message_id text NOT NULL, session_id text NOT NULL, data text NOT NULL,
  CONSTRAINT fk_p FOREIGN KEY (message_id) REFERENCES message(id) ON DELETE CASCADE);
CREATE TABLE event_sequence (aggregate_id text PRIMARY KEY, seq integer NOT NULL);
CREATE TABLE event (
  id text PRIMARY KEY, aggregate_id text NOT NULL, data text NOT NULL,
  CONSTRAINT fk_e FOREIGN KEY (aggregate_id) REFERENCES event_sequence(aggregate_id) ON DELETE CASCADE);
"""


def _store(tmp_path, sessions):
    """sessions: [(id, age_days)] — builds the store under a workspace root."""
    path = runner_store.store_path(str(tmp_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    now = time.time()
    for sid, age in sessions:
        at = int((now - age * 86400) * 1000)
        conn.execute("INSERT INTO session VALUES (?,?,?,?)", (sid, f"run {sid}", at, at))
        conn.execute("INSERT INTO message VALUES (?,?,?)", (f"m-{sid}", sid, "x" * 200))
        conn.execute("INSERT INTO part VALUES (?,?,?,?)", (f"p-{sid}", f"m-{sid}", sid, "y" * 5000))
        conn.execute("INSERT INTO event_sequence VALUES (?,?)", (sid, 1))
        conn.execute("INSERT INTO event VALUES (?,?,?)", (f"e-{sid}", sid, "z" * 5000))
    conn.commit()
    conn.close()
    return path


@pytest.fixture(autouse=True)
def _no_live_workers(monkeypatch):
    from core import sessions
    monkeypatch.setattr(sessions, "list_active", lambda *a, **kw: [])


def _rows(path, table):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


class TestOldSessionsGo:
    def test_only_what_is_older_than_the_cutoff(self, tmp_path):
        path = _store(tmp_path, [("ses_old", 30), ("ses_week", 8), ("ses_today", 0)])
        res = runner_store.prune(keep_days=7, root=str(tmp_path))
        assert res["removed"] == 2 and res["kept"] == 1
        assert _rows(path, "session") == 1

    def test_a_session_takes_its_messages_parts_and_events_with_it(self, tmp_path):
        path = _store(tmp_path, [("ses_old", 30), ("ses_today", 0)])
        runner_store.prune(keep_days=7, root=str(tmp_path))
        for table in ("message", "part", "event", "event_sequence"):
            assert _rows(path, table) == 1, table

    def test_the_file_actually_shrinks(self, tmp_path):
        path = _store(tmp_path, [(f"ses_{i}", 30) for i in range(80)] + [("ses_now", 0)])
        before = path.stat().st_size
        res = runner_store.prune(keep_days=7, root=str(tmp_path))
        assert res["removed"] == 80
        assert path.stat().st_size < before and res["freed"] > 0

    def test_keeping_nothing_takes_every_session_there_is(self, tmp_path):
        """keep_days=0 means keep nothing. It is reachable only by hand: the
        daemon loop reads 0 as "pruning off" and never calls this. Both rows
        are seconds old, never "this same millisecond" — a session written in
        the millisecond the cutoff lands on is not older than it, which made
        this test a coin flip."""
        path = _store(tmp_path, [("ses_a_minute_ago", 60.0 / 86400), ("ses_seconds_ago", 5.0 / 86400)])
        res = runner_store.prune(keep_days=0, root=str(tmp_path))
        assert res["removed"] == 2 and _rows(path, "session") == 0


class TestItNeverGetsInTheWay:
    def test_a_running_worker_defers_the_prune(self, tmp_path, monkeypatch):
        from core import sessions
        monkeypatch.setattr(sessions, "list_active", lambda *a, **kw: [{"task_id": "t"}])
        path = _store(tmp_path, [("ses_old", 30)])
        res = runner_store.prune(keep_days=7, root=str(tmp_path))
        assert res["skipped"] == "a worker is running" and _rows(path, "session") == 1

    def test_no_store_is_not_an_error(self, tmp_path):
        res = runner_store.prune(keep_days=7, root=str(tmp_path))
        assert res["skipped"] == "no store yet" and res["removed"] == 0

    def test_a_store_it_cannot_read_is_reported_not_raised(self, tmp_path):
        path = runner_store.store_path(str(tmp_path))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("this is not a database")
        res = runner_store.prune(keep_days=7, root=str(tmp_path))
        assert res["skipped"] and res["removed"] == 0


def test_stats_describe_the_store_without_touching_it(tmp_path):
    _store(tmp_path, [("ses_old", 30), ("ses_now", 0)])
    st = runner_store.stats(str(tmp_path))
    assert st["exists"] and st["sessions"] == 2 and st["bytes"] > 0
    assert st["oldest"] < time.time() - 29 * 86400


def test_sizes_read_the_way_people_say_them():
    assert runner_store.human_size(900) == "900 B"
    assert runner_store.human_size(6_600_000_000) == "6.1 GB"
