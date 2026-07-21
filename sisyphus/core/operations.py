"""Unified operations module — single source of truth for card lifecycle.

All card lifecycle operations (create, run, resume, cancel) live here.
Both CLI (sisyphus/sisyphus.py) and Web (sisyphus/web/routes.py) will
become thin wrappers calling these functions.
"""

import asyncio
import logging
import os
import signal
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.db import get_conn
from core.fsm import transition_step
from core.repos import get_repo
from core.resources import list_resources
from core.state import (
    claim_card,
    create_card,
    get_card,
    get_steps,
    list_cards,
    update_card_status,
    update_run_status,
)

logger = logging.getLogger(__name__)

# Global registry of cancel events, keyed by card_id
_cancel_events: dict[str, asyncio.Event] = {}


# ── 1. resolve_pipeline_path ───────────────────────────────────────────

def resolve_pipeline_path(name: str) -> Path:
    """Resolve pipeline YAML path relative to the repo root.

    Caller handles missing-file errors if it doesn't exist.
    """
    return Path(__file__).resolve().parent.parent / "pipelines" / f"{name}.yaml"


# ── 2. create_card_only ────────────────────────────────────────────────

def create_card_only(
    pipeline_name: str,
    repo_name: str,
    description: str,
    jira_url: Optional[str] = None,
    name: str = "",
) -> dict:
    """Create a card in 'backlog' status (NO pipeline execution).

    ``core.state.create_card`` already defaults to ``status='backlog'``.
    """
    return create_card(
        pipeline_name, repo_name, description or "No description", jira_url,
        name=name,
    )


# ── 3. create_and_run (sync / blocking — for CLI) ──────────────────────

def create_and_run(
    pipeline_name: str,
    repo_name: str,
    description: str,
    jira_url: Optional[str] = None,
    repo_url: Optional[str] = None,
    name: str = "",
) -> dict:
    """Create card + run pipeline synchronously. Blocks until done.

    Returns:
        ``{"status": "completed"|"failed", "run_id": ..., "error": ...}``
    """
    from core.pipeline import run_pipeline

    card = create_card_only(pipeline_name, repo_name, description, jira_url, name=name)
    if not claim_card(card["id"]):
        return {"status": "failed", "run_id": "", "error": "card_already_claimed"}
    path = str(resolve_pipeline_path(pipeline_name))
    return asyncio.run(run_pipeline(path, card, gitlab_repo_url=repo_url))


# ── 4. create_and_run_async (background — for Web) ─────────────────────

async def create_and_run_async(
    pipeline_name: str,
    repo_name: str,
    description: str,
    jira_url: Optional[str] = None,
    repo_url: Optional[str] = None,
    name: str = "",
) -> str:
    """Create card + fire pipeline in background. Returns *card_id* immediately."""
    card = create_card_only(pipeline_name, repo_name, description, jira_url, name=name)
    claim_card(card["id"])
    asyncio.create_task(
        _run_bg_async(pipeline_name, card, gitlab_repo_url=repo_url),
    )
    return card["id"]


# ── 5. run_card ────────────────────────────────────────────────────────

async def run_card(
    card_id: str, repo_url: Optional[str] = None,
) -> None:
    """Run pipeline for an existing card.

    Auto-resolves *repo_url* from the card's ``repo_name`` via
    ``get_repo`` when none is provided.
    """
    card = get_card(card_id)
    if not card:
        raise ValueError(f"Card {card_id} not found")

    if not claim_card(card_id):
        logger.info("run_card: card %s already claimed/running — skipping", card_id)
        return

    # Auto-resolve repo from repo_name
    repo_name = card.get("repo_name", "")
    if not repo_url and repo_name:
        repo = get_repo(repo_name)
        if repo:
            repo_url = repo["source"]
    await _run_bg_async(card["pipeline_name"], card, gitlab_repo_url=repo_url)


# ── 6. resume_card ─────────────────────────────────────────────────────

async def resume_card(card_id: str) -> None:
    """Resume a failed card.

    Finds all failed/pending steps in the latest run, marks them as
    cancelled (superseded), and fires a fresh pipeline execution in
    the background.
    """
    card = get_card(card_id)
    if not card:
        raise ValueError(f"Card {card_id} not found")

    conn = get_conn()
    run = conn.execute(
        "SELECT * FROM runs WHERE card_id = ? ORDER BY started_at DESC LIMIT 1",
        (card_id,),
    ).fetchone()
    conn.close()

    if not run:
        raise ValueError(f"No runs found for card {card_id}")

    if not claim_card(card_id, from_statuses=("failed",)):
        logger.info("resume_card: card %s not failed or already claimed — skipping", card_id)
        return

    steps = get_steps(run["id"])
    failed_pending = [
        s for s in steps
        if s["status"] in ("failed", "timeout", "error", "cancelled", "pending")
    ]

    for s in failed_pending:
        # Reset to running (now allowed by FSM for terminal statuses),
        # then immediately cancel — marks old steps as superseded so they
        # don't appear as orphaned "running" steps.
        if transition_step(s["id"], "running"):
            transition_step(s["id"], "cancelled", error="superseded_by_resume")

    # Fire background execution via the shared helper so card/run
    # statuses stay in sync and cancel events are wired correctly.
    asyncio.create_task(_run_bg_async(card["pipeline_name"], card))


# ── 7. cancel_card ─────────────────────────────────────────────────────

async def cancel_card(card_id: str) -> None:
    """Cancel a running card.

    Marks running steps as cancelled, updates card/run status to failed,
    and kills opencode subprocesses for the card.
    """
    card = get_card(card_id)
    if not card:
        raise ValueError(f"Card {card_id} not found")

    conn = get_conn()
    running_steps = conn.execute(
        """
        SELECT s.id, s.agent_name, s.started_at
        FROM steps s
        JOIN runs r ON s.run_id = r.id
        WHERE r.card_id = ? AND s.status = 'running'
        """,
        (card_id,),
    ).fetchall()

    run = conn.execute(
        "SELECT id FROM runs WHERE card_id = ? ORDER BY started_at DESC LIMIT 1",
        (card_id,),
    ).fetchone()
    conn.close()

    for step in running_steps:
        transition_step(step["id"], "cancelled", error="user_cancelled")

    update_card_status(card_id, "failed")
    if run:
        update_run_status(run["id"], "failed", "cancelled")

    cancel_event = asyncio.Event()
    cancel_event.set()
    _cancel_events[card_id] = cancel_event

    _kill_opencode_for_card(card_id)


# ── 8. get_card_detail ─────────────────────────────────────────────────

def get_card_detail(card_id: str) -> dict:
    """Return full card detail.

    Returns a dict with keys: ``card``, ``run`` (latest), ``steps``
    (with duration computed from started_at/completed_at),
    ``resources``, and ``repo_name`` (resolved from ``repo_name`` field).
    """
    card = get_card(card_id)
    if not card:
        raise ValueError(f"Card {card_id} not found")

    conn = get_conn()
    run = conn.execute(
        "SELECT * FROM runs WHERE card_id = ? ORDER BY started_at DESC LIMIT 1",
        (card_id,),
    ).fetchone()
    conn.close()

    run_dict = dict(run) if run else None
    steps = get_steps(run["id"]) if run else []
    resources = list_resources(run["id"]) if run else []

    # Compute duration for each step
    for s in steps:
        if s.get("started_at") and s.get("completed_at"):
            try:
                start = datetime.fromisoformat(
                    s["started_at"].replace("Z", "+00:00"),
                )
                end = datetime.fromisoformat(
                    s["completed_at"].replace("Z", "+00:00"),
                )
                s["duration"] = str(end - start).split(".")[0]
            except (ValueError, AttributeError):
                s["duration"] = "\u2014"
        else:
            s["duration"] = "\u2014"

    repo = get_repo(card.get("repo_name", "")) if card.get("repo_name") else None
    repo_name = repo["name"] if repo else None

    return {
        "card": card,
        "run": run_dict,
        "steps": steps,
        "resources": resources,
        "repo_name": repo_name,
    }


# ── 9. list_cards_filtered ─────────────────────────────────────────────

def list_cards_filtered(
    pipeline: Optional[str] = None,
    status: Optional[str] = None,
    repo: Optional[str] = None,
) -> list:
    """List cards with optional filters. Returns raw card dicts."""
    cards = list_cards(status_filter=status)

    filtered: list = []
    for c in cards:
        if pipeline and c.get("pipeline_name") != pipeline:
            continue
        if repo and c.get("repo_name") != repo:
            continue
        filtered.append(c)

    return filtered


# ── Internal: _run_bg_async ────────────────────────────────────────────

async def _run_bg_async(
    pipeline_name: str,
    card: dict,
    gitlab_repo_url: Optional[str] = None,
    cancel_event: Optional[asyncio.Event] = None,
) -> None:
    """Run pipeline in background. Port from routes.py ``_run_bg``.

    Resolves the pipeline path, runs ``run_pipeline`` (which handles
    auto-repo resolution internally), then updates the card status.
    """
    from core.pipeline import run_pipeline

    pp = str(resolve_pipeline_path(pipeline_name))
    cancel_event = _cancel_events.get(card["id"])
    try:
        r = await run_pipeline(
            pp,
            card,
            gitlab_repo_url=gitlab_repo_url,
            cancel_event=cancel_event,
        )
        update_card_status(
            card["id"],
            "completed" if r["status"] == "completed" else "failed",
        )
    except Exception:
        update_card_status(card["id"], "failed")
    finally:
        _cancel_events.pop(card["id"], None)


# ── Internal: _kill_opencode_for_card ──────────────────────────────────

def _kill_opencode_for_card(card_id: str) -> None:
    """Kill opencode subprocesses for a card. Port from sisyphus.py."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"opencode.*{card_id}"],
            capture_output=True, text=True,
        )
        for pid_str in result.stdout.strip().split("\n"):
            pid = pid_str.strip()
            if not pid:
                continue
            try:
                os.kill(int(pid), signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass
    except Exception:
        pass  # pgrep might fail — non-critical



