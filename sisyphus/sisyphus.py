#!/usr/bin/env python3
"""Sisyphus — Personal AI Agent Orchestration System.

Local entrypoint. Provides a Click CLI for pipeline execution, card
management, and operational commands.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import click

os.environ.setdefault(
    "WORKSPACE_ROOT", str(Path(__file__).resolve().parent / "workspace")
)

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
        logger.debug("DB init skipped: %s", exc)


@cli.command()
@click.option("--name", help="Card name (short title)")
@click.option("--desc", help="Task description")
@click.option("--jira", help="Jira issue URL")
@click.option("--repo", default=None, help="Repository name (auto-detected if not set)")
@click.option("--pipeline", default="investigate", help="Pipeline name")
@click.option("--detach", is_flag=True, help="Run in background (fire-and-forget)")
def run(name, desc, jira, repo, pipeline, detach):
    """Create a card and run a pipeline."""
    from core.operations import create_and_run, create_and_run_async
    from core.repos import list_repos

    if repo is None:
        repos = list_repos()
        if not repos:
            click.echo("ERROR: No repos configured. Add one at /repos or via --repo.")
            return
        repo = repos[0]["name"]

    if detach:
        card_id = asyncio.run(create_and_run_async(pipeline, repo, desc or "No description", jira, name=name or ""))
        click.echo(f"Card {card_id} created. Running in background.")
    else:
        result = create_and_run(pipeline, repo, desc or "No description", jira, name=name or "")
        if result["status"] == "completed":
            click.echo(f"Pipeline completed.")
        else:
            click.echo(f"Pipeline failed: {result.get('error', 'Unknown error')}")


@cli.command()
@click.option("--name", help="Card name (short title)")
@click.option("--desc", help="Task description")
@click.option("--jira", help="Jira issue URL")
@click.option("--repo", default=None, help="Repository name (auto-detected if not set)")
@click.option("--pipeline", default="investigate", help="Pipeline name")
def create(name, desc, jira, repo, pipeline):
    """Create a card in backlog (no pipeline execution)."""
    from core.operations import create_card_only
    from core.repos import list_repos

    if repo is None:
        repos = list_repos()
        if not repos:
            click.echo("ERROR: No repos configured. Add one at /repos or via --repo.")
            return
        repo = repos[0]["name"]

    card = create_card_only(pipeline, repo, desc or "No description", jira, name=name or "")
    click.echo(f"Card {card['id']} created in backlog.")


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
        if card.get("name"):
            click.echo(f"Name:    {card['name']}")
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

        click.echo(f"{'CARD':<12} {'PIPELINE':<16} {'STATUS':<12} {'REPO':<20}")
        click.echo("-" * 62)
        for c in cards:
            repo_name = c.get("repo_name", "")
            click.echo(
                f"{c['id']:<12} {c['pipeline_name']:<16} {c['status']:<12} {repo_name:<20}"
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
@click.option("--repo", help="Filter by repository name")
def list_cmd(pipeline, status, repo):
    """List cards with optional filters."""
    from core.operations import list_cards_filtered

    cards = list_cards_filtered(pipeline=pipeline, status=status, repo=repo)
    if not cards:
        click.echo("No cards found.")
        return

    click.echo(f"{'CARD':<12} {'PIPELINE':<16} {'STATUS':<12} {'REPO':<20} {'CREATED':<20}")
    click.echo("-" * 82)
    for c in cards:
        created = c.get("created_at", "")[:19]
        repo_name = c.get("repo_name", "")
        click.echo(
            f"{c['id']:<12} {c['pipeline_name']:<16} {c['status']:<12} {repo_name:<20} {created:<20}"
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
def runners():
    """Manage AI runners (opencode, Claude, Ollama, etc.)."""

@runners.command("list")
def runners_list():
    """List all configured runners."""
    from core.runners import init_runners_table, list_runners
    init_runners_table()
    all_runners = list_runners()
    if not all_runners:
        click.echo("No runners configured.")
        return
    click.echo(f"{'NAME':<24} {'BINARY':<14} {'MODEL':<24} {'DEFAULT':<8} {'HEALTH':<12}")
    click.echo("-" * 82)
    for r in all_runners:
        default = "STAR" if r["is_default"] else ""
        health = r.get("health_status", "unknown")
        click.echo(f"{r['name']:<24} {r['binary']:<14} {r.get('model','')[:22]:<24} {default:<8} {health:<12}")

@runners.command("add")
@click.option("--name", required=True, help="Runner name")
@click.option("--binary", required=True, help="Binary path or name")
@click.option("--type", "runner_type", default="opencode", help="Runner type")
@click.option("--model", default="", help="Default model")
@click.option("--api-key-env", default="", help="Env var for API key")
@click.option("--base-url", default="", help="API base URL")
@click.option("--default", "is_default", is_flag=True, help="Set as default runner")
def runners_add(name, binary, runner_type, model, api_key_env, base_url, is_default):
    """Add a new AI runner."""
    from core.runners import add_runner, init_runners_table
    init_runners_table()
    r = add_runner(name, binary, runner_type, model, api_key_env, base_url, is_default=is_default)
    click.echo(f"Runner '{r['name']}' added (id={r['id']}).")

@runners.command("edit")
@click.argument("runner_id")
@click.option("--name", default=None, help="New name")
@click.option("--binary", default=None, help="New binary")
@click.option("--type", "runner_type", default=None, help="New type")
@click.option("--model", default=None, help="New model")
@click.option("--api-key-env", default=None, help="New API key env var")
@click.option("--base-url", default=None, help="New base URL")
def runners_edit(runner_id, name, binary, runner_type, model, api_key_env, base_url):
    """Edit an existing runner."""
    from core.runners import update_runner
    fields = {}
    for k, v in [("name", name), ("binary", binary), ("type", runner_type),
                 ("model", model), ("api_key_env", api_key_env), ("base_url", base_url)]:
        if v is not None:
            fields[k] = v
    if not fields:
        click.echo("No fields to update.")
        return
    r = update_runner(runner_id, **fields)
    if r:
        click.echo(f"Runner '{r['name']}' updated.")
    else:
        click.echo(f"Runner {runner_id} not found.")

@runners.command("delete")
@click.argument("runner_id")
@click.confirmation_option(prompt="Are you sure?")
def runners_delete(runner_id):
    """Delete a runner."""
    from core.runners import delete_runner
    delete_runner(runner_id)
    click.echo(f"Runner {runner_id} deleted.")

@runners.command("set-default")
@click.argument("runner_id")
def runners_set_default(runner_id):
    """Set a runner as the default."""
    from core.runners import set_default_runner
    r = set_default_runner(runner_id)
    if r:
        click.echo(f"Runner '{r['name']}' is now the default.")
    else:
        click.echo(f"Runner {runner_id} not found.")

@runners.command("health")
@click.argument("runner_id", required=False)
def runners_health(runner_id):
    """Check runner health (or all runners if no ID given)."""
    import asyncio
    from core.runners import health_check, list_runners

    async def _check():
        if runner_id:
            result = await health_check(runner_id)
            click.echo(f"Runner {runner_id}: {result['status']}")
            if result.get("error"):
                click.echo(f"  Error: {result['error']}")
        else:
            for r in list_runners():
                result = await health_check(r["id"])
                icon = "OK" if result["status"] == "healthy" else "XX"
                click.echo(f"  {icon} {r['name']}: {result['status']}")
                if result.get("error"):
                    click.echo(f"     {result['error']}")

    asyncio.run(_check())


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
