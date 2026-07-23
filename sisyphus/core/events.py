"""Live ingestion of agent events (events.jsonl) into the activity timeline.

The agent appends JSON lines via the toolbelt CLI (tools/agent_cli.py) inside
its sandboxed output dir; the orchestrator tails that file while the step
runs so comments appear in the UI in near-real-time. The DB write side lives
entirely in the orchestrator process — the agent never touches SQLite.
"""
import asyncio
import json
import logging
from pathlib import Path

from core.state import add_activity

logger = logging.getLogger(__name__)


class EventIngestor:
    """Tail one step's events.jsonl and turn comment events into activities.

    Byte-offset based: only complete lines (ending in ``\\n``) are consumed;
    a partial trailing line (agent mid-write) is left for the next poll.
    The final post-step sweep reuses the same object and offset, so nothing
    is ever double-ingested. Malformed lines are skipped once (offset still
    advances past them) and never re-read.
    """

    def __init__(self, events_path: Path, card_id: str, run_id: str, step_id: str, author: str):
        self.events_path = Path(events_path)
        self.card_id = card_id
        self.run_id = run_id
        self.step_id = step_id
        self.author = author
        self.comment_count = 0
        self._offset = 0
        self._stopped = False

    def ingest_once(self) -> int:
        """Consume newly appended complete lines. Returns #activities added."""
        if self._stopped or not self.events_path.exists():
            return 0

        try:
            size = self.events_path.stat().st_size
            if size < self._offset:
                logger.warning(
                    "events.jsonl shrank (%d -> %d) for step %s — stopping ingestion",
                    self._offset, size, self.step_id,
                )
                self._stopped = True
                return 0

            with open(self.events_path, "rb") as f:
                f.seek(self._offset)
                chunk = f.read()
        except OSError as e:
            logger.warning("failed reading events.jsonl for step %s: %s", self.step_id, e)
            return 0

        last_newline = chunk.rfind(b"\n")
        if last_newline == -1:
            return 0  # no complete line yet
        complete = chunk[: last_newline + 1]
        self._offset += len(complete)

        added = 0
        for raw_line in complete.split(b"\n"):
            if not raw_line.strip():
                continue
            try:
                event = json.loads(raw_line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                logger.warning(
                    "skipping malformed events.jsonl line for step %s: %r",
                    self.step_id, raw_line[:200],
                )
                continue
            if event.get("type") != "comment" or not event.get("text"):
                continue
            add_activity(
                self.card_id,
                event["text"],
                author=self.author,
                author_type="agent",
                run_id=self.run_id,
                step_id=self.step_id,
                resources=event.get("resources") or [],
                created_at=event.get("ts"),
            )
            added += 1

        self.comment_count += added
        return added

    async def run(self, poll_interval: float = 1.0) -> None:
        while True:
            self.ingest_once()
            await asyncio.sleep(poll_interval)
