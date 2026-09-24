"""Jira issue intake — issues become tasks, on demand and by polling.

On-demand: POST /api/integrations/jira/import with a JQL to create tasks.
Polling: a daemon loop watches for changes every jira.intake.interval_seconds
and creates tasks for new issues matching the configured JQL.

Every import (manual or polling) records one journal row per pass, and skips
issues that already became tasks in an earlier pass (via the origin field).
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Optional

from . import jira, integrations, store, heartbeat
from .config import get_config

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL = 300
MIN_INTERVAL = 60


class JiraIntakeError(RuntimeError):
    """Intake operation failed."""
    pass


def init_jira_sync_table() -> None:
    """Create the jira_sync table if it does not exist.

    Tracks the last time we polled each JQL so we only fetch issues changed
    since then.
    """
    from .db import get_conn
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jira_sync (
            jql         TEXT PRIMARY KEY,
            last_seen   TEXT,                      -- newest updated_at we saw (Jira format)
            updated_at  TEXT NOT NULL              -- when we last polled
        )
    """)
    conn.commit()
    conn.close()


def _get_last_seen(jql: str) -> Optional[str]:
    """The newest updated timestamp from the last poll of this JQL."""
    from .db import get_conn
    conn = get_conn()
    row = conn.execute("SELECT last_seen FROM jira_sync WHERE jql = ?", (jql,)).fetchone()
    conn.close()
    return row["last_seen"] if row else None


def _update_last_seen(jql: str, last_seen: str) -> None:
    """Record the newest updated timestamp after a successful poll."""
    from .db import get_conn
    now = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    conn.execute(
        "INSERT INTO jira_sync (jql, last_seen, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(jql) DO UPDATE SET last_seen = excluded.last_seen, updated_at = excluded.updated_at",
        (jql, last_seen, now)
    )
    conn.commit()
    conn.close()


def _format_jira_date(dt: datetime) -> str:
    """Format a datetime to Jira JQL date format: yyyy-MM-dd HH:mm."""
    return dt.strftime("%Y-%m-%d %H:%M")


def goal_for(issue: dict, repo: dict) -> str:
    """Build the task goal text from a Jira issue.

    Exact shape:
    ```
    <KEY> · <summary>
    Jira: <url> · <issuetype> · Priority: <priority> · Status: <status>
    Labels: a, b · Components: x · Reporter: <name>      (omit a part when empty)

    Description (from Jira):
    <description text, or "(none)">

    <closing line>
    ```

    The closing line depends on repo mode: read-only repos get investigation
    instructions, others get fix instructions.
    """
    from .repos import is_read_only

    key = issue.get("key", "")
    url = issue.get("url", "")
    f = issue.get("fields", {})
    summary = f.get("summary", "")
    issuetype = f.get("issuetype", "")
    priority = f.get("priority", "")
    status = f.get("status", "")
    labels = f.get("labels", [])
    components = f.get("components", [])
    reporter = f.get("reporter", "")
    description = f.get("description", "")

    # Header line
    lines = [f"{key} · {summary}"]

    # Metadata line 1
    metadata = []
    if url:
        metadata.append(f"Jira: {url}")
    if issuetype:
        metadata.append(issuetype)
    if priority:
        metadata.append(f"Priority: {priority}")
    if status:
        metadata.append(f"Status: {status}")
    if metadata:
        lines.append(" · ".join(metadata))

    # Metadata line 2
    metadata2 = []
    if labels:
        metadata2.append(f"Labels: {', '.join(labels)}")
    if components:
        metadata2.append(f"Components: {', '.join(components)}")
    if reporter:
        metadata2.append(f"Reporter: {reporter}")
    if metadata2:
        lines.append(" · ".join(metadata2))

    # Description
    lines.append("")
    lines.append("Description (from Jira):")
    lines.append(description or "(none)")

    # Closing line
    lines.append("")
    repo_name = (repo or {}).get("name", "")
    if is_read_only(repo_name):
        lines.append("Repo is read-only: reproduce from the description, find the likely cause in the code, "
                     "classify it (module, platform, severity, effort) and answer with finish(). Do not change files.")
    else:
        lines.append("Reproduce from the description, fix the cause, add a test, commit and open a merge request (open_mr).")

    return "\n".join(lines)


def find_by_key(key: str) -> Optional[dict]:
    """Find a task by its Jira origin (jira:<key>).

    Used by tools to look up a task by Jira key.
    """
    from .store import get_conn
    conn = get_conn()
    task = conn.execute(
        "SELECT * FROM tasks WHERE origin = ?",
        (f"jira:{key}",)
    ).fetchone()
    conn.close()
    return dict(task) if task else None


def import_issues(jql: str, repo: str, actor: str, max_results: int = 200) -> dict:
    """Create tasks from Jira issues matching *jql*, skipping those already imported.

    Returns:
        {
            "created": [keys],          # Newly created tasks
            "skipped": [keys],          # Already existed (by origin)
            "tasks": [task_ids]         # IDs of created tasks
        }

    Records one journal row per import (not per issue): the action, count of
    created tasks, list of skipped keys in meta.
    """
    from .repos import get_repo
    repo_obj = get_repo(repo) if repo else {}

    # Fetch issues from Jira
    try:
        issues = jira.search(jql, actor, max_results=max_results)
    except jira.JiraError as exc:
        # Journal the error, re-raise
        integrations.record(
            "jira", "create_tasks", actor,
            subject=jql,
            detail=f"search failed: {exc}",
            ok=False, error=str(exc),
            meta={"created": [], "skipped": []}
        )
        raise

    created_keys: List[str] = []
    skipped_keys: List[str] = []
    task_ids: List[str] = []

    # Process each issue
    for issue in issues:
        key = issue.get("key", "")

        # Check if we've already imported this issue
        origin = f"jira:{key}"
        existing = store.get_conn().execute(
            "SELECT id FROM tasks WHERE origin = ?", (origin,)
        ).fetchone()
        store.get_conn().close()

        if existing:
            skipped_keys.append(key)
            continue

        # Create the task
        title = f"{key} · {issue.get('fields', {}).get('summary', '')}"
        goal = goal_for(issue, repo_obj)

        task = store.create_task(
            goal=goal,
            repo=repo,
            title=title,
            origin=origin
        )
        task_id = task["id"]
        created_keys.append(key)
        task_ids.append(task_id)

        # Emit the "created" event for orchestrator
        from .orchestrator import emit
        emit(task_id, "created")

    # Journal one row per import
    integrations.record(
        "jira", "create_tasks", actor,
        subject=jql,
        detail=f"created {len(created_keys)}, skipped {len(skipped_keys)}",
        count=len(created_keys),
        ok=True,
        meta={"created": created_keys, "skipped": skipped_keys}
    )

    return {
        "created": created_keys,
        "skipped": skipped_keys,
        "tasks": task_ids
    }


def poll_once(actor: str = "daemon:jira-poll") -> dict:
    """Poll Jira once for new issues matching the configured JQL.

    Returns the result of import_issues if configured and enabled, or
    {"created": [], "skipped": [], "tasks": []} if disabled.

    Reads config jira.intake = {enabled: bool, jql: str, repo: str,
    interval_seconds: int, max_per_pass: int}.

    On first poll with no last_seen, uses the plain JQL. On subsequent polls,
    appends AND updated >= "<last_seen>" to only fetch changes.

    last_seen is written only after a successful pass.
    """

    intake_config = get_config("jira", {}).get("intake", {})

    if not intake_config.get("enabled"):
        return {"created": [], "skipped": [], "tasks": []}

    jql = (intake_config.get("jql") or "").strip()
    repo = (intake_config.get("repo") or "").strip()
    max_per_pass = int(intake_config.get("max_per_pass", 50) or 50)

    if not jql or not repo:
        return {"created": [], "skipped": [], "tasks": []}

    # Check if Jira is configured
    if not jira.configured():
        return {"created": [], "skipped": [], "tasks": []}

    # Append incremental filter if we've polled before
    last_seen = _get_last_seen(jql)
    search_jql = jql
    if last_seen:
        # Format last_seen from ISO to Jira date format if needed
        try:
            # If it's an ISO string, convert to Jira format
            if "T" in last_seen:
                dt = datetime.fromisoformat(last_seen)
                last_seen = _format_jira_date(dt)
        except (ValueError, AttributeError):
            pass
        search_jql = f"({jql}) AND updated >= \"{last_seen}\""

    # Import the issues
    result = import_issues(search_jql, repo, actor, max_results=max_per_pass)

    # Update last_seen only if the import succeeded
    if result["created"] or result["skipped"]:
        # Find the newest updated timestamp from what we just fetched
        try:
            issues = jira.search(search_jql, actor, max_results=max_per_pass)
            if issues:
                newest = max(
                    (i.get("fields", {}).get("updated", "") for i in issues),
                    default=""
                )
                if newest:
                    _update_last_seen(jql, newest)
        except Exception:  # noqa: BLE001
            # If we can't re-fetch to find the newest, just keep the old last_seen
            pass

    return result


async def poll_forever() -> None:
    """Daemon background loop for Jira intake polling.

    Polls once every jira.intake.interval_seconds (default 300, floor 60).
    Registers a heartbeat and never lets an exception end the loop.
    """

    intake_config = get_config("jira", {}).get("intake", {})
    interval = int(intake_config.get("interval_seconds", DEFAULT_INTERVAL) or DEFAULT_INTERVAL)

    if interval < MIN_INTERVAL:
        logger.warning(
            "jira_intake: interval_seconds=%d is below the %ds floor — using %ds",
            interval, MIN_INTERVAL, MIN_INTERVAL
        )
        interval = MIN_INTERVAL

    heartbeat.register("jira poll", every=interval, note="polling Jira for issues")

    if not intake_config.get("enabled"):
        heartbeat.mark_off("jira poll", "jira.intake.enabled is false")
        logger.info("jira_intake: disabled (jira.intake.enabled is false)")
        return

    logger.info("jira_intake: polling every %ds", interval)

    while True:
        try:
            result = await asyncio.to_thread(poll_once)
            note = f"created {len(result['created'])}"
            if result["skipped"]:
                note += f", skipped {len(result['skipped'])}"
            heartbeat.beat("jira poll", note=note)
            if result["created"] or result.get("error"):
                logger.info("jira_intake: %s", note)
        except Exception:  # noqa: BLE001
            logger.exception("jira_intake: poll failed")

        await asyncio.sleep(interval)
