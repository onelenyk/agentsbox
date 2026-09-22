"""The test taxonomy — every test module belongs to exactly one tier.

Applied automatically by conftest.pytest_collection_modifyitems, so files carry
no markers themselves and the whole classification is reviewable in one place.
A module missing from this map fails collection on purpose: a new test file
must say what kind of test it is before it gets to run.

Tiers (see docs/testing.md for the strategy):
  unit       in-process logic; at most a tmp SQLite db. No subprocesses, no git.
  component  one subsystem against the real thing it drives: FastAPI routes via
             TestClient, git actions on real temp repos, the MCP stdio server.
  process    real OS processes: spawning, signals, sandbox, timeouts. The tier
             that carries honest environment skips.
  e2e        a whole path through the system, artifacts checked at the end
             (machine e2e without the model; browser e2e via Playwright).
"""

TIERS = {
    "unit": [
        "test_agents", "test_ai_assist", "test_autopilot", "test_backup",
        "test_budget", "test_concurrency", "test_config", "test_design_system",
        "test_env", "test_forge_poll",
        "test_heartbeat", "test_launcher_cancel", "test_ledger", "test_logging_setup",
        "test_markdown", "test_mergerequests", "test_migrations", "test_notifications", "test_pulse", "test_timeline", "test_runner_catalog", "test_asks",
        "test_notify",
        "test_orchestrator", "test_preflight", "test_repo_context",
        "test_repos", "test_reviews",
        "test_runlogs", "test_runner_detect", "test_runner_profiles",
        "test_runner_home", "test_runner_smoke", "test_runners",
        "test_sessions",
        "test_small_modules", "test_store", "test_usage", "test_worker_env",
    ],
    "component": [
        "test_agent_mcp", "test_agents_api", "test_api", "test_auth",
        "test_git_ops", "test_goal_report", "test_mcp_server",
        "test_mr_actions", "test_mr_resolve",
        "test_page_smoke", "test_planner", "test_providers", "test_repo_check",
        "test_setup_hook", "test_stats", "test_task_lifecycle", "test_verify",
        "test_web_routes", "test_webhook_auth", "test_webhooks", "test_worker",
        "test_worktree", "test_worktree_utils",
    ],
    "process": [
        "test_agent_mcp_stdio", "test_daemon_ctl", "test_launcher", "test_orphans",
        "test_sandbox",
    ],
    "e2e": [
        "test_end_to_end", "test_notifications_browser",
    ],
}

_BY_MODULE = {m: tier for tier, mods in TIERS.items() for m in mods}


def tier_for(module_basename: str) -> str:
    """The tier of a test module, by basename without .py. Raises KeyError with
    instructions when the module is unclassified."""
    try:
        return _BY_MODULE[module_basename]
    except KeyError:
        raise KeyError(
            f"{module_basename}.py is not classified — add it to a tier in "
            "tests/tiers.py (unit / component / process / e2e) before it can run."
        ) from None
