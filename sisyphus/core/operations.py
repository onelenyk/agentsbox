"""Unified operations module — single source of truth for card lifecycle.

All card lifecycle operations (create, run, resume, cancel) live here.
Both CLI (sisyphus/sisyphus.py) and Web (sisyphus/web/routes.py) will
become thin wrappers calling these functions.
"""

import asyncio
import json
import logging
import os
import signal
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.db import get_conn
from core.fsm import transition_step
from core.repos import get_repo_by_platform
from core.resources import list_resources
from core.state import (
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
    """Resolve pipeline YAML path, portable across macOS and Docker.

    Tries the local dev path first (relative to this module), then falls
    back to the Docker container path.
    """
    local = Path(__file__).resolve().parent.parent / "pipelines" / f"{name}.yaml"
    if local.exists():
        return local
    docker = Path(f"/app/pipelines/{name}.yaml")
    if docker.exists():
        return docker
    # Return local path anyway — caller handles missing-file errors.
    return local


# ── 2. create_card_only ────────────────────────────────────────────────

def create_card_only(
    pipeline_name: str,
    platforms: list,
    description: str,
    jira_url: Optional[str] = None,
) -> dict:
    """Create a card in 'backlog' status (NO pipeline execution).

    ``core.state.create_card`` already defaults to ``status='backlog'``.
    """
    return create_card(
        pipeline_name, platforms, description or "No description", jira_url,
    )


# ── 3. create_and_run (sync / blocking — for CLI) ──────────────────────

def create_and_run(
    pipeline_name: str,
    platforms: list,
    description: str,
    jira_url: Optional[str] = None,
    repo_url: Optional[str] = None,
) -> dict:
    """Create card + run pipeline synchronously. Blocks until done.

    Returns:
        ``{"status": "completed"|"failed", "run_id": ..., "error": ...}``
    """
    from core.pipeline import run_pipeline

    card = create_card_only(pipeline_name, platforms, description, jira_url)
    path = str(resolve_pipeline_path(pipeline_name))
    return asyncio.run(run_pipeline(path, card, gitlab_repo_url=repo_url))


# ── 4. create_and_run_async (background — for Web) ─────────────────────

async def create_and_run_async(
    pipeline_name: str,
    platforms: list,
    description: str,
    jira_url: Optional[str] = None,
    repo_url: Optional[str] = None,
) -> str:
    """Create card + fire pipeline in background. Returns *card_id* immediately."""
    card = create_card_only(pipeline_name, platforms, description, jira_url)
    update_card_status(card["id"], "created")
    asyncio.create_task(
        _run_bg_async(pipeline_name, card, gitlab_repo_url=repo_url),
    )
    return card["id"]


# ── 5. run_card ────────────────────────────────────────────────────────

async def run_card(
    card_id: str, repo_url: Optional[str] = None,
) -> None:
    """Run pipeline for an existing card.

    Auto-resolves *repo_url* from the card's first platform via
    ``get_repo_by_platform`` when none is provided.
    """
    card = get_card(card_id)
    if not card:
        raise ValueError(f"Card {card_id} not found")

    # Auto-resolve repo from platform
    if not repo_url:
        platforms = _parse_platforms(card.get("platforms", "[]"))
        if platforms:
            repo = get_repo_by_platform(platforms[0])
            if repo:
                repo_url = repo["source"]

    update_card_status(card_id, "running")
    asyncio.create_task(
        _run_bg_async(card["pipeline_name"], card, gitlab_repo_url=repo_url),
    )


# ── 6. resume_card ─────────────────────────────────────────────────────

async def resume_card(card_id: str) -> None:
    """Resume a failed card from the last failed/pending step.

    Finds ALL steps in (failed, timeout, error, cancelled, pending) for
    the latest run, resets each to ``"running"`` via ``transition_step``,
    then re-runs the pipeline synchronously.
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

    steps = get_steps(run["id"])
    failed_pending = [
        s for s in steps
        if s["status"] in ("failed", "timeout", "error", "cancelled", "pending")
    ]

    for s in failed_pending:
        transition_step(s["id"], "running")

    from core.pipeline import run_pipeline

    path = str(resolve_pipeline_path(card["pipeline_name"]))
    await run_pipeline(path, card)


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
    ``resources``, and ``repo_name`` (resolved from the first platform).
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

    # Resolve repo_name from first platform
    platforms = _parse_platforms(card.get("platforms", "[]"))
    repo = get_repo_by_platform(platforms[0]) if platforms else None
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
    platform: Optional[str] = None,
) -> list:
    """List cards with optional filters. Returns raw card dicts."""
    cards = list_cards(status_filter=status)

    filtered: list = []
    for c in cards:
        if pipeline and c.get("pipeline_name") != pipeline:
            continue
        if platform:
            plats = _parse_platforms(c.get("platforms", "[]"))
            if platform not in plats:
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


# ── Internal: _parse_platforms ─────────────────────────────────────────

def _parse_platforms(raw) -> list:
    """Parse platforms field (JSON string or list) into a Python list."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    return []
