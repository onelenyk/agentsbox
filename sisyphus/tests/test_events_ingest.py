"""Tests for core/events.py — live ingestion of agent events into activities."""
import json

import pytest

from core.events import EventIngestor
from core.state import create_run, create_step, get_activities_for_card


@pytest.fixture
def step_env(db_path, card):
    run = create_run(card["id"])
    step = create_step(run["id"], "Engineer", 1)
    return card, run, step


def _make_ingestor(tmp_path, card, run, step):
    return EventIngestor(
        tmp_path / "events.jsonl", card["id"], run["id"], step["id"], "Engineer",
    )


def _write_event(path, text, ts="2026-07-22T10:00:00+00:00", resources=None, newline=True):
    line = json.dumps({"ts": ts, "type": "comment", "text": text, "resources": resources or []})
    with open(path, "a") as f:
        f.write(line + ("\n" if newline else ""))


class TestEventIngestor:
    def test_missing_file_returns_zero(self, tmp_path, step_env):
        card, run, step = step_env
        ingestor = _make_ingestor(tmp_path, card, run, step)
        assert ingestor.ingest_once() == 0
        assert ingestor.comment_count == 0

    def test_ingests_complete_lines(self, tmp_path, step_env):
        card, run, step = step_env
        events = tmp_path / "events.jsonl"
        _write_event(events, "one", resources=["a.md"])
        _write_event(events, "two")

        ingestor = _make_ingestor(tmp_path, card, run, step)
        assert ingestor.ingest_once() == 2

        activities = get_activities_for_card(card["id"])
        assert [a["text"] for a in activities] == ["one", "two"]
        assert activities[0]["resources"] == ["a.md"]
        assert activities[0]["created_at"] == "2026-07-22T10:00:00+00:00"
        assert activities[0]["author"] == "Engineer"
        assert activities[0]["step_id"] == step["id"]

    def test_partial_trailing_line_deferred(self, tmp_path, step_env):
        card, run, step = step_env
        events = tmp_path / "events.jsonl"
        _write_event(events, "complete")
        _write_event(events, "partial", newline=False)  # agent mid-write

        ingestor = _make_ingestor(tmp_path, card, run, step)
        assert ingestor.ingest_once() == 1  # only the complete line

        with open(events, "a") as f:
            f.write("\n")  # agent finishes the line
        assert ingestor.ingest_once() == 1

        activities = get_activities_for_card(card["id"])
        assert [a["text"] for a in activities] == ["complete", "partial"]

    def test_no_double_ingest_on_sweep(self, tmp_path, step_env):
        card, run, step = step_env
        events = tmp_path / "events.jsonl"
        _write_event(events, "once")

        ingestor = _make_ingestor(tmp_path, card, run, step)
        assert ingestor.ingest_once() == 1
        assert ingestor.ingest_once() == 0  # sweep: nothing new
        assert len(get_activities_for_card(card["id"])) == 1

    def test_malformed_line_skipped_once(self, tmp_path, step_env):
        card, run, step = step_env
        events = tmp_path / "events.jsonl"
        with open(events, "a") as f:
            f.write("not json at all\n")
        _write_event(events, "good")

        ingestor = _make_ingestor(tmp_path, card, run, step)
        assert ingestor.ingest_once() == 1  # malformed skipped, good ingested
        assert ingestor.ingest_once() == 0  # malformed never re-read

        activities = get_activities_for_card(card["id"])
        assert [a["text"] for a in activities] == ["good"]

    def test_non_comment_events_ignored(self, tmp_path, step_env):
        card, run, step = step_env
        events = tmp_path / "events.jsonl"
        with open(events, "a") as f:
            f.write(json.dumps({"type": "heartbeat"}) + "\n")
            f.write(json.dumps({"type": "comment", "text": ""}) + "\n")  # empty text
        _write_event(events, "real")

        ingestor = _make_ingestor(tmp_path, card, run, step)
        assert ingestor.ingest_once() == 1

    def test_shrunk_file_stops_ingestion(self, tmp_path, step_env):
        card, run, step = step_env
        events = tmp_path / "events.jsonl"
        _write_event(events, "one")

        ingestor = _make_ingestor(tmp_path, card, run, step)
        assert ingestor.ingest_once() == 1

        events.write_text("")  # truncation — should never happen (append-only)
        _write_event(events, "after-truncate")
        assert ingestor.ingest_once() == 0
        assert ingestor.ingest_once() == 0
