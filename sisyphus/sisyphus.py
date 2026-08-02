#!/usr/bin/env python3
"""Sisyphus — Personal AI Agent Orchestration System.

Local entrypoint. Provides a Click CLI for runner management. Task/thread
creation and monitoring go through the web UI (/tasks) or the daemon's API —
this CLI only manages the AI runner configs the daemon and its workers use.
"""

import asyncio
import logging
import os
from pathlib import Path

import click

from core.env import load_env
load_env()
os.environ.setdefault(
    "WORKSPACE_ROOT", str(Path(__file__).resolve().parent / "workspace")
)

from core.db import init_db

logger = logging.getLogger(__name__)


@click.group()
@click.version_option(version="0.1.0", prog_name="sisyphus")
def cli():
    """Sisyphus — Personal AI Agent Orchestration System."""
    try:
        init_db()
    except Exception as exc:
        logger.debug("DB init skipped: %s", exc)


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


@runners.command("detect")
def runners_detect():
    """Auto-detect installed runner binaries (PATH + common install dirs)."""
    from core.runner_detect import detect, resolve
    from core.runners import list_runners

    found = detect()
    if found:
        click.echo("Detected on this machine:")
        for f in found:
            click.echo(f"  {f['name']:<12} {f['path']}")
    else:
        click.echo("No known runner binaries found.")
    click.echo("\nConfigured runtimes:")
    for r in list_runners():
        p = resolve(r["binary"])
        click.echo(f"  {r['name']:<22} {p or 'NOT FOUND: ' + r['binary']}")


@cli.group()
def daemon():
    """Control the daemon (manual, or launchd-supervised)."""


@daemon.command("status")
def daemon_status():
    """Show whether the daemon is running and supervised."""
    from core import daemon_ctl as d

    s = d.status()
    click.echo(f"daemon:     {'live' if s['running'] else 'down'}"
               + (f" (v{s['version']})" if s["version"] else ""))
    click.echo(f"supervised: {'yes (launchd)' if s['supervised'] else 'no'}")
    click.echo(f"url:        {s['url']}")


@daemon.command("install")
def daemon_install():
    """Install & load the launchd LaunchAgent (start at login, restart on crash)."""
    from core import daemon_ctl as d

    res = d.install()
    if res["ok"]:
        click.echo(f"Installed and loaded: {res['plist']}")
        click.echo("The daemon now starts at login and restarts on crash.")
        click.echo("Reminder: put your OPENROUTER_API_KEY in .env (launchd has no shell env).")
    else:
        click.echo(f"launchctl load failed: {res['err']}", err=True)


@daemon.command("uninstall")
def daemon_uninstall():
    """Unload & remove the launchd LaunchAgent."""
    from core import daemon_ctl as d

    click.echo("Removed the LaunchAgent." if d.uninstall()["removed"]
               else "No LaunchAgent installed.")


@daemon.command("start")
def daemon_start():
    """Start the daemon (via launchd if installed, else spawn uvicorn)."""
    from core import daemon_ctl as d

    res = d.start()
    if res.get("already_running"):
        click.echo("Already running.")
    else:
        click.echo(f"Started ({res['via']}).")


@daemon.command("stop")
def daemon_stop():
    """Stop the daemon."""
    from core import daemon_ctl as d

    d.stop()
    click.echo("Stopped.")


@daemon.command("restart")
def daemon_restart():
    """Restart the daemon (to pick up new code)."""
    from core import daemon_ctl as d

    d.restart()
    click.echo("Restarted." if d.is_running() else "Restart issued (still starting…).")


@daemon.command("logs")
@click.option("-f", "--follow", is_flag=True, help="Follow the log (tail -f).")
def daemon_logs(follow):
    """Tail the daemon log."""
    import subprocess
    from core import daemon_ctl as d

    log = d.LOG_DIR / "daemon.err.log"
    if not log.exists():
        click.echo(f"No log yet at {log}")
        return
    subprocess.run(["tail"] + (["-f"] if follow else ["-n", "200"]) + [str(log)])


if __name__ == "__main__":
    cli()
