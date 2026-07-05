#!/usr/bin/env python3
"""Sisyphus — Personal AI Agent Orchestration System.

Entrypoint for the Sisyphus Docker container. Provides a Click CLI for
pipeline execution, card management, and operational commands.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import click

from core.db import init_db, get_conn
from core.recovery import recover_orphaned_steps
from core.scheduler import start_scheduler

logger = logging.getLogger(__name__)

WORKSPACE_ROOT = os.environ.get("WORKSPACE_ROOT", "/workspace")
RUNS_DIR = Path(WORKSPACE_ROOT) / "runs"


@click.group()
@click.version_option(version="0.1.0", prog_name="sisyphus")
def cli():
    """Sisyphus — Personal AI Agent Orchestration System.

    Orchestrates AI agents through code analysis pipelines.
    """
    try:
        init_db()
        recovered = recover_orphaned_steps()
        if recovered > 0:
            click.echo(f"Recovered {recovered} orphaned steps (orchestrator crash detected)")
        start_scheduler()
    except Exception as exc:
        logger.debug("DB init skipped (not in Docker?): %s", exc)


@cli.command()
@click.option("--desc", help="Task description")
@click.option("--jira", help="Jira issue URL")
@click.option("--platforms", default="android", help="Comma-separated platforms")
@click.option("--pipeline", default="investigate", help="Pipeline name")
@click.option("--detach", is_flag=True, help="Run in background (fire-and-forget)")
def run(desc, jira, platforms, pipeline, detach):
    """Create a card and run a pipeline."""
    from core.operations import create_and_run, create_and_run_async

    platform_list = [p.strip() for p in platforms.split(",")]

    if detach:
        card_id = asyncio.run(create_and_run_async(pipeline, platform_list, desc or "No description", jira))
        click.echo(f"Card {card_id} created. Running in background.")
    else:
        result = create_and_run(pipeline, platform_list, desc or "No description", jira)
        if result["status"] == "completed":
            click.echo(f"Pipeline completed.")
        else:
            click.echo(f"Pipeline failed: {result.get('error', 'Unknown error')}")


@cli.command()
@click.argument("card_id", required=False)
@click.option("--verbose", is_flag=True, help="Show resources for each step")
def status(card_id, verbose):
    """Show card and pipeline status.

    Without CARD_ID: shows a table of all cards.
    With CARD_ID: shows detailed per-step view with resources.
    """
    if card_id:
        from core.operations import get_card_detail

        try:
            detail = get_card_detail(card_id)
        except ValueError:
            click.echo(f"Card {card_id} not found.")
            return

        card = detail["card"]
        run = detail["run"]
        steps = detail["steps"]

        click.echo(f"Card:    {card['id']}")
        click.echo(f"Pipeline: {card['pipeline_name']}")
        click.echo(f"Status:   {card['status']}")
        click.echo(f"Desc:     {card.get('description', 'N/A')}")
        if card.get("jira_url"):
            click.echo(f"Jira:     {card['jira_url']}")
        click.echo()

        if not run:
            click.echo("No runs found for this card.")
            return

        click.echo(f"Run: {run['id']}  Status: {run['status']}  Started: {run['started_at']}")
        click.echo(f"{'  STEP':<12} {'AGENT':<22} {'STATUS':<12} {'ERROR'}")
        click.echo("  " + "-" * 58)
        for s in steps:
            error = s.get("error", "") or ""
            click.echo(
                f"  {s['id']:<10} {s['agent_name']:<22} {s['status']:<12} {error[:35]}"
            )
        click.echo()

        if verbose:
            _show_resources(run["id"])
    else:
        from core.operations import list_cards_filtered

        cards = list_cards_filtered()
        if not cards:
            click.echo("No cards found.")
            return

        click.echo(f"{'CARD':<12} {'PIPELINE':<16} {'STATUS':<12} {'PLATFORMS':<20}")
        click.echo("-" * 62)
        for c in cards:
            platforms = c.get("platforms", "[]")
            click.echo(
                f"{c['id']:<12} {c['pipeline_name']:<16} {c['status']:<12} {platforms:<20}"
            )


def _show_resources(run_id):
    conn = get_conn()
    resources = conn.execute(
        "SELECT * FROM resources WHERE run_id = ? ORDER BY step_order ASC, created_at ASC",
        (run_id,),
    ).fetchall()
    conn.close()

    if not resources:
        return

    click.echo("Resources:")
    for r in resources:
        click.echo(f"  [{r['resource_type']}] {r['filename']} (step: {r['step_id']})")


@cli.command()
@click.argument("card_id")
@click.option("--step", help="Filter by step name")
@click.option("--follow", is_flag=True, help="Tail -f mode (Ctrl+C to stop)")
def logs(card_id, step, follow):
    """Show agent logs for a card's latest run."""
    conn = get_conn()
    run = conn.execute(
        "SELECT id FROM runs WHERE card_id = ? ORDER BY started_at DESC LIMIT 1",
        (card_id,),
    ).fetchone()
    conn.close()

    if not run:
        click.echo(f"No runs found for card {card_id}")
        return

    log_dir = RUNS_DIR / run["id"] / "logs"
    if not log_dir.exists():
        click.echo("No logs found.")
        return

    if follow:
        click.echo("Following logs (Ctrl+C to stop)...")
        import time

        try:
            while True:
                _print_logs(log_dir, step)
                time.sleep(2)
        except KeyboardInterrupt:
            click.echo()
    else:
        _print_logs(log_dir, step)


def _print_logs(log_dir, step_filter=None):
    for logfile in sorted(log_dir.glob("*.log")):
        if step_filter and step_filter not in logfile.name:
            continue
        content = logfile.read_text()
        if content.strip():
            click.echo(f"--- {logfile.name} ---")
            click.echo(content[-2000:])


@cli.command()
@click.argument("card_id")
def cancel(card_id):
    """Cancel a running pipeline. Kills active agent subprocesses."""
    from core.operations import cancel_card

    try:
        asyncio.run(cancel_card(card_id))
        click.echo(f"Card {card_id} cancelled.")
    except ValueError as e:
        click.echo(str(e))


@cli.command()
@click.argument("card_id")
def resume(card_id):
    """Resume a failed pipeline from the last failed step."""
    from core.operations import resume_card

    try:
        asyncio.run(resume_card(card_id))
        click.echo(f"Card {card_id} resumed and completed.")
    except ValueError as e:
        click.echo(str(e))


@cli.command("list")
@click.option("--pipeline", help="Filter by pipeline name")
@click.option("--status", help="Filter by status (created, running, completed, failed)")
@click.option("--platform", help="Filter by platform")
def list_cmd(pipeline, status, platform):
    """List cards with optional filters."""
    from core.operations import list_cards_filtered

    cards = list_cards_filtered(pipeline=pipeline, status=status, platform=platform)
    if not cards:
        click.echo("No cards found.")
        return

    click.echo(f"{'CARD':<12} {'PIPELINE':<16} {'STATUS':<12} {'CREATED':<20}")
    click.echo("-" * 62)
    for c in cards:
        created = c.get("created_at", "")[:19]
        click.echo(
            f"{c['id']:<12} {c['pipeline_name']:<16} {c['status']:<12} {created:<20}"
        )


@cli.command()
@click.option(
    "--keep-days",
    default=7,
    help="Keep run directories newer than N days (default: 7)",
)
@click.option("--dry-run", is_flag=True, help="Show what would be removed without removing")
def cleanup(keep_days, dry_run):
    """Remove old run directories and orphan worktrees."""
    import shutil
    from datetime import timedelta

    if not RUNS_DIR.exists():
        click.echo("No run directories to clean up.")
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    removed = 0

    for run_dir in sorted(RUNS_DIR.iterdir()):
        if not run_dir.is_dir():
            continue
        mtime = datetime.fromtimestamp(run_dir.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            if dry_run:
                click.echo(f"  Would remove: {run_dir.name}")
            else:
                shutil.rmtree(run_dir, ignore_errors=True)
            removed += 1

    action = "Would clean" if dry_run else "Cleaned"
    click.echo(
        f"{action} up {removed} run director{'y' if removed == 1 else 'ies'} "
        f"(older than {keep_days} days)."
    )


@cli.group()
def scheduler():
    """Manage the background scheduler."""


@scheduler.command("status")
def scheduler_status():
    """Show scheduler job status."""
    from core.scheduler import get_scheduler
    sched = get_scheduler()
    if not sched or not sched.get_jobs():
        click.echo("No scheduler jobs configured.")
        return
    click.echo(f"{'Job':<20} {'Next Run':<30} {'Trigger':<20}")
    click.echo("-" * 70)
    for job in sched.get_jobs():
        next_run = str(job.next_run_time)[:19] if job.next_run_time else "N/A"
        trigger = str(job.trigger)
        click.echo(f"{job.name:<20} {next_run:<30} {trigger:<20}")


if __name__ == "__main__":
    cli()
