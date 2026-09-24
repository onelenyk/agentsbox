import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import List, Optional
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from core.config import get_config
from core import mergerequests, sessions
from web.markdown import render_md

router = APIRouter()
logger = logging.getLogger(__name__)

def _asset_v(name: str) -> str:
    """Cache-busting stamp for a static asset — its mtime.

    Static files are served with an ETag, but a browser is free to reuse a
    cached copy without revalidating; editing app.css then shows nothing until
    a manual hard reload. Putting the mtime in the URL makes an edited file a
    different URL, so the change lands on an ordinary refresh.
    """
    try:
        return str(int((Path(__file__).parent / "static" / name).stat().st_mtime))
    except OSError:
        return "0"


def _repo_names() -> list:
    from core.repos import load_repos  # not list_repos: that probes each source's reach
    return [r["name"] for r in load_repos()]


def _worker_agent_names() -> list:
    from core.agents import list_agents
    return [a["name"] for a in list_agents(kind="worker")]


def _tmpl():
    from core.store import task_display_title, message_lane
    tmpl = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    tmpl.env.globals["task_display_title"] = task_display_title
    tmpl.env.globals["message_lane"] = message_lane
    # Called from base.html's sidebar and the quick-task box, so every page
    # gets them without each route remembering to pass them.
    tmpl.env.globals["repo_names"] = _repo_names
    tmpl.env.globals["worker_agent_names"] = _worker_agent_names
    tmpl.env.globals["asset_v"] = _asset_v
    tmpl.env.filters["md"] = render_md
    tmpl.env.filters["when"] = _when
    return tmpl


def _when(iso: str) -> str:
    """A stored UTC stamp as the reader's local "4 Sep 19:17". The page is read
    in a week and a timezone, not in ISO."""
    from datetime import datetime
    try:
        d = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    d = d.astimezone() if d.tzinfo else d
    return f"{d.day} {d.strftime('%b')} {d.strftime('%H:%M')}"

_started = False


def _enrich_sessions(raw_sessions):
    from datetime import datetime, timezone
    from core import store

    now = datetime.now(timezone.utc)
    enriched = []
    for s in raw_sessions:
        s = dict(s)
        try:
            started = datetime.fromisoformat(s["created_at"])
            s["elapsed_mins"] = int((now - started).total_seconds() // 60)
        except Exception:
            s["elapsed_mins"] = 0
        # Seconds since the runner last wrote anything. None = not a byte yet,
        # which on a run that has been going a while is the interesting case.
        s["quiet_s"] = None
        if s.get("last_output_at"):
            try:
                s["quiet_s"] = int((now - datetime.fromisoformat(s["last_output_at"])).total_seconds())
            except Exception:  # noqa: BLE001
                s["quiet_s"] = None
        task = store.get_task(s["task_id"])
        s["task_title"] = store.task_display_title(task) if task else (s["task_id"] or "")
        enriched.append(s)
    return enriched


async def startup():
    """Daemon boot — registered as the app lifespan in web.app."""
    global _started
    if _started:
        # Expected under uvicorn reload/multiple mounts — a handled case, not a warning.
        logger.info("startup() fired again — skipping (avoids a duplicate dispatch loop)")
        return
    _started = True

    # One call owns the schema: every init in order (daemon is self-sufficient —
    # runners, agents, usage, MRs, autopilots all seed
    # themselves), then a PRAGMA user_version stamp so a future non-additive
    # migration has something to compare against.
    from core.migrations import migrate_all
    migrate_all()

    import web.agent_mcp as agent_mcp
    from core import orchestrator
    agent_mcp.on_worker_finished = lambda task_id: orchestrator.emit(task_id, "worker_finished")
    reconciled = orchestrator.reconcile_on_boot()
    if reconciled:
        logger.info("orchestrator: resumed %d task(s) orphaned by restart", reconciled)

    from core import notify, preflight
    pf = preflight.check()
    if pf["ok"]:
        logger.info("preflight: ready (runner=%s)", pf["runner"])
    else:
        logger.warning("preflight: NOT ready to run work — %s", pf["reason"])
        # Say it outward too: a daemon that boots unable to run work, at
        # 03:00 under launchd, is exactly the state nobody is watching for.
        # wait=True — a daemon thread this early may not outlive startup.
        notify.send("preflight_not_ready", reason=pf["reason"], wait=True)

    # Name the active spend caps once, loudly — a cap someone believes is on
    # while config says otherwise is how a quiet $100 night happens.
    from core.config import get_config
    from core.orchestrator import DEFAULT_DAILY_BUDGET_USD
    _task_cap = float(get_config("task_budget_usd", 0) or 0)
    _raw_daily = get_config("daily_budget_usd", DEFAULT_DAILY_BUDGET_USD)
    _daily_cap = float(0 if _raw_daily is None else _raw_daily)
    logger.info("budgets: per-task %s · daily %s",
                f"${_task_cap:.2f}" if _task_cap > 0 else "OFF",
                f"${_daily_cap:.2f}" if _daily_cap > 0 else "OFF (explicit 0 in config)")

    # Name the active spend caps once, loudly — a cap someone believes is on
    # while config says otherwise is how a quiet $100 night happens.
    from core.config import get_config
    from core.orchestrator import DEFAULT_DAILY_BUDGET_USD
    _task_cap = float(get_config("task_budget_usd", 0) or 0)
    _raw_daily = get_config("daily_budget_usd", DEFAULT_DAILY_BUDGET_USD)
    _daily_cap = float(0 if _raw_daily is None else _raw_daily)
    logger.info("budgets: per-task %s · daily %s",
                f"${_task_cap:.2f}" if _task_cap > 0 else "OFF",
                f"${_daily_cap:.2f}" if _daily_cap > 0 else "OFF (explicit 0 in config)")

    from core import lifecycle
    lifecycle.start_loop(orchestrator.dispatch_forever(), "dispatcher")
    # reconcile_on_boot only covers the restart. Keep guarding at runtime, so a
    # task created without an event recovers in minutes instead of waiting for
    # the next daemon restart.
    lifecycle.start_loop(orchestrator.watchdog_forever(), "watchdog")

    from core import autopilot
    lifecycle.start_loop(autopilot.run_forever(), "autopilot")

    # Ask the forges what happened to the pull requests we opened. A webhook
    # would be cheaper, but it needs the forge to reach us and this daemon
    # listens on loopback — see core.forge_poll.
    from core import forge_poll
    lifecycle.start_loop(forge_poll.poll_forever(), "forge poll")

    # A consistent snapshot at boot and daily after — restore is only as good
    # as the last time anything actually called snapshot().
    from core import backup
    lifecycle.start_loop(backup.backup_forever(), "backup")

    # The runner's own session store grows with every worker run and is never
    # read back (core.runner_store) — prune it so it cannot quietly eat the disk.
    from core import runner_store
    lifecycle.start_loop(runner_store.prune_forever(), "runner-store")

    # How much of each subscription is left, and the tasks waiting on a window
    # to reset (core.limits).
    from core import limits
    lifecycle.start_loop(limits.poll_forever(), "limits")

    # Started by the desktop app: end with it, even when it cannot say so.
    parent = os.environ.get("SISYPHUS_PARENT_PID")
    if parent and parent.isdigit():
        lifecycle.start_loop(lifecycle.watch_parent(int(parent)), "parent watch")
        logger.info("owned by the app (pid %s) — exits when it does", parent)

@router.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse('<meta http-equiv="refresh" content="0;url=/dashboard">')

@router.get("/system", response_class=HTMLResponse)
async def system_page():
    """The System door in the 3-item nav — lands on its first tab."""
    return HTMLResponse('<meta http-equiv="refresh" content="0;url=/agents">')

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    return _tmpl().TemplateResponse(request, "dashboard.html", {"active_page": "dashboard"})

@router.get("/api/dashboard/view", response_class=HTMLResponse)
async def dashboard_view(request: Request):
    from core.stats import dashboard
    return _tmpl().TemplateResponse(request, "dashboard_view.html", {
        "d": dashboard(),
        "live_sessions": _enrich_sessions(sessions.list_active()),
    })

@router.get("/api/inbox/view", response_class=HTMLResponse)
async def inbox_view(request: Request):
    """The Needs-you feed: waiting tasks (with the open question + inline reply)
    and done tasks whose merge request is still open."""
    from core import store

    waiting = []
    for t in store.list_tasks(status="waiting"):
        q = next((m["text"] for m in reversed(store.get_thread(t["id"]))
                  if m["kind"] == "question"), "")
        waiting.append({"task": t, "question": q[:120] + "…" if len(q) > 120 else q})

    open_mr = {}
    for mr in mergerequests.list_mrs(status="open"):
        open_mr.setdefault(mr["task_id"], mr)   # newest first — keep the latest
    to_merge = [{"task": t, "mr": open_mr[t["id"]]}
                for t in store.list_tasks(status="done") if t["id"] in open_mr]

    return _tmpl().TemplateResponse(request, "inbox_view.html",
                                    {"waiting": waiting, "to_merge": to_merge})

@router.get("/api/notifications/poll")
async def notifications_poll():
    """Recent task state changes worth a browser notification: tasks that just
    finished (done/failed) or now need input (waiting). Bounded to the last 15
    minutes so the payload stays small; the client dedupes by key."""
    from datetime import datetime, timedelta, timezone
    from core import store

    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    # One bounded query, not three unbounded ones: this used to pull every
    # waiting, done and failed task in the database — the whole archive — and
    # then throw away all but the last fifteen minutes, on a timer.
    conn = store.get_conn()
    try:
        rows = conn.execute(
            "SELECT id, status, goal, updated_at FROM tasks "
            "WHERE status IN ('waiting', 'done', 'failed') AND updated_at >= ? "
            "ORDER BY updated_at", (cutoff,)).fetchall()
    finally:
        conn.close()
    # Say which kind of "wants you" it is (core.asks): a broken runner and a
    # question are different reasons to come back, and a finished task whose
    # branch now waits is not merely "completed".
    from core import asks
    open_mr = {m["task_id"] for m in mergerequests.list_mrs() if m["status"] == "open"}
    items = []
    for r in rows:
        verb = None
        if r["status"] == "waiting":
            kind = asks.for_waiting(dict(r), store.get_thread(r["id"]))["kind"]
            verb = {"stuck": "is stuck", "budget": "hit a spending cap",
                    "question": "asks you something"}[kind]
        elif r["status"] == "done" and r["id"] in open_mr:
            verb = "is ready to merge"
        items.append({"key": f"{r['id']}:{r['status']}:{r['updated_at']}",
                      "task_id": r["id"], "status": r["status"], "goal": r["goal"], "verb": verb})
    return {"items": items}

def _loops() -> list:
    """The daemon's background loops, each from a heartbeat it actually
    stamped (core.heartbeat) — the dispatcher with its queue depth."""
    from core import heartbeat, orchestrator
    loops = heartbeat.list_all()
    for w in loops:
        if w["kind"] == "event":
            w["depth"] = orchestrator.queue_depth()
    return loops


@router.get("/runtime", response_class=HTMLResponse)
async def runtime_page(request: Request):
    import os
    from core.runners import list_runners

    cfg = get_config()

    # Actual workspace root the daemon is running with (set via env in app.py),
    # not the bare config default which is misleading.
    workspace_root = os.environ.get("WORKSPACE_ROOT") or cfg.get("workspace_root") or "(unset)"

    # The default runner is what actually drives agents now (raw LLM HTTP was
    # removed), so surface it instead of the stale seed-only default_model.
    default_runner = None
    for r in list_runners():
        if r.get("is_default"):
            default_runner = r
            break

    # Only show integration base URLs that are actually configured — unresolved
    # ${ENV} placeholders are noise.
    def _clean(v):
        v = (v or "").strip()
        return v if v and not v.startswith("${") else None

    integrations = {}
    for key in ("jira", "gitlab", "github"):
        url = _clean((cfg.get(key) or {}).get("base_url"))
        if url:
            integrations[key] = url

    # This is the page you open when something looks wrong, so it has to answer
    # "is the process I'm talking to actually running my current code?" — the
    # ghost-daemon question. Config alone can't: a stale process serves stale
    # config just as confidently.
    import datetime as _dt, os as _os
    from core import worker as _worker
    from web.api import _GIT_COMMIT, _PROCESS_START_TIME
    started = _dt.datetime.fromisoformat(_PROCESS_START_TIME)
    uptime_s = int((_dt.datetime.now(_dt.timezone.utc) - started).total_seconds())
    d, rem = divmod(uptime_s, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    process = {
        "pid": _os.getpid(),
        "git_commit": _GIT_COMMIT or "unknown",
        "started": _PROCESS_START_TIME[:19].replace("T", " "),
        "uptime": (f"{d}d {h}h" if d else (f"{h}h {m}m" if h else f"{m}m")),
        "workers_in_flight": len(sessions.list_active()),
    }

    return _tmpl().TemplateResponse(
        request,
        "runtime.html",
        {
            "active_page": "runtime",
            "config": cfg,
            "workspace_root": workspace_root,
            "default_runner": default_runner,
            "integrations": integrations,
            "process": process,
            # from the code that enforces them, not from a config key — see
            # core.worker for why this page used to advertise a 6x-wrong number
            "limits": {"worker_timeout": _worker.WORKER_TIMEOUT_SECONDS,
                       "worker_idle_timeout": _worker.WORKER_IDLE_TIMEOUT_SECONDS},
            "runner_store": _runner_store_view(),
            "loops": _loops(),
        },
    )

def _runner_store_view() -> dict:
    from core import runner_store
    from core.config import get_config
    st = runner_store.stats()
    st["size_h"] = runner_store.human_size(st["bytes"])
    st["keep_days"] = int(get_config("runner_store.keep_days", runner_store.DEFAULT_KEEP_DAYS) or 0)
    return st


@router.post("/api/runtime/prune-runner-store", response_class=HTMLResponse)
async def runtime_prune_runner_store(request: Request):
    """Prune the runner's session store now (the daily loop does it too)."""
    from core import runner_store
    res = await asyncio.to_thread(runner_store.prune,
                                  _runner_store_view()["keep_days"] or runner_store.DEFAULT_KEEP_DAYS)
    view = _runner_store_view()
    view["flash"] = (f"skipped — {res['skipped']}" if res["skipped"] else
                     f"removed {res['removed']} session(s), freed {runner_store.human_size(res['freed'])}")
    return _tmpl().TemplateResponse(request, "runtime_store.html", {"runner_store": view})


@router.get("/repos", response_class=HTMLResponse)
async def repos_page(request: Request):
    return _tmpl().TemplateResponse(request, "repos.html", {"active_page": "repos"})

@router.get("/api/repos/list", response_class=HTMLResponse)
async def repos_list(request: Request):
    from core.repos import list_repos
    from core import store
    repos = list_repos()
    tasks = [t for t in store.list_tasks() if not t.get("parent_id")]
    for r in repos:  # annotate each repo with its task progress (Projects-style)
        rt = [t for t in tasks if t.get("repo") == r["name"]]
        r["task_total"] = len(rt)
        r["task_done"] = sum(1 for t in rt if t["status"] == "done")
    return _tmpl().TemplateResponse(request, "repos_list.html", {"repos": repos})

@router.get("/repos/{name}", response_class=HTMLResponse)
async def repo_detail_page(request: Request, name: str):
    from core.repos import get_repo, REPOS_DIR
    from core import repo_context, store
    repo = get_repo(name)
    if not repo:
        return HTMLResponse('<div class="empty">Repository not found</div>', status_code=404)
    bare = REPOS_DIR / name
    repo["cloned"] = bare.exists() and (bare / "HEAD").exists()
    source = repo.get("source", "")
    tasks = [t for t in store.list_tasks() if t.get("repo") == name and not t.get("parent_id")]
    return _tmpl().TemplateResponse(request, "repos_detail.html", {
        "active_page": "repos", "repo": repo,
        "resources": repo_context.resources(source),
        "summary": repo_context.summary(source),
        "tasks": tasks,
        "done": sum(1 for t in tasks if t["status"] == "done"),
    })

@router.get("/api/repos/{name}/resource", response_class=HTMLResponse)
async def repo_resource_view(request: Request, name: str, path: str):
    from core.repos import get_repo
    from core import repo_context
    repo = get_repo(name)
    content = repo_context.read_resource(repo["source"], path) if repo else None
    return _tmpl().TemplateResponse(request, "repo_resource_modal.html",
        {"name": name, "path": path, "content": content})

@router.get("/api/repos/new-form", response_class=HTMLResponse)
async def repos_new_form(request: Request):
    return _tmpl().TemplateResponse(request, "repos_new.html", {})

@router.post("/api/repos")
async def repos_add(name: str = Form(...), source: str = Form(...), platform: str = Form(""), branch: str = Form("main")):
    from core.repos import add_repo
    from fastapi.responses import Response
    try:
        add_repo(name, source, platform, branch)
        resp = Response(status_code=200)
        resp.headers["HX-Trigger"] = "reposUpdated"
        return resp
    except Exception as e:
        return HTMLResponse(f'<div class="error-banner">Error: {e}</div>')

@router.get("/api/repos/health")
async def repos_health():
    from core.repos import list_repos
    repos = list_repos()
    total = len(repos)
    cloned = sum(1 for r in repos if r.get("cloned"))
    unreachable = sum(1 for r in repos if r.get("reach") == "missing")
    return {"total": total, "cloned": cloned, "pending": total - cloned,
            "unreachable": unreachable}



@router.get("/api/repos/{name}/edit-form", response_class=HTMLResponse)
async def repos_edit_form(request: Request, name: str):
    from core.repos import get_repo
    repo = get_repo(name)
    if not repo:
        return HTMLResponse("<p>Repo not found</p>")
    return _tmpl().TemplateResponse(request, "repos_edit.html", {"repo": repo})

@router.post("/api/repos/{name}/edit")
async def repos_edit(name: str, source: str = Form(""), platform: str = Form(""), branch: str = Form(""), auto_merge: str = Form(""), read_only: str = Form("")):
    from core.repos import update_repo_config
    from fastapi.responses import Response
    update_repo_config(name, source or None, platform, branch or None,
                       auto_merge=(auto_merge == "1"),
                       mode="read-only" if read_only == "read-only" else "")
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "reposUpdated"
    return resp


@router.delete("/api/repos/{name}")
@router.post("/api/repos/{name}/delete")
async def repos_delete(name: str):
    from core.repos import delete_repo
    from fastapi.responses import Response
    delete_repo(name)
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "reposUpdated"
    return resp


@router.post("/api/repos/{name}/update")
async def repos_update(name: str):
    from core.repos import update_repo
    from fastapi.responses import Response
    update_repo(name)
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "reposUpdated"
    return resp


def _repo_error(message: str):
    """A 400 the repos page can actually show.

    Same channel and same reason as _runner_error: a bare 400 reaches the user
    as "Bad Request", which names nothing. The connection check has real
    failures worth naming (unknown repo, git missing), and they are not stage
    failures — a stage failure is a successful check with a bad answer and comes
    back as a rendered 200.
    """
    from fastapi.responses import Response
    resp = Response(status_code=400)
    resp.headers["HX-Trigger"] = json.dumps({"repoError": message})
    return resp


async def _render_check(request: Request, **kwargs):
    """Run one connection check off the event loop and render its report.

    check_connection shells out to git and dials the forge; both block, and this
    runs inside the daemon that is also serving the task pages.
    """
    from core.repo_check import check_connection
    try:
        res = await asyncio.to_thread(check_connection, **kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.exception("repo connection check failed to run")
        return _repo_error(f"the connection check could not run: {exc}")
    return _tmpl().TemplateResponse(request, "repos_check.html", {"c": res})


@router.post("/api/repos/check", response_class=HTMLResponse)
async def repos_check_form(request: Request, name: str = Form(""), source: str = Form(""),
                           platform: str = Form(""), branch: str = Form("main")):
    """Check the fields in the add/edit form, before anything is saved.

    The point of the whole feature is that "GitLab" in a dropdown is a claim, so
    the most useful moment to test it is while the user is still typing it.
    """
    return await _render_check(request, name=name.strip(), source=source.strip(),
                               platform=platform.strip(), branch=branch.strip())


@router.post("/api/repos/{name}/check", response_class=HTMLResponse)
async def repos_check(request: Request, name: str):
    """Prove this repo can do what it was configured for — read, authenticate, push.

    Nothing else verifies any of it. A repo with a GitLab platform and no token
    looks identical to a working one until core.providers tries to open the MR,
    which happens after an agent has spent a task's worth of tokens writing the
    code. This asks the questions up front and reports which one failed.
    """
    from core.repos import get_repo
    if not get_repo(name):
        return _repo_error(f"no repo named {name!r}")
    return await _render_check(request, name=name)

# ── Runner CRUD ────────────────────────────────────────────────────────

@router.get("/backups", response_class=HTMLResponse)
async def backups_page(request: Request):
    # Snapshots live on the Daemon page now; old links and bookmarks land there.
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/runtime#backups", status_code=307)

@router.get("/api/backups/list", response_class=HTMLResponse)
async def backups_list_view(request: Request):
    from core import backup
    return _tmpl().TemplateResponse(
        request,
        "backups_list.html",
        {"backups": backup.list_backups(), "retention": backup.RETENTION},
    )

def _backups_list_response(request, flash=None, error=None):
    from core import backup
    resp = _tmpl().TemplateResponse(
        request,
        "backups_list.html",
        {"backups": backup.list_backups(),
         "retention": backup.RETENTION, "flash": flash, "error": error},
    )
    return resp

@router.post("/api/backups/create", response_class=HTMLResponse)
async def backups_create(request: Request):
    from core import backup
    try:
        info = backup.snapshot(label="manual")
        return _backups_list_response(request, flash=f"Backup created — {info['name']} ({info['size_h']})")
    except Exception as exc:  # noqa: BLE001
        return _backups_list_response(request, error=f"Backup failed: {exc}")

@router.post("/api/backups/{name}/restore", response_class=HTMLResponse)
async def backups_restore(request: Request, name: str):
    from core import backup
    try:
        res = backup.restore(name)
        return _backups_list_response(
            request,
            flash=f"Restored from {res['restored']}. A safety copy of the previous state was saved as {res['safety']}.",
        )
    except Exception as exc:  # noqa: BLE001
        return _backups_list_response(request, error=f"Restore failed: {exc}")

@router.post("/api/backups/{name}/delete", response_class=HTMLResponse)
async def backups_delete(request: Request, name: str):
    from core import backup
    ok = backup.delete_backup(name)
    return _backups_list_response(
        request,
        flash=f"Deleted {name}" if ok else None,
        error=None if ok else f"Could not delete {name}",
    )

@router.get("/autopilots", response_class=HTMLResponse)
async def autopilots_page(request: Request):
    return _tmpl().TemplateResponse(request, "autopilots.html", {"active_page": "autopilots"})

def _autopilots_list_response(request, flash=None, error=None):
    from core.autopilot import explain, list_autopilots, schedule_problem
    rows = list_autopilots()
    for a in rows:
        # An automation whose clock can never fire it used to render the same
        # "—" as a webhook one and a paused one. See autopilot.schedule_problem.
        a["problem"] = schedule_problem(a)
        a["explained"] = explain(a)
    return _tmpl().TemplateResponse(
        request,
        "autopilots_list.html",
        {"autopilots": rows, "flash": flash, "error": error},
    )

@router.get("/api/autopilots/list", response_class=HTMLResponse)
async def autopilots_list_view(request: Request):
    return _autopilots_list_response(request)

@router.get("/api/autopilots/new-form", response_class=HTMLResponse)
async def autopilots_new_form(request: Request):
    from core.repos import list_repos
    from core.agents import list_agents
    return _tmpl().TemplateResponse(request, "autopilots_form.html",
        {"ap": None, "repos": list_repos(), "agents": list_agents(kind="worker")})

@router.get("/api/autopilots/{ap_id}/edit-form", response_class=HTMLResponse)
async def autopilots_edit_form(request: Request, ap_id: str):
    from core.autopilot import get_autopilot
    from core.repos import list_repos
    from core.agents import list_agents
    return _tmpl().TemplateResponse(request, "autopilots_form.html",
        {"ap": get_autopilot(ap_id), "repos": list_repos(), "agents": list_agents(kind="worker")})

def _ap_form_fields(name, description, trigger_type, cron, interval_seconds,
                    webhook_platform, webhook_event, goal_template, repo, agent, skip_if_running):
    return dict(name=name, description=description, trigger_type=trigger_type, cron=cron,
                interval_seconds=int(interval_seconds or 0), webhook_platform=webhook_platform,
                webhook_event=webhook_event, goal_template=goal_template, repo=repo,
                agent=agent, skip_if_running=1 if skip_if_running else 0)

@router.post("/api/autopilots", response_class=HTMLResponse)
async def autopilots_create(request: Request, name: str = Form(...), description: str = Form(""),
        trigger_type: str = Form("schedule"), cron: str = Form(""), interval_seconds: str = Form("0"),
        webhook_platform: str = Form(""), webhook_event: str = Form(""), goal_template: str = Form(""),
        repo: str = Form(""), agent: str = Form(""), skip_if_running: str = Form("")):
    from core.autopilot import create_autopilot, schedule_problem
    ap = create_autopilot(**_ap_form_fields(name, description, trigger_type, cron, interval_seconds,
        webhook_platform, webhook_event, goal_template, repo, agent, skip_if_running))
    # Saved either way — losing a typed form to reject one field would be worse
    # — but a schedule that can never fire is said out loud, at the moment the
    # operator is still looking, not only on the card afterwards.
    problem = schedule_problem(ap or {})
    if problem:
        return _autopilots_list_response(request, error=f"Created “{name}”, but {problem}")
    return _autopilots_list_response(request, flash=f"Created “{name}”")

@router.post("/api/autopilots/{ap_id}", response_class=HTMLResponse)
async def autopilots_update(request: Request, ap_id: str, name: str = Form(...), description: str = Form(""),
        trigger_type: str = Form("schedule"), cron: str = Form(""), interval_seconds: str = Form("0"),
        webhook_platform: str = Form(""), webhook_event: str = Form(""), goal_template: str = Form(""),
        repo: str = Form(""), agent: str = Form(""), skip_if_running: str = Form("")):
    from core.autopilot import update_autopilot, schedule_problem
    ap = update_autopilot(ap_id, **_ap_form_fields(name, description, trigger_type, cron, interval_seconds,
        webhook_platform, webhook_event, goal_template, repo, agent, skip_if_running))
    problem = schedule_problem(ap or {})
    if problem:
        return _autopilots_list_response(request, error=f"Saved “{name}”, but {problem}")
    return _autopilots_list_response(request, flash=f"Saved “{name}”")

@router.post("/api/autopilots/{ap_id}/run", response_class=HTMLResponse)
async def autopilots_run(request: Request, ap_id: str):
    from core.autopilot import get_autopilot, fire
    ap = get_autopilot(ap_id)
    if not ap:
        return _autopilots_list_response(request, error="autopilot not found")
    tid = fire(ap)
    return _autopilots_list_response(request,
        flash=(f"Fired → task {tid}" if tid else "Skipped — previous run still running"))

@router.post("/api/autopilots/{ap_id}/toggle", response_class=HTMLResponse)
async def autopilots_toggle(request: Request, ap_id: str):
    from core.autopilot import get_autopilot, set_enabled
    ap = get_autopilot(ap_id)
    if ap:
        set_enabled(ap_id, not ap["enabled"])
    return _autopilots_list_response(request)

@router.post("/api/autopilots/{ap_id}/delete", response_class=HTMLResponse)
async def autopilots_delete(request: Request, ap_id: str):
    from core.autopilot import delete_autopilot
    delete_autopilot(ap_id)
    return _autopilots_list_response(request)

@router.get("/runners", response_class=HTMLResponse)
async def runners_page(request: Request):
    return _tmpl().TemplateResponse(request, "runners.html", {"active_page": "runners"})

@router.get("/api/runners/list", response_class=HTMLResponse)
async def runners_list_view(request: Request):
    from core.runners import auth_mode, list_runners
    runners = [{**r, "mode": auth_mode(r)} for r in list_runners()]
    return _tmpl().TemplateResponse(request, "runners_list.html", {"runners": runners})

async def _runners_plan_response(request: Request, error: str = ""):
    """What runs the agents: subscription limits and the model classes."""
    # A cold model catalog spawns the runner's CLI (~8s for opencode); off the
    # event loop, or every other page waits on it.
    ctx = await asyncio.to_thread(_runners_plan_context, error)
    return _tmpl().TemplateResponse(request, "runners_plan.html", ctx)

def _runners_plan_context(error: str = "") -> dict:
    from core import limits, model_classes, runner_catalog
    from core.runners import list_runners
    runners = list_runners()
    # Model suggestions per runner, for the "add an option" field.
    suggestions = {}
    for r in runners:
        try:
            groups = runner_catalog.models(r.get("type") or "opencode", r.get("binary") or "")
            suggestions[r["id"]] = [m["id"] for g in groups for m in g["models"]]
        except Exception:  # noqa: BLE001 — suggestions only; typing a model still works
            suggestions[r["id"]] = []
    return {
        "subscriptions": limits.view(),
        "classes": model_classes.view(),
        "runners": runners,
        "suggestions": suggestions,
        "error": error,
    }

@router.get("/api/runners/plan", response_class=HTMLResponse)
async def runners_plan_view(request: Request):
    return await _runners_plan_response(request)

@router.post("/api/limits/refresh", response_class=HTMLResponse)
async def limits_refresh(request: Request):
    from core import limits
    err = await asyncio.to_thread(limits.poll_opencode_go)
    return await _runners_plan_response(request, error=f"OpenCode Go: {err}" if err else "")

@router.post("/api/classes/{name}/add", response_class=HTMLResponse)
async def classes_add(request: Request, name: str, runner_id: str = Form(...), model: str = Form("")):
    from core import model_classes
    try:
        model_classes.add(name, runner_id, model.strip())
    except ValueError as exc:
        return await _runners_plan_response(request, error=str(exc))
    return await _runners_plan_response(request)

@router.post("/api/classes/{name}/{index}/{op}", response_class=HTMLResponse)
async def classes_edit(request: Request, name: str, index: int, op: str):
    from core import model_classes
    try:
        if op == "up":
            model_classes.move(name, index, -1)
        elif op == "down":
            model_classes.move(name, index, 1)
        elif op == "remove":
            model_classes.remove(name, index)
        else:
            return await _runners_plan_response(request, error=f"unknown change {op!r}")
    except ValueError as exc:
        return await _runners_plan_response(request, error=str(exc))
    return await _runners_plan_response(request)

def _runner_error(message: str):
    """A 400 the runners page can actually show.

    Runner writes now reject unsupported types and duplicate names. Letting
    those escape as a 500 gave the UI's generic htmx handler nothing but
    "Internal Server Error"; this rides the same HX-Trigger channel the success
    path uses, so base.html can flash the real reason.
    """
    from fastapi.responses import Response
    resp = Response(status_code=400)
    resp.headers["HX-Trigger"] = json.dumps({"runnerError": message})
    return resp


RUNNER_LABELS = {"opencode": "opencode", "claude-cli": "Claude Code"}


def _runner_kinds() -> list:
    """Each launchable runner type with the binary this machine has for it."""
    from core.runner_catalog import version
    from core.runner_detect import KNOWN, resolve
    from core.runners import RUNNER_TYPES
    binary_of = {t: b for b, t in KNOWN.items()}
    out = []
    for t in RUNNER_TYPES:
        b = binary_of.get(t, t)
        path = resolve(b)
        out.append({"id": t, "label": RUNNER_LABELS.get(t, t), "binary": b,
                    "path": path or "", "version": version(path) if path else ""})
    return out


def _runner_form(request: Request, runner: Optional[dict]):
    kinds = _runner_kinds()
    if runner:
        selected = runner.get("type") or "opencode"
    else:
        selected = next((k["id"] for k in kinds if k["path"]), kinds[0]["id"])
    return _tmpl().TemplateResponse(request, "runner_form.html",
                                    {"kinds": kinds, "selected": selected, "runner": runner})


@router.get("/api/runners/new-form", response_class=HTMLResponse)
async def runners_new_form(request: Request):
    return await asyncio.to_thread(_runner_form, request, None)


@router.get("/api/runners/form-body", response_class=HTMLResponse)
async def runners_form_body(request: Request, type: str = "opencode", runner_id: str = ""):
    """The part of the runner form that depends on the runner type: its own
    sign-in, the models it accepts, and how a key would reach it."""
    from core import keychain, runner_catalog
    from core.runner_detect import KNOWN, resolve
    from core.runners import RUNNER_TYPES, auth_mode, get_runner, list_runners

    rtype = type if type in RUNNER_TYPES else "opencode"
    runner = get_runner(runner_id) if runner_id else None
    same = bool(runner and (runner.get("type") or "opencode") == rtype)
    binary = runner["binary"] if same else {t: b for b, t in KNOWN.items()}.get(rtype, rtype)
    resolved = resolve(binary) or ""

    def probe():
        groups = runner_catalog.models(rtype, resolved) if resolved else []
        login = (runner_catalog.login_status(rtype, resolved) if resolved
                 else {"ok": False, "summary": "Runner not found", "detail": ""})
        return groups, login
    groups, login = await asyncio.to_thread(probe)

    if runner:
        name = runner["name"]
    else:
        taken = {r["name"] for r in list_runners()}
        name, n = binary.split("/")[-1], 2
        while name in taken:
            name, n = f"{binary.split('/')[-1]}-{n}", n + 1
    model = (runner.get("model") or "") if same else ""
    listed = model if runner_catalog.find(groups, model) else ""
    mode = auth_mode(runner) if same else "login"
    has_key = bool(same and mode == "key" and await asyncio.to_thread(keychain.read, runner["id"]))
    if rtype == "claude-cli":
        key_hint = "Handed to Claude Code as ANTHROPIC_API_KEY — billed to that key, not your subscription."
        default_label = "Claude Code's own setting"
        placeholder = "claude-sonnet-5"
    else:
        key_hint = ("Handed to opencode in the variable the chosen model's provider reads "
                    "(OPENROUTER_API_KEY for openrouter/…).")
        default_label = "opencode's configured model"
        placeholder = "provider/model"
    return _tmpl().TemplateResponse(request, "runner_form_body.html", {
        "rtype": rtype, "label": RUNNER_LABELS.get(rtype, rtype), "name": name,
        "binary": binary, "resolved": resolved, "login": login, "groups": groups,
        "count": sum(len(g["models"]) for g in groups), "listed": listed,
        "custom": "" if listed else model, "custom_placeholder": placeholder,
        "default_label": default_label, "mode": mode, "has_key": has_key,
        "key_hint": key_hint,
        "api_key_env": (runner.get("api_key_env") or "") if same and mode == "env" else "",
        "base_url": (runner.get("base_url") or "") if same else "",
    })


@router.get("/api/runners/detect", response_class=HTMLResponse)
async def runners_detect_view(request: Request):
    from core.runner_detect import detect, resolve
    from core.runner_profiles import suggested_config
    from core.runners import list_runners
    configured = list_runners()
    configured_names = {r["name"] for r in configured}
    # Compare where each binary actually RESOLVES, not the string in the row.
    # A row stores whatever was typed — often an absolute path — while detect()
    # reports the bare name, so a plain string match reported an already
    # configured runtime as missing and offered to add a duplicate.
    configured_bins = {r["binary"] for r in configured}
    configured_paths = {p for p in (resolve(r["binary"]) for r in configured) if p}
    detected = detect()
    for d in detected:
        d["configured"] = (
            d["binary"] in configured_bins or (d.get("path") in configured_paths)
        )
        # Prefill per type rather than assuming OpenRouter for everything.
        d.update(suggested_config(d["type"]))
        # Names are UNIQUE; suggest a free one so "+ Add" can't 400 on a
        # collision with an unrelated row that happens to hold the name.
        name = d["name"]
        if name in configured_names:
            n = 2
            while f"{name}-{n}" in configured_names:
                n += 1
            name = f"{name}-{n}"
        d["suggested_name"] = name
    conf = [{"name": r["name"], "binary": r["binary"], "resolved": resolve(r["binary"])}
            for r in configured]
    return _tmpl().TemplateResponse(
        request,
        "runners_detect.html",
        {"detected": detected, "configured": conf},
    )

def _runner_auth(rtype: str, model: str, auth_mode: str, api_key: str, api_key_env: str,
                 runner_id: str = "") -> tuple:
    """(auth_mode, api_key_env, key_to_store) for a submitted runner form, or
    raise ValueError with the reason to show. A form without auth_mode (the
    MCP tools, older clients) means what it always did: a variable if one is
    named, else the CLI's own sign-in."""
    from core import keychain
    from core.runner_catalog import provider_env
    mode = (auth_mode or "").strip() or ("env" if api_key_env.strip() else "login")
    if mode == "login":
        return mode, "", ""
    if mode == "env":
        if not api_key_env.strip():
            raise ValueError("name the environment variable that holds the key")
        return mode, api_key_env.strip(), ""
    if mode == "key":
        key = (api_key or "").strip()
        if key and not keychain.valid_key(key):
            raise ValueError("that does not look like an API key (no spaces or quotes)")
        if not key and not (runner_id and keychain.read(runner_id)):
            raise ValueError("paste the API key")
        return mode, provider_env(rtype, model), key
    raise ValueError(f"unknown sign-in mode {mode!r}")


@router.post("/api/runners")
async def runners_add(name: str = Form(...), binary: str = Form(...), type: str = Form("opencode"),
                       model: str = Form(""), model_custom: str = Form(""), api_key_env: str = Form(""),
                       base_url: str = Form(""), auth_mode: str = Form(""), api_key: str = Form("")):
    import sqlite3
    from core import keychain
    from core.runners import add_runner, delete_runner
    from fastapi.responses import Response
    model = (model_custom or model or "").strip()
    try:
        mode, key_env, key = _runner_auth(type, model, auth_mode, api_key, api_key_env)
        row = add_runner(name, binary, type, model, key_env, base_url, auth_mode=mode)
    except ValueError as e:
        return _runner_error(str(e))
    except sqlite3.IntegrityError:
        return _runner_error(f"a runner named {name!r} already exists")
    if key:
        try:
            keychain.store(row["id"], key)
        except keychain.KeychainError as e:
            delete_runner(row["id"])
            return _runner_error(str(e))
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "runnersUpdated"
    return resp

@router.get("/api/runners/{runner_id}/edit-form", response_class=HTMLResponse)
async def runners_edit_form(request: Request, runner_id: str):
    from core.runners import get_runner
    runner = get_runner(runner_id)
    if not runner:
        return HTMLResponse("<p>Runner not found</p>")
    return await asyncio.to_thread(_runner_form, request, runner)

@router.post("/api/runners/{runner_id}/edit")
async def runners_edit(runner_id: str, name: str = Form(""), binary: str = Form(""),
                        type: str = Form(""), model: str = Form(""), model_custom: str = Form(""),
                        api_key_env: str = Form(""), base_url: str = Form(""),
                        auth_mode: str = Form(""), api_key: str = Form("")):
    import sqlite3
    from core import keychain
    from core.runners import get_runner, update_runner
    from fastapi.responses import Response
    current = get_runner(runner_id)
    if not current:
        return _runner_error("runner not found")
    fields = {}
    if name: fields["name"] = name
    if binary: fields["binary"] = binary
    if type: fields["type"] = type
    model = (model_custom or model or "").strip()
    # Blank means "clear it", not "leave it alone" — model and base_url are
    # optional and routinely need removing; the form submits every field on
    # every save. name/binary/type stay skip-if-blank: they are required.
    fields["model"] = model
    fields["base_url"] = base_url
    try:
        mode, key_env, key = _runner_auth(type or current.get("type") or "opencode", model,
                                          auth_mode, api_key, api_key_env, runner_id)
        fields["auth_mode"], fields["api_key_env"] = mode, key_env
        if key:
            keychain.store(runner_id, key)
        update_runner(runner_id, **fields)
    except (ValueError, keychain.KeychainError) as e:
        return _runner_error(str(e))
    except sqlite3.IntegrityError:
        return _runner_error(f"a runner named {name!r} already exists")
    if mode != "key":
        keychain.delete(runner_id)
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "runnersUpdated"
    return resp

@router.post("/api/runners/{runner_id}/delete")
async def runners_delete(runner_id: str):
    from core.runners import delete_runner
    from fastapi.responses import Response
    delete_runner(runner_id)
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "runnersUpdated"
    return resp

@router.post("/api/runners/{runner_id}/set-default")
async def runners_set_default(runner_id: str):
    from core.runners import set_default_runner
    from fastapi.responses import Response
    set_default_runner(runner_id)
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "runnersUpdated"
    return resp

@router.post("/api/runners/{runner_id}/health")
async def runners_health(runner_id: str):
    from core.runners import health_check
    from fastapi.responses import Response
    await health_check(runner_id)
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "runnersUpdated"
    return resp


@router.post("/api/runners/{runner_id}/smoke")
async def runners_smoke(runner_id: str):
    """Prove the runner can do the job, not just that its binary exists.

    Health answers "does the binary resolve". Every runner failure this system
    has hit passed that and still produced nothing but "worker ended without
    reporting a result". This spawns the CLI the way the launcher does and
    requires the model to call one tool through a throwaway MCP server, then
    reports the first stage that failed. Takes ~10s on a working runner.
    """
    from core.runners import get_runner
    from core.runner_smoke import smoke_test
    from fastapi.responses import Response
    runner = get_runner(runner_id)
    if not runner:
        return _runner_error("runner not found")
    res = await smoke_test(runner)
    secs = res["duration_ms"] / 1000
    if res["ok"]:
        msg = f"{runner['name']}: smoke test passed in {secs:.0f}s — {res['detail']}"
    else:
        msg = f"{runner['name']}: failed at {res['stage']} — {res['detail']}"
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = json.dumps({"runnerSmoke": {"ok": res["ok"], "message": msg}})
    return resp

# ── Statistics ──────────────────────────────────────────────────────────

@router.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request):
    return _tmpl().TemplateResponse(request, "stats.html", {"active_page": "stats"})

@router.get("/api/stats/view", response_class=HTMLResponse)
async def stats_view(request: Request):
    from core.stats import overview
    return _tmpl().TemplateResponse(request, "stats_view.html", {"s": overview()})

# ── Live Workers ─────────────────────────────────────────────────────────

@router.get("/api/workers/live", response_class=HTMLResponse)
async def workers_live(request: Request):
    return _tmpl().TemplateResponse(request, "workers_live.html",
                                    {"sessions": _enrich_sessions(sessions.list_active())})

# ── Agents ──────────────────────────────────────────────────────────────

@router.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request):
    return _tmpl().TemplateResponse(request, "agents.html", {"active_page": "agents"})

@router.get("/api/agents/list", response_class=HTMLResponse)
async def agents_list_view(request: Request):
    from core.agents import list_agents
    from core.runners import list_runners
    from core import sessions, usage
    from core.model_classes import class_of
    runners = {r["id"]: r["name"] for r in list_runners()}
    team = usage.roster()
    stats, handoffs = team["agents"], team["handoffs"]
    live = {s["role"] for s in sessions.list_active()}
    agents = list_agents()
    for a in agents:
        n = a["name"]
        a["stats"] = stats.get(n)
        a["live"] = n in live
        a["class"] = class_of(a)
        # First sentence of the persona: what it is for, in its own words.
        sentences = [x.strip().rstrip(".") for x in
                     (a.get("persona") or "").strip().split("\n")[0].split(". ") if x.strip()]
        # "You are the Sisyphus orchestrator." names it; the next sentence says
        # what it does — use that one.
        if len(sentences) > 1 and sentences[0].startswith("You are") and len(sentences[0].split()) < 8:
            sentences = sentences[1:]
        a["role_line"] = (sentences[0] + ".") if sentences else ""
        a["gives"] = sorted(((to, k) for (fr, to), k in handoffs.items() if fr == n), key=lambda x: -x[1])
        a["takes"] = sorted(((fr, k) for (fr, to), k in handoffs.items() if to == n), key=lambda x: -x[1])
    lead = [a for a in agents if a["kind"] == "orchestrator"]
    # In the order work flows through them; anyone else after, busiest first.
    flow = ["explorer", "engineer", "reviewer", "mr-reviewer"]
    workers = sorted((a for a in agents if a["kind"] != "orchestrator"),
                     key=lambda a: (flow.index(a["name"]) if a["name"] in flow else len(flow),
                                    -((a["stats"] or {}).get("runs", 0))))
    # The orchestrator hands every task to someone: how many tasks each got.
    dispatch = [(w["name"], (w["stats"] or {}).get("tasks", 0)) for w in workers if w["stats"]]
    return _tmpl().TemplateResponse(request, "agents_list.html",
                                    {"lead": lead, "workers": workers, "dispatch": dispatch,
                                     "runners": runners, "agents": agents})

@router.get("/api/agents/new-form", response_class=HTMLResponse)
async def agents_new_form(request: Request):
    from web.agent_mcp import TOOLS
    from core.runners import list_runners
    return _tmpl().TemplateResponse(request, "agents_new.html",
                                    {"catalog": list(TOOLS), "runners": list_runners()})

@router.post("/api/agents")
async def agents_add(name: str = Form(...), kind: str = Form("worker"), persona: str = Form(""),
                     model: str = Form(""), runner_id: str = Form(""), tools: List[str] = Form([])):
    from core.agents import add_agent, get_agent
    from fastapi.responses import Response
    if not name.strip() or get_agent(name):
        return Response(status_code=409)
    add_agent(name, persona, tools, runner_id or None, model, kind)
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "agentsUpdated"
    return resp

@router.get("/api/agents/{name}/edit-form", response_class=HTMLResponse)
async def agents_edit_form(request: Request, name: str):
    from core.agents import get_agent
    from web.agent_mcp import TOOLS
    from core.runners import list_runners
    agent = get_agent(name)
    if not agent:
        return HTMLResponse("not found", status_code=404)
    from core.model_classes import CLASSES
    return _tmpl().TemplateResponse(request, "agents_edit.html",
                                    {"agent": agent, "classes": CLASSES,
                                     "catalog": list(TOOLS), "runners": list_runners()})

@router.post("/api/agents/{name}/edit")
async def agents_edit(name: str, persona: str = Form(""), model: str = Form(""),
                      runner_id: str = Form(""), model_class: str = Form(""),
                      tools: List[str] = Form([])):
    from core.agents import update_agent
    from fastapi.responses import Response
    # A blank runner is a choice ("follow the class"), not a missing field: it
    # has to clear a pin, which passing None would silently leave in place.
    update_agent(name, persona=persona, model=model, runner_id=runner_id or "",
                 model_class=model_class, tools=tools)
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "agentsUpdated"
    return resp

@router.post("/api/agents/{name}/delete")
async def agents_delete(name: str):
    from core.agents import delete_agent
    from fastapi.responses import Response
    try:
        delete_agent(name)
    except ValueError:
        return Response(status_code=422)   # built-in — protected
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "agentsUpdated"
    return resp

# ── Tasks (Task+Thread system) ──────────────────────────────────────────

def _enrich_now_card(t, mr, status):
    """Rich-card data for the NOW zone: pipeline stage dots, the last dialogue
    event, cost, and the pending question. One thread read per live card —
    the NOW zone holds a handful of tasks, not the whole history."""
    from core import store, usage

    th = store.get_thread(t["id"])
    # An empty thread means nothing has happened to this task yet — it is
    # queued behind the orchestrator, not working. With a thread it is in
    # flight even between two workers, and it has progress worth drawing.
    t["started"] = bool(th)
    has_commit = any(m["kind"] == "commit" for m in th)
    reviewed = any(m["kind"] == "result" and m["author"] not in ("engineer", "human")
                   and (m["text"].startswith("APPROVED") or m["text"].startswith("CHANGES REQUESTED"))
                   for m in th)
    merged = bool(mr and mr.get("status") == "merged")
    t["stages"] = [
        ("explore", bool(th)),
        ("code", has_commit),
        ("review", reviewed),
        ("merge", merged),
    ]
    t["stage_now"] = next((name for name, done in t["stages"] if not done), "merge")

    for m in reversed(th):
        if store.message_lane(m) == "dialogue" and m["text"]:
            txt = m["text"].split("\n", 1)[0]
            t["last_event"] = (txt[:70] + "…") if len(txt) > 70 else txt
            t["last_author"] = m["author"]
            break

    if status == "waiting":
        qs = [m for m in th if m["kind"] == "question"]
        if qs:
            q = qs[-1]["text"]
            t["question"] = (q[:90] + "…") if len(q) > 90 else q

    cost = usage.task_usage(t["id"])["cost"]
    t["cost"] = f"${cost:.2f}" if cost else None


_PAUSED_BECAUSE = {
    "closed": ("Sisyphus was closed", "resumes when Sisyphus opens"),
    "limits": ("subscription limits", "resumes when a window resets"),
    "stopped": ("you stopped it", ""),
}


def _enrich_paused(t: dict) -> None:
    """Why a paused card is paused, and whether it comes back by itself."""
    kind = t.get("pause_kind") or "stopped"
    why, auto = _PAUSED_BECAUSE.get(kind, ("paused", ""))
    t["pause_why"], t["pause_auto"] = why, auto


def _runner_line(agent_name: str) -> dict:
    """Which runner and model an agent runs on — what a stuck card names so you
    know where to look."""
    from core.agents import get_agent
    from core.runners import get_default_runner, get_runner
    agent = get_agent(agent_name) or {}
    runner = (get_runner(agent["runner_id"]) if agent.get("runner_id") else None) or get_default_runner() or {}
    return {"runner": runner.get("name") or "default", "runner_id": runner.get("id") or "",
            "model": agent.get("model") or runner.get("model") or "the runner's default"}


def _attach_asks(needs_you: list, latest_mr: dict) -> None:
    """Give each "Wants you" task the card it needs (core.asks): a question to
    answer, a stuck task to retry, a budget to lift, or a branch to merge."""
    from core import asks, attachments, store
    for t in needs_you:
        mr = latest_mr.get(t["id"])
        if t.get("status") == "waiting":
            ask = asks.for_waiting(t, store.get_thread(t["id"]))
            if ask["kind"] == "stuck":
                ask.update(_runner_line("orchestrator"))
        elif mr:
            ask = asks.for_merge(mr, t.get("gate") or "", attachments.read(t["id"], "change.patch") or "")
        else:
            ask = {"kind": "question", "prompt": "Waiting for your reply.", "options": []}
        t["ask"] = ask
    order = {"stuck": 0, "question": 1, "budget": 2, "merge": 3}
    needs_you.sort(key=lambda t: (order.get(t["ask"]["kind"], 9), t.get("updated_at") or ""))


def board_column(task, mr):
    """Derived board column from task status + latest merge-request state."""
    status = task.get("status", "")
    if status == "active":
        return "in-progress"
    if status == "waiting":
        return "needs-you"
    if status == "paused":
        return "paused"
    if status == "done":
        if mr is not None:
            if mr.get("status") == "merged":
                return "shipped"
            if mr.get("status") == "open":
                return "to-merge"
            if mr.get("status") == "closed":
                # The work was proposed and then not taken. Calling that "done"
                # put it beside everything that shipped — eight of those on
                # this board, one of them approved before it was closed.
                return "not-merged"
        return "done"
    if status == "failed":
        return "failed"
    return "done"


@router.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request):
    return _tmpl().TemplateResponse(request, "tasks.html", {"active_page": "tasks"})

@router.get("/p/{name}", response_class=HTMLResponse)
async def project_page(request: Request, name: str):
    """One repo's board: the same columns as /tasks, narrowed to this repo, with
    a task box that needs no repo picker because the page already is one."""
    from core.repos import get_repo
    from core.autopilot import describe_schedule, list_autopilots
    repo = get_repo(name)
    if not repo:
        return _tmpl().TemplateResponse(request, "not_found.html", {"path": f"/p/{name}"},
                                        status_code=404)
    autopilots = [a for a in list_autopilots() if a.get("repo") == name]
    for a in autopilots:
        a["explained"] = describe_schedule(a)
    return _tmpl().TemplateResponse(request, "project.html", {
        "active_page": f"project:{name}", "repo": repo, "autopilots": autopilots,
    })

@router.get("/api/tasks/list", response_class=HTMLResponse)
async def tasks_list_view(request: Request, repo: str = "", sprint: str = "", view: str = ""):
    from core import store

    all_mrs = mergerequests.list_mrs()
    task_latest_mr = {}
    for mr in all_mrs:
        tid = mr["task_id"]
        if tid not in task_latest_mr or mr["created_at"] > task_latest_mr[tid]["created_at"]:
            task_latest_mr[tid] = mr

    # Board cards are read at a glance — enrich them with what a glance needs:
    # who is working RIGHT NOW (live session), what a waiting task actually
    # asks, and a snippet of the goal beyond the title line.
    live_by_task = {s["task_id"]: s for s in _enrich_sessions(sessions.list_active())}

    cols = ("in-progress", "paused", "needs-you", "to-merge", "shipped", "done", "not-merged", "failed")
    columns = {c: [] for c in cols}
    kids_by_parent = store.child_counts()
    for status in store.VALID_STATUSES:
        tasks = [t for t in store.list_tasks(status=status) if not t.get("parent_id")
                 and (not repo or t.get("repo") == repo)]
        for t in tasks:
            kids = kids_by_parent.get(t["id"])
            if kids:
                t["child_total"], t["child_done"] = kids
            mr = task_latest_mr.get(t["id"])
            t["review_status"] = mr.get("review_status") if mr and mr.get("review_status") else None
            # While the branch can still land, the card shows what the MERGE
            # GATE says, not just what a reviewer said. The two came apart the
            # day the gate started requiring a recorded check as well: a card
            # reading "reviewed" over a failing check looks ready and is not,
            # and a branch nobody reviewed showed no badge at all — silence that
            # reads as "nothing to report".
            t["gate"] = mergerequests.gate_state(mr) if mr and mr["status"] == "open" else ""

            goal = t.get("goal", "")
            rest = goal.split("\n", 1)[1].strip() if "\n" in goal else ""
            t["snippet"] = (rest[:110] + "…") if len(rest) > 110 else rest

            live = live_by_task.get(t["id"])
            if live and status == "active":
                t["live_role"] = live["role"]
                t["live_mins"] = live["elapsed_mins"]

            col = board_column(t, mr)
            if col in ("in-progress", "paused", "needs-you", "to-merge"):
                _enrich_now_card(t, mr, status)
            if col == "paused":
                _enrich_paused(t)
            columns[col].append(t)

    from core import autopilot as _autopilot
    automations = {f"autopilot:{a['id']}": a for a in _autopilot.list_autopilots()}

    def _origin_of(t):
        """Who asked for this task, as the board shows it: None for a person,
        or the automation that fired it — with when it fires and whether it
        still can, which is what a person wants to know about it."""
        ap = automations.get(t.get("origin") or "")
        if not ap:
            return None
        notes = [_autopilot.describe_schedule(ap)]
        if not ap.get("enabled"):
            notes.append("paused")
        problem = _autopilot.schedule_problem(ap)
        if problem:
            notes.append(problem)
        # The run's own title already names its automation ("Nightly repo
        # scan · 2026-09-06"), so the tag says what the title cannot: when the
        # automation fires — or that it no longer can.
        if problem:
            short = "broken schedule"
        elif not ap.get("enabled"):
            short = "paused"
        else:
            short = notes[0]
        return {"name": ap["name"], "id": ap["id"], "about": " · ".join(notes),
                "short": short, "broken": bool(problem), "paused": not ap.get("enabled")}

    # A status here is not a column a card sits in — it is what the task is
    # doing to you right now, and the three that ask something of you get
    # their own surface above everything else.
    needs_you = columns["needs-you"] + columns["to-merge"]
    _attach_asks(needs_you, task_latest_mr)
    running, queued = [], []
    for t in columns["in-progress"]:
        # A live worker is in flight whatever the thread says; a thread with
        # anything in it means work has begun even between two workers. Only a
        # task with neither is genuinely still queued behind the orchestrator.
        (running if (t.get("live_role") or t.get("started")) else queued).append(t)

    # Everything else is the backlog, and a backlog is read the way a sprint
    # board is read: grouped by week, parents with their subtasks under them,
    # every row carrying the tags you would filter by.
    from core import reviews
    facts = _row_facts()
    rows, by_id, reviewing = [], {}, {}
    for col in ("shipped", "done", "not-merged", "failed", "in-progress", "paused", "needs-you", "to-merge"):
        for t in columns[col]:
            marker = reviews.parse_review_marker(t.get("goal") or "")
            row = _row(t, col, facts, mr=task_latest_mr.get(t["id"]),
                       origin=_origin_of(t), review=bool(marker))
            if marker and marker.get("origin_task"):
                reviewing[row["id"]] = marker["origin_task"]
            rows.append(row)
    for r in rows:
        by_id[r["id"]] = r

    # Subtasks hang under their parent instead of standing beside it: a plan's
    # steps are the plan, not four more items competing with it.
    children = [_row(t, board_column(t, task_latest_mr.get(t["id"])), facts,
                     mr=task_latest_mr.get(t["id"]))
                for t in store.list_tasks() if t.get("parent_id")
                and (not repo or t.get("repo") == repo)]
    for c in children:
        parent = by_id.get(c["parent_id"])
        if parent is not None:
            parent.setdefault("children", []).append(c)
    # So does a review, under the work it reviewed. The link is display-only —
    # parent_id is not touched — and it holds only while the reviewed task is
    # on the board; a review of something filtered away stands on its own.
    for rid, origin_id in reviewing.items():
        parent = by_id.get(origin_id)
        if parent is not None and rid in by_id:
            child = by_id[rid]
            child["parent_id"] = origin_id
            parent.setdefault("children", []).append(child)
    top = [r for r in rows if not r.get("parent_id")]
    top.sort(key=lambda r: r["when"] or "", reverse=True)
    for r in top:
        r["sprint"] = _sprint_of(r["when"])

    # A week is a sprint. Every one of them is on the page, newest open and the
    # rest collapsed — a dropdown that shows one week at a time hides the very
    # comparison ("more failed this week than last") the grouping exists for.
    #
    # Repeats fold INSIDE a week, never across weeks. Folding globally put all
    # 34 of an autopilot's runs on one row in the week of the latest one, and
    # took them out of every earlier week — so "Week 33 · 26 tasks" was missing
    # that week's cron runs entirely, and a ×34 on a row dated today counted
    # runs from six weeks ago.
    sprints = []
    for r in top:
        if not sprints or sprints[-1]["id"] != r["sprint"]["id"]:
            sprints.append({**r["sprint"], "entries": [], "tasks": 0, "done": 0, "failed": 0})
        sprint = sprints[-1]
        # A task folds only into the row right above it — same title, same
        # outcome, back to back, which is what two review runs of one merge
        # request look like. An automation's runs never fold: each is a task
        # it made, and two on the same day share a title by construction (an
        # hourly one would have folded 24 runs into "×24"). They stand as the
        # tasks they are — among yours, marked with who asked for them, one
        # filter away from hidden.
        last = sprint["entries"][-1] if sprint["entries"] else None
        prev = last if (last and not r["origin"] and not last["row"]["origin"]
                        and last["row"]["title"] == r["title"]
                        and last["row"]["kind"] == r["kind"] and not last["row"].get("why")
                        and not last["row"].get("children") and not r.get("children")) else None
        if prev is not None:
            prev["row"]["runs"] += 1
        else:
            sprint["entries"].append({"type": "row", "row": r})
        sprint["tasks"] += 1
        if r["kind"] == "failed":
            sprint["failed"] += 1
        elif r["kind"] in ("shipped", "done", "not-merged"):
            sprint["done"] += 1

    if view == "sprint":
        return _tmpl().TemplateResponse(request, "sprint_board.html", {
            **sprint_board(top, {t["id"] for t in running}), "repo": repo,
            "repos": sorted({r["repo"] for r in rows if r["repo"]}),
            "whos": sorted({a for r in rows for a in r["trail"]}),
            # the same actionable cards the list view shows, keyed by task
            "asks": {t["id"]: t for t in needs_you},
        })

    return _tmpl().TemplateResponse(request, "tasks_list.html", {
        "needs_you": needs_you, "running": running, "queued": queued,
        "paused": columns["paused"],
        "sprints": sprints, "repo": repo,
        "repos": sorted({r["repo"] for r in rows if r["repo"]}),
        "whos": sorted({a for r in rows for a in r["trail"]}),
    })


_KIND_WORD = {"in-progress": "in flight", "paused": "paused", "needs-you": "wants you", "not-merged": "not merged",
              "to-merge": "to merge", "shipped": "shipped",
              "done": "done", "failed": "failed"}


_FAIL_NO_RESULT = re.compile(r"\[fail\]\s*(\S+) ended without a result(?:\s*\((\d+) attempts?\))?", re.I)
_MR_COUNT = re.compile(r"(\d+) open MRs?", re.I)


def _row_facts() -> dict:
    """Everything the backlog row says beyond the task table, read in four
    queries for the whole board — never one per row:

      trail   every agent that worked the task, in the order it first did
      spend   (cost, runs) per task
      fail    the orchestrator's last "[fail]" note per task
      result  the last result message per task

    plus the thresholds above which cost or run count is worth a mention.
    """
    from core.db import get_conn

    conn = get_conn()
    try:
        trail, spend = {}, {}
        for r in conn.execute(
                "SELECT task_id, agent, MIN(id) AS first, SUM(cost) AS cost, COUNT(*) AS n "
                "FROM usage GROUP BY task_id, agent ORDER BY task_id, first").fetchall():
            trail.setdefault(r["task_id"], []).append(r["agent"])
            c, n = spend.get(r["task_id"], (0.0, 0))
            spend[r["task_id"]] = (c + (r["cost"] or 0.0), n + r["n"])
        fail = {r["task_id"]: r["text"] for r in conn.execute(
            "SELECT task_id, text FROM messages WHERE id IN ("
            "SELECT MAX(id) FROM messages WHERE text LIKE '[fail]%' GROUP BY task_id)").fetchall()}
        result = {r["task_id"]: r["text"] for r in conn.execute(
            "SELECT task_id, text FROM messages WHERE id IN ("
            "SELECT MAX(id) FROM messages WHERE kind = 'result' GROUP BY task_id)").fetchall()}
    finally:
        conn.close()

    def p90(values):
        values = sorted(values)
        return values[int(len(values) * 0.9)] if values else 0

    return {"trail": trail, "spend": spend, "fail": fail, "result": result,
            "dear": max(p90([c for c, _ in spend.values()]), 0.10),
            "busy": max(p90([n for _, n in spend.values()]), 5)}


def _result_head(text: str):
    """The headline of a result, for a row that has room for one clause."""
    t = (text or "").strip()
    low = t.lower()
    if low.startswith("changes requested"):
        m = _MR_COUNT.search(t)
        return (f"changes requested · {m.group(1)} MRs" if m else "changes requested"), "bad"
    if "no open merge request" in low:
        return "nothing to review — no open MRs", "good"
    m = re.match(r"reviewed (?:all )?(\d+) open (?:mrs?|merge requests?)", low)
    if m:
        return f"reviewed {m.group(1)} open MRs", "muted"
    if low.startswith("approved"):
        rest = t.split(":", 1)[1].strip() if ":" in t[:20] else ""
        head = re.split(r"(?<=[.;])\s|\(", rest, maxsplit=1)[0].strip(" .;") if rest else ""
        return ((head[:58] + "…") if len(head) > 58 else head) or "approved", "good"
    head = re.split(r"(?<=[.;])\s", t, maxsplit=1)[0].strip(" .;")
    return ((head[:58] + "…") if len(head) > 58 else head), "muted"


def _why(t: dict, kind: str, mr: Optional[dict], facts: dict, auto: bool,
         review: bool) -> Optional[dict]:
    """The one clause this row should say, or None when it has nothing to add.
    Ranked: what is happening now, what blocks a merge, why it failed, why it
    did not land, what a machine run found, and whether it cost unusually much.
    A row that says nothing is saying "this went the ordinary way"."""
    tid = t["id"]
    if t.get("live_role"):
        return {"text": f"{t['live_role']} · {t.get('live_mins', 0)}m", "tone": "live"}
    if mr and mr.get("status") == "open":
        if mr.get("review_status") == "changes_requested":
            return {"text": "changes requested", "tone": "bad"}
        gate = mergerequests.gate_state(mr)
        words = {"check-failed": ("check failed", "bad"), "died": ("the review died", "bad"),
                 "unreviewed": ("nobody has reviewed it", "warn"), "pending": ("in review", "muted"),
                 "ready": ("reviewed and checked — ready", "good")}.get(gate)
        if words:
            return {"text": words[0], "tone": words[1]}
    if kind == "failed":
        note = facts["fail"].get(tid, "")
        m = _FAIL_NO_RESULT.search(note)
        if m:
            n = int(m.group(2) or 1)
            return {"text": f"no result from the {m.group(1)}" + (f" ×{n}" if n > 1 else ""),
                    "tone": "bad", "title": note}
        if note:
            short = note.replace("[fail]", "").strip()
            return {"text": (short[:56] + "…") if len(short) > 56 else short, "tone": "bad", "title": note}
        return {"text": "failed without saying why", "tone": "bad"}
    if kind == "not-merged":
        approved = mr and mr.get("review_status") == "approved"
        return {"text": "approved, closed unmerged" if approved else "closed unmerged", "tone": "muted"}
    if mr and mr.get("status") == "merged" and mr.get("review_status") == "changes_requested":
        return {"text": "changes requested · merged anyway", "tone": "muted",
                "title": "The reviewer asked for changes; the branch was merged anyway — the thread says by whom and why"}
    if review and facts["result"].get(tid):
        # A review has exactly one thing to say, and it is a verdict — not the
        # first clause of whatever prose the reviewer wrapped around it.
        said = facts["result"][tid].strip().upper()
        if said.startswith(("CHANGES REQUESTED", "REQUEST-CHANGES", "REQUEST CHANGES")):
            return {"text": "verdict: changes requested", "tone": "bad", "title": facts["result"][tid][:400]}
        if said.startswith("APPROVE"):
            return {"text": "verdict: approved", "tone": "good", "title": facts["result"][tid][:400]}
        return {"text": "verdict: see the thread", "tone": "muted", "title": facts["result"][tid][:400]}
    if auto and facts["result"].get(tid):
        text, tone = _result_head(facts["result"][tid])
        return {"text": text, "tone": tone, "title": facts["result"][tid][:400]}
    cost, runs = facts["spend"].get(tid, (0.0, 0))
    if cost >= facts["dear"] or runs >= facts["busy"]:
        return {"text": f"${cost:.2f} · {runs} runs", "tone": "warn",
                "title": "Among the dearest tenth of tasks on this board"}
    return None


def _row(t: dict, kind: str, facts: dict, mr: Optional[dict] = None,
         origin: Optional[dict] = None, review: bool = False) -> dict:
    """One task as the backlog draws it. Everything comes from the task, its
    latest MR and the board-wide `facts` — nothing here reads the database."""
    from core import store
    title = store.task_display_title(t)
    if review:
        m = re.match(r"Review (mr-[0-9a-f]+)", title)
        title = f"Review of {m.group(1)}" if m else title
    first_line = (t.get("goal") or "").split("\n", 1)[0].strip()
    trail = list(facts["trail"].get(t["id"], []))
    if t.get("live_role") and t["live_role"] not in trail:
        trail.append(t["live_role"])
    return {
        "id": t["id"], "key": t.get("key") or "", "parent_id": t.get("parent_id"),
        "title": title,
        # the whole thing, for the tooltip: a title cut at 80 characters and
        # then again by the column was unreadable with no way to read it
        "full": first_line if len(first_line) > len(title) else "",
        "kind": kind, "word": _KIND_WORD.get(kind, kind),
        "repo": t.get("repo") or "", "runs": 1, "origin": origin,
        # every role that worked it, in order — the last one alone was almost
        # always the reviewer, which hid who did the work
        "trail": trail, "live": t.get("live_role"),
        "why": _why(t, kind, mr, facts, auto=bool(origin), review=review),
        "when": t.get("updated_at") or t.get("created_at"),
        # Pipeline progress, when this task has any (core routes._enrich_now_card):
        # the list draws it on its in-flight card, the sprint board on the card
        # in its In progress lane — same work, same picture.
        "stages": t.get("stages"), "stage_now": t.get("stage_now"),
    }


SPRINT_LANES = (
    ("queued", "Queued", "waiting for the orchestrator to pick it up"),
    ("running", "In progress", "an agent is on it"),
    ("you", "Needs you", "a question to answer or a merge to decide"),
    ("done", "Done", "finished this week — shipped, done, or closed unmerged"),
    ("failed", "Failed", "ended this week without a result"),
)


def sprint_board(top: list, running_ids: set, today=None) -> dict:
    """The current sprint as a board: this week's work in status lanes.

    A sprint holds everything still open, whenever it started — work carried
    over from an earlier week is still this week's work, and says where it came
    from — and only what CLOSED this week. Done work from earlier weeks is
    history, and lives in the list view.
    """
    from datetime import date
    today = today or date.today()
    now = _sprint_of(today.isoformat())
    lanes = {key: [] for key, _, _ in SPRINT_LANES}
    for r in top:
        still_open = r["kind"] in ("in-progress", "paused", "needs-you", "to-merge")
        this_week = r["sprint"]["id"] == now["id"]
        if not (still_open or this_week):
            continue
        if r["kind"] == "in-progress":
            lane = "running" if (r["id"] in running_ids or r.get("live")) else "queued"
        elif r["kind"] == "paused":
            lane = "queued"
        elif r["kind"] in ("needs-you", "to-merge"):
            lane = "you"
        elif r["kind"] == "failed":
            lane = "failed"
        else:
            lane = "done"
        kids = r.get("children") or []
        lanes[lane].append({**r, "carried": "" if this_week else r["sprint"]["label"],
                            "kids": len(kids),
                            "kids_done": sum(1 for k in kids if k["kind"] in ("shipped", "done"))})
    total = sum(len(v) for v in lanes.values())
    closed = len(lanes["done"]) + len(lanes["failed"])
    return {
        "sprint": now, "lanes": [{"key": k, "label": label, "about": about, "cards": lanes[k]}
                                 for k, label, about in SPRINT_LANES],
        "total": total, "closed": closed, "done": len(lanes["done"]),
        "pct": round(100 * closed / total) if total else 0,
        "days_left": 6 - today.weekday(),
    }


def _sprint_of(when: str) -> dict:
    """The sprint (ISO week) an instant belongs to: its id, its label and the
    dates it spans. A week is the unit a person actually reviews work in."""
    from datetime import date, datetime, timedelta

    try:
        # Stamps are stored in UTC; a sprint is a week in the week the reader
        # lives in. Without the conversion a task started at 00:12 on Monday
        # filed itself under "last week", because in UTC it was still Sunday.
        parsed = datetime.fromisoformat(when)
        d = (parsed.astimezone() if parsed.tzinfo else parsed).date()
    except (TypeError, ValueError):
        d = date.today()
    year, week, weekday = d.isocalendar()
    monday = d - timedelta(days=weekday - 1)
    sunday = monday + timedelta(days=6)
    today_year, today_week, _ = date.today().isocalendar()
    if (year, week) == (today_year, today_week):
        label = "This week"
    elif monday <= date.today() - timedelta(days=7) <= sunday:
        label = "Last week"
    else:
        label = f"Week {week}"
    span = f"{monday.strftime('%-d %b')} – {sunday.strftime('%-d %b')}"
    return {"id": f"{year}-W{week:02d}", "label": label, "span": span}


MAX_ATTACHMENT_BYTES = 25 * 2**20


def _size(n: int) -> str:
    return f"{n / 2**20:.1f} MB" if n >= 2**20 else f"{max(n // 1024, 1)} KB"


def _task_box_error(message: str):
    from fastapi.responses import Response
    resp = Response(status_code=422)
    resp.headers["HX-Trigger"] = json.dumps({"formError": message})
    return resp


@router.post("/api/tasks")
async def tasks_create(request: Request, goal: str = Form(...), repo: str = Form(""),
                       agent: str = Form(""), title: str = Form(""),
                       files: List[UploadFile] = File(default=[])):
    from core import store
    from core.agents import get_agent
    from core.orchestrator import emit
    from fastapi.responses import Response

    if not goal.strip():
        # htmx doesn't swap on non-2xx; this surfaces via the global error
        # toast (base.html's htmx:afterRequest handler).
        return Response(status_code=422)
    if agent.strip():
        a = get_agent(agent)
        if not a or a["kind"] != "worker":
            return Response(status_code=422)
    # Repo optional — repo-less tasks run in a scratch dir (see web/api.py).
    uploads = []
    for f in files or []:
        data = await f.read()
        if not data:
            continue
        if len(data) > MAX_ATTACHMENT_BYTES:
            return _task_box_error(f"{f.filename} is over {MAX_ATTACHMENT_BYTES // 2**20} MB")
        uploads.append((f.filename or "file", data))
    task = store.create_task(goal, repo, agent=agent or None, title=title or None)
    # Attached before the first pass, so the orchestrator's very first read of
    # the thread already has them — with the path, which is what an agent opens.
    from core import attachments
    saved = []
    for name, data in uploads:
        stored = attachments.save_bytes(task["id"], name, data)
        saved.append(stored)
        store.post_message(task["id"], "human", kind="note", attachment=stored,
                           text=f"attached {stored} ({_size(len(data))}) — {attachments.path(task['id'], stored)}")
    emit(task["id"], "created")
    resp = _tmpl().TemplateResponse(request, "taskbox_done.html", {
        "task": store.get_task(task["id"]), "files": saved,
    })
    # quiet: the receipt under the box says it; a toast on top would repeat it.
    resp.headers["HX-Trigger"] = json.dumps({"taskCreated": {"quiet": True}})
    return resp

@router.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_page(request: Request, task_id: str):
    ctx = _task_ctx(task_id)
    if ctx is None:
        return HTMLResponse('<div class="detail-panel"><p class="tone-bad">Task not found</p></div>',
                            status_code=404)
    ctx["active_page"] = "tasks"
    return _tmpl().TemplateResponse(request, "task_detail.html", ctx)

def _review_of(task) -> dict:
    """For a review task, the MR it reviews and the task that opened it.

    A review task doesn't *own* an MR, so ``latest_for_task`` finds nothing and
    the detail page offers no way to reach the thing under review — the one
    object the task is entirely about. The link lives in the goal marker.
    """
    from core import reviews, mergerequests, store
    marker = reviews.parse_review_marker(task.get("goal", ""))
    if not marker:
        return {}
    return {
        "mr": mergerequests.get_mr(marker["mr_id"]),
        "origin": store.get_task(marker["origin_task"]) if marker["origin_task"] else None,
    }


def _classify_diff(text: str):
    """Split a unified diff into (class, line) pairs + add/del counts, for
    colourised rendering. Shared by the diff dialog and the MR view."""
    lines = []
    for ln in (text or "").splitlines():
        if ln.startswith("+") and not ln.startswith("+++"):
            cls = "add"
        elif ln.startswith("-") and not ln.startswith("---"):
            cls = "del"
        elif ln.startswith("@@"):
            cls = "hunk"
        elif ln.startswith(("diff --git", "index ", "+++ ", "--- ", "new file", "deleted file", "old mode", "new mode", "similarity", "rename ")):
            cls = "meta"
        else:
            cls = "ctx"
        lines.append((cls, ln))
    adds = sum(1 for c, _ in lines if c == "add")
    dels = sum(1 for c, _ in lines if c == "del")
    return lines, adds, dels


@router.get("/api/tasks/{task_id}/read/{filename}", response_class=HTMLResponse)
async def task_read_modal(request: Request, task_id: str, filename: str):
    """Open a task's attachment the way its kind reads: a report as a rendered
    document, a patch in the diff viewer, anything else as plain text. Reports
    used to open in the diff viewer, which showed markdown as grey context
    lines."""
    from core import attachments
    if filename.endswith((".patch", ".diff")):
        return await task_diff_modal(request, task_id, filename)
    text = attachments.read(task_id, filename)
    if text is None:
        return HTMLResponse('<div class="modal-backdrop" onclick="this.remove()"><div class="modal" onclick="event.stopPropagation()"><div class="modal__body"><div class="empty">Attachment not found.</div></div></div></div>')
    return _tmpl().TemplateResponse(request, "doc_modal.html", {
        "filename": filename, "text": text, "markdown": filename.endswith(".md")})


@router.get("/api/tasks/{task_id}/diff/{filename}", response_class=HTMLResponse)
async def task_diff_modal(request: Request, task_id: str, filename: str):
    """Render a task attachment (a diff patch / any text) in a dialog, with diff
    syntax colouring — so the change opens in-app, not a separate browser tab."""
    from core import attachments
    text = attachments.read(task_id, filename)
    if text is None:
        return HTMLResponse('<div class="modal-backdrop" onclick="this.remove()"><div class="modal" onclick="event.stopPropagation()"><div class="modal__body"><div class="empty">Attachment not found.</div></div></div></div>')
    lines, adds, dels = _classify_diff(text)
    return _tmpl().TemplateResponse(request, "diff_modal.html", {
        "filename": filename, "lines": lines,
        "adds": adds, "dels": dels,
    })


@router.get("/api/tasks/{task_id}/logs", response_class=HTMLResponse)
async def task_logs_modal(request: Request, task_id: str, name: str = ""):
    """The run logs for a task — what each worker's CLI actually printed.

    These answered every question during the runner debugging that produced the
    smoke test, and reading them required a terminal. *name* selects one log;
    without it the newest is shown.
    """
    from core import runlogs
    logs = runlogs.list_logs(task_id)
    selected = name or (logs[0]["name"] if logs else "")
    text = runlogs.read_log(task_id, selected) if selected else None
    return _tmpl().TemplateResponse(request, "logs_modal.html", {
        "task_id": task_id, "logs": logs,
        "selected": selected, "text": text,
    })


# ── Local Merge Requests ────────────────────────────────────────────────

def _mr_view_ctx(request, mr):
    """Build the MR-view context: branches, mergeability, and the colourised diff."""
    from core import attachments, mergerequests
    branches = mergerequests.list_branches(mr["source_path"])
    merge = mergerequests.check_mergeability(
        mr["bare_path"], mr["source_path"], mr["source_branch"], mr["target_branch"])
    patch = attachments.read(mr["task_id"], "change.patch") or ""
    lines, adds, dels = _classify_diff(patch)
    resolved_files = [p for p in (mr.get("merge_resolved") or "").split("\n") if p.strip()]
    # The template asks the gate rather than re-deriving it. It used to carry its
    # own copy of the rule ("pending or changes_requested"), which is how a NULL
    # review status ended up rendering a plain Merge button for a branch nobody
    # had reviewed.
    return {"request": request, "mr": mr, "branches": branches, "merge": merge,
            "lines": lines, "adds": adds, "dels": dels,
            "resolved_files": resolved_files,
            "blocked": mergerequests.merge_block_reason(mr),
            "verdict": mergerequests.verdict_line(mr)}

@router.get("/api/mr/{mr_id}/view", response_class=HTMLResponse)
async def mr_view(request: Request, mr_id: str):
    from core import mergerequests
    mr = mergerequests.get_mr(mr_id)
    if not mr:
        return HTMLResponse('<div class="modal-backdrop" onclick="this.remove()"><div class="modal"><div class="modal__body"><div class="empty">Merge request not found.</div></div></div></div>')
    return _tmpl().TemplateResponse(request, "mr_modal.html", _mr_view_ctx(request, mr))

@router.post("/api/mr/{mr_id}/target", response_class=HTMLResponse)
async def mr_set_target(request: Request, mr_id: str, target: str = Form(...)):
    from core import mergerequests
    mr = mergerequests.set_target(mr_id, target)
    return _tmpl().TemplateResponse(request, "mr_modal.html", _mr_view_ctx(request, mr))

@router.post("/api/mr/{mr_id}/land", response_class=HTMLResponse)
async def mr_land(request: Request, mr_id: str):
    from core import mergerequests, store
    from fastapi.responses import Response
    mr = mergerequests.get_mr(mr_id)
    if not mr:
        return Response(status_code=404)
    res = mergerequests.land_branch(mr)
    if res.get("ok"):
        store.post_message(mr["task_id"], "system", kind="note",
                           text=f"Landed branch {mr['source_branch']} into {mr['source_path']}")
        mr = mergerequests.get_mr(mr_id)
        ctx = _mr_view_ctx(request, mr)
        ctx["flash"] = f"Landed {mr['source_branch']} — review it in your repo (git checkout {mr['source_branch']})"
    else:
        ctx = _mr_view_ctx(request, mr)
        ctx["error"] = res.get("error", "land failed")
    return _tmpl().TemplateResponse(request, "mr_modal.html", ctx)

async def _recheck_stale_gate(mr: dict, blocked: str):
    """When the only thing blocking a merge is a stale check — the daemon's
    verdict covers an older commit, not the branch's current head — re-run the
    check against the current head and re-read the gate before refusing
    (SIS-115, mr-acbf1f50: the merge sat blocked until someone re-ran the check
    by hand). A fresh failure blocks with itself; a clean pass clears the gate.
    Returns (current row, blocked reason).

    The run is long (~80s on this repo), so it stays off the event loop.
    ``blocked and gate_state == 'check-stale'`` is exactly "the only blocker is
    the stale check": gate_state reaches 'check-stale' only when the review is
    approved and the recorded check did not fail, so the stale verdict is the
    whole reason. ``force`` never reaches here — an explicit override does not
    wait for a re-run.
    """
    from core import verify
    if mergerequests.gate_state(mr) == "check-stale":
        await asyncio.to_thread(verify.run_and_record, mr)
        mr = mergerequests.get_mr(mr["id"])
        blocked = mergerequests.merge_block_reason(mr)
    return mr, blocked


@router.post("/api/mr/{mr_id}/merge", response_class=HTMLResponse)
async def mr_merge(request: Request, mr_id: str, force: str = Form("")):
    from core import mergerequests
    from fastapi.responses import Response
    mr = mergerequests.get_mr(mr_id)
    if not mr:
        return Response(status_code=404)
    if mr["status"] != "open":
        ctx = _mr_view_ctx(request, mr); ctx["error"] = f"MR is already {mr['status']}"
        return _tmpl().TemplateResponse(request, "mr_modal.html", ctx)
    blocked = mergerequests.merge_block_reason(mr)
    if blocked and not force:
        # A stale check is not a refusal — the branch may simply have moved
        # since the daemon's check ran. Re-run against the current head first,
        # then re-evaluate the gate.
        mr, blocked = await _recheck_stale_gate(mr, blocked)
        if blocked:
            ctx = _mr_view_ctx(request, mr)
            ctx["error"] = f"{blocked} — merge blocked; use Merge anyway to override"
            return _tmpl().TemplateResponse(request, "mr_modal.html", ctx)
    res = mergerequests.merge_branch(mr, mr["target_branch"])
    if res.get("ok"):
        # Name the evidence in the same breath as the merge. An override says so
        # out loud: a merge that skipped the gate must not read like one that
        # cleared it. mark_merged + the thread note live in
        # record_merge_success, the one function the manual merge and auto-merge
        # share, so the two can never drift apart.
        cite = mergerequests.verdict_line(mr)
        if blocked:
            cite = f"OVERRIDDEN by the human — {blocked} ({cite})"
        mergerequests.record_merge_success(mr, res, cite=cite)
        mr = mergerequests.get_mr(mr_id)
        ctx = _mr_view_ctx(request, mr)
        ctx["flash"] = f"Merged into {res['target']} as {res['sha'][:8]} — reversible with git reset --hard @{{1}}"
    else:
        ctx = _mr_view_ctx(request, mr); ctx["error"] = res.get("error", "merge failed")
    return _tmpl().TemplateResponse(request, "mr_modal.html", ctx)

@router.post("/api/mr/{mr_id}/merge-resolve", response_class=HTMLResponse)
async def mr_merge_resolve(request: Request, mr_id: str, force: str = Form("")):
    """Merge an MR that conflicts, letting the local model resolve each conflicted
    file (core.mergerequests.ai_conflict_resolver) instead of sending the user off to
    fix it by hand.

    Guarded to the case it exists for: only an MR that does NOT merge cleanly.
    A clean merge has nothing to resolve, and putting model-written content into
    a merge git could do exactly is strictly worse. The mergeability check is
    repeated here rather than trusted from the button, because the modal's button
    state was computed when it rendered and the target branch can move under it.

    Failure is never dressed up as success: merge_resolving aborts the merge and
    leaves the target branch untouched the moment one file comes back unresolved,
    and this route then leaves the MR open and renders the reason.
    """
    from core import mergerequests, store
    from fastapi.responses import Response
    mr = mergerequests.get_mr(mr_id)
    if not mr:
        return Response(status_code=404)
    if mr["status"] != "open":
        ctx = _mr_view_ctx(request, mr); ctx["error"] = f"MR is already {mr['status']}"
        return _tmpl().TemplateResponse(request, "mr_modal.html", ctx)
    blocked = mergerequests.merge_block_reason(mr)
    if blocked and not force:
        # Same as the plain merge route: a stale check re-runs rather than
        # refuses.
        mr, blocked = await _recheck_stale_gate(mr, blocked)
        if blocked:
            ctx = _mr_view_ctx(request, mr)
            ctx["error"] = f"{blocked} — merge blocked; use Merge anyway to override"
            return _tmpl().TemplateResponse(request, "mr_modal.html", ctx)

    ctx = _mr_view_ctx(request, mr)
    merge = ctx["merge"]
    if not merge.get("ok"):
        ctx["error"] = f"cannot check mergeability ({merge.get('error')}) — nothing was merged"
        return _tmpl().TemplateResponse(request, "mr_modal.html", ctx)
    if merge.get("mergeable"):
        ctx["error"] = ("this branch merges cleanly — use Merge; AI resolution is only "
                        "for a merge git cannot do on its own")
        return _tmpl().TemplateResponse(request, "mr_modal.html", ctx)

    res = mergerequests.merge_resolving(mr, mr["target_branch"], mergerequests.ai_conflict_resolver)
    if not res.get("ok"):
        ctx["error"] = (f"AI resolution failed: {res.get('error', 'unknown error')} — "
                        f"{mr['target_branch']} is untouched and nothing was merged")
        return _tmpl().TemplateResponse(request, "mr_modal.html", ctx)

    resolved = res.get("resolved") or []
    mergerequests.mark_merged(mr_id, res["sha"], resolved)
    cite = mergerequests.verdict_line(mr)
    if blocked:
        cite = f"OVERRIDDEN by the human — {blocked} ({cite})"
    note = (f"Merged {mr['source_branch']} into {res['target']} ({res['sha'][:8]}) in "
            f"{mr['source_path']} — conflicts AI-resolved in: {', '.join(resolved)}; {cite}"
            if resolved else
            f"Merged {mr['source_branch']} into {res['target']} ({res['sha'][:8]}) in "
            f"{mr['source_path']} — no conflicts remained, nothing was AI-resolved; {cite}")
    store.post_message(mr["task_id"], "system", kind="note", text=note)
    ctx = _mr_view_ctx(request, mergerequests.get_mr(mr_id))
    ctx["flash"] = (
        f"AI-resolved {len(resolved)} conflicting file(s) — {', '.join(resolved)} — and merged "
        f"into {res['target']} as {res['sha'][:8]}. Those hunks are model-written: read them "
        f"before you push (revert with git reset --hard @{{1}})"
        if resolved else
        f"Merged into {res['target']} as {res['sha'][:8]} — the conflict was already gone, "
        f"nothing was AI-resolved")
    return _tmpl().TemplateResponse(request, "mr_modal.html", ctx)

@router.post("/api/mr/{mr_id}/close", response_class=HTMLResponse)
async def mr_close(request: Request, mr_id: str):
    from core import mergerequests
    from fastapi.responses import Response
    mr = mergerequests.get_mr(mr_id)
    if not mr:
        return Response(status_code=404)
    mergerequests.set_status(mr_id, "closed")
    return _tmpl().TemplateResponse(request, "mr_modal.html", _mr_view_ctx(request, mergerequests.get_mr(mr_id)))

def _task_ctx(task_id: str) -> Optional[dict]:
    """Everything a task's content template needs — built once and rendered
    three ways: the full page, the panel over the board, and the poll that
    refreshes either of them. Returns None when there is no such task."""
    from datetime import datetime, timezone
    from core import store, usage

    task = store.get_task(task_id)
    if not task:
        return None
    messages = store.get_thread(task_id)

    # Live "what is happening right now" for the header: a worker running or
    # queued, with elapsed minutes — the thread is silent while an agent works,
    # which reads as "stuck" without this.
    working = None
    if task["status"] == "active" and messages:
        last = messages[-1]
        if last["kind"] in ("worker_started", "worker_queued"):
            try:
                started = datetime.fromisoformat(last["created_at"])
                mins = int((datetime.now(timezone.utc) - started).total_seconds() // 60)
            except Exception:  # noqa: BLE001
                mins = 0
            role = last["text"].split(" ", 2)[1].rstrip(":") if last["kind"] == "worker_started" \
                else last["text"].split(" ", 1)[0]
            label = "working" if last["kind"] == "worker_started" else "queued for a slot"
            working = {"role": role, "label": label, "mins": mins}

    # Nothing running? Show what JUST ended for a few minutes — otherwise a
    # worker finishing makes the indicator vanish mid-glance and a refresh
    # reads as "did anything happen at all?".
    finished = None
    if working is None:
        for m in reversed(messages):
            if m["kind"] == "worker_finished":
                try:
                    ended = datetime.fromisoformat(m["created_at"])
                    mins = int((datetime.now(timezone.utc) - ended).total_seconds() // 60)
                except Exception:  # noqa: BLE001
                    break
                if mins <= 5:
                    finished = {"text": m["text"], "mins": mins}
                break

    from core import attachments, timeline

    mr = mergerequests.latest_for_task(task_id)
    live = _enrich_sessions(sessions.list_for_task(task_id))
    steps = timeline.steps(messages)
    live_by_role = {s["role"]: s for s in live}
    for st in steps:
        if st["type"] == "run" and st["how"] == "running":
            st["live"] = live_by_role.get(st["role"])

    change = None
    if mr:
        patch = attachments.read(task_id, "change.patch") or ""
        _, adds, dels = _classify_diff(patch)
        checks = [s_ for s_ in steps if s_["type"] == "check"]
        verdicts = timeline.verdicts(messages)
        change = {"adds": adds, "dels": dels,
                  "files": sum(1 for ln in patch.splitlines() if ln.startswith("diff --git")),
                  "check": checks[-1] if checks else None,
                  "approvals": sum(1 for v in verdicts if v["approved"]),
                  "gate": mergerequests.gate_state(mr) if mr["status"] == "open" else ""}

    return {
        "task": task, "messages": messages, "working": working, "finished": finished,
        "live_sessions": live,
        "result": store.result_view(task_id),
        "children": store.list_children(task_id),
        "parent": store.get_task(task["parent_id"]) if task.get("parent_id") else None,
        "mr": mr,
        "review_of": _review_of(task),
        "usage": usage.task_usage(task_id),
        # the four reads the page is built from — see core/timeline.py
        "outcome": timeline.outcome(task, messages),
        "steps": steps,
        "history": timeline.history(task, messages, mr, store.list_events(task_id)),
        "files": timeline.files(task_id, messages),
        "change": change,
        # the patch a person would open: the one the worker posted with its
        # change, or change.patch on disk — whether or not an MR row exists
        "diff_file": next((m["attachment"] for m in reversed(messages)
                           if m["kind"] in ("mr", "result") and (m.get("attachment") or "").endswith(".patch")),
                          "change.patch" if attachments.read(task_id, "change.patch") is not None else ""),
        "asked_by": _asked_by(task),
        "trail": _trail(task_id),
    }


def _asked_by(task: dict) -> dict:
    """Who asked for the task: you, or the automation that fired it."""
    origin = task.get("origin") or ""
    if origin.startswith("autopilot:"):
        from core import autopilot
        ap = autopilot.get_autopilot(origin.split(":", 1)[1])
        if ap:
            return {"who": ap["name"], "auto": True, "about": autopilot.describe_schedule(ap)}
    return {"who": "you", "auto": False, "about": ""}


def _trail(task_id: str) -> list:
    """Every role that worked the task, in the order it first did."""
    from core.db import get_conn
    conn = get_conn()
    try:
        rows = conn.execute("SELECT agent, MIN(id) AS first FROM usage WHERE task_id = ? "
                            "GROUP BY agent ORDER BY first", (task_id,)).fetchall()
    finally:
        conn.close()
    return [r["agent"] for r in rows]


@router.get("/api/tasks/{task_id}/panel", response_class=HTMLResponse)
async def task_panel(request: Request, task_id: str):
    """A task, ready to drop into the overlay over the board — the same shell
    the full page uses, so it polls itself the same way. The board opens this
    instead of navigating: reading a task should not cost you your place in a
    list you scrolled to, and /tasks/{id} still answers for a shared link."""
    ctx = _task_ctx(task_id)
    if ctx is None:
        return HTMLResponse('<div class="empty">Task not found</div>', status_code=404)
    return _tmpl().TemplateResponse(request, "_task_shell.html", ctx)


@router.get("/api/tasks/{task_id}/detail", response_class=HTMLResponse)
async def task_detail_view(request: Request, task_id: str):
    ctx = _task_ctx(task_id)
    if ctx is None:
        return HTMLResponse('<div class="detail-panel"><p class="tone-bad">Task not found</p></div>')
    return _tmpl().TemplateResponse(request, "task_detail_content.html", ctx)

@router.post("/api/tasks/{task_id}/messages")
async def task_reply(task_id: str, text: str = Form(...), from_card: str = Form("")):
    from core import store
    from core.orchestrator import emit
    from fastapi.responses import Response

    text = text.strip()
    if not text:
        return Response(status_code=422)
    task = store.get_task(task_id)
    if not task:
        return Response(status_code=404)

    if task["status"] in ("done", "failed"):
        # The orchestrator ignores every event on a finished task, so a reply
        # typed into a done task used to post and then vanish: nothing ran,
        # nothing said so. Replying IS reopening — do both, in that order.
        store.set_status(task_id, "waiting", cause="reopened by your reply")
        store.post_message(task_id, "system", kind="note", text="reopened by the human with a reply")
    if task["status"] == "paused":
        # A reply to a paused task is also a Resume; the reply's own event
        # drives the pass, so no second `continue` is queued beside it.
        store.set_status(task_id, "active", cause="resumed by your reply")
    store.post_message(task_id, "human", kind="answer", text=text)
    emit(task_id, "human_replied")
    if from_card:
        # Answered from a "Wants you" card: the card becomes a receipt for what
        # was said, and the board catches up on its next pulse — reloading it
        # here would wipe the receipt before it could be read.
        from html import escape
        said = (text[:70] + "…") if len(text) > 70 else text
        return HTMLResponse(
            f'<div class="askc-done"><span class="askc-done__tick">✓</span>'
            f'<span class="tkey">{escape(task.get("key") or task_id)}</span>'
            f'<span>answered “{escape(said)}” — back to work</span></div>')
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "taskUpdated"
    return resp

@router.post("/api/tasks/{task_id}/stop")
async def task_stop(task_id: str):
    """Stop a running task: signal its worker to die, then pause it.

    Paused rather than `failed` — the work did not fail, a human interrupted
    it — and rather than `waiting`, which would put it among the questions
    asked of you when nothing was asked. Resume (or a reply) continues it.

    The cancelled worker still posts its own worker_finished on the way out
    (spawn_worker's "ended without reporting" path), so the thread marker closes
    itself and the task stays runnable — unlike killing the pid by hand, which
    leaves it stuck until a daemon restart.
    """
    from core import cancellation, store
    from fastapi.responses import Response

    task = store.get_task(task_id)
    if not task:
        return Response(status_code=404)
    if task["status"] in ("done", "failed"):
        return Response(status_code=409)

    # Stop the whole fan-out, not just the parent. Children run their own
    # workers; parking only the root leaves them burning the API key and rolling
    # their results up into a task the human already stopped. Park them too, or
    # their cancelled worker's event drives a pass and the orchestrator simply
    # spawns the next one.
    tree = store.task_tree(task_id)
    signalled = 0
    for tid in tree:
        signalled += cancellation.request(tid)
        t = store.get_task(tid)
        if t and t["status"] not in ("done", "failed"):
            store.set_status(tid, "paused", cause="stopped by you", pause_kind="stopped")
            if tid != task_id:
                store.post_message(
                    tid, "system", kind="note",
                    text=f"stopped because its parent {task_id} was stopped by the human",
                )
    store.post_message(
        task_id, "system", kind="note",
        text=("stopped by the human — "
              + (f"{signalled} worker(s) signalled to stop. " if signalled
                 else "no worker was running. ")
              + (f"{len(tree) - 1} child task(s) stopped too. " if len(tree) > 1 else "")
              + "Press Resume (or reply) to continue."),
    )
    logger.info("task %s stopped by the human (%d worker(s) across %d task(s))",
                task_id, signalled, len(tree))
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "taskUpdated"
    return resp


@router.post("/api/tasks/{task_id}/resume")
async def task_resume(task_id: str):
    """Put a paused task back to work — the Resume button. A task that is not
    paused (already resumed, or never paused) answers 409, so a double click
    cannot start a second worker."""
    from core import orchestrator, store
    from fastapi.responses import Response

    task = store.get_task(task_id)
    if not task:
        return Response(status_code=404)
    tree = [tid for tid in store.task_tree(task_id)
            if (store.get_task(tid) or {}).get("status") == "paused"]
    if task_id not in tree:
        return Response(status_code=409)
    for tid in tree:
        orchestrator.resume(tid, "resumed by you")
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "taskUpdated"
    return resp


@router.post("/api/tasks/{task_id}/reopen")
async def task_reopen(task_id: str):
    """Reopen a terminal task as *waiting*: the follow-up work continues on the
    same thread (and branch) once the human replies with what to do next —
    waiting is the right state because only a human reply should resume it."""
    from core import store
    from fastapi.responses import Response

    task = store.get_task(task_id)
    if not task:
        return Response(status_code=404)
    if task["status"] not in ("done", "failed"):
        return Response(status_code=409)

    store.set_status(task_id, "waiting", cause="reopened by you")
    store.post_message(task_id, "system", kind="note",
                       text="reopened by the human — reply below to tell the orchestrator what to do next")
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "taskUpdated"
    return resp

@router.post("/api/tasks/{task_id}/retry")
async def task_retry(task_id: str):
    """Retry a terminal task *from scratch*: a brand-new task with the same
    goal, repo and agent, but a fresh id — and therefore a fresh branch, thread
    and worktree. The old attempt's work is deliberately ignored; this is the
    complement of /reopen, which continues the same branch. base_branch is not
    copied, so the new task starts from the repo default."""
    from core import store
    from core.orchestrator import emit
    from fastapi.responses import Response

    task = store.get_task(task_id)
    if not task:
        return Response(status_code=404)
    if task["status"] not in ("done", "failed"):
        return Response(status_code=409)

    new_task = store.create_task(task["goal"], task["repo"] or "",
                                 agent=task["agent"] or None)
    store.post_message(task_id, "system", kind="note",
                       text=f"retried from scratch as {new_task['id']}")
    store.post_message(new_task["id"], "system", kind="note",
                       text=f"retry of {task_id} — previous attempt's thread and branch deliberately ignored")
    emit(new_task["id"], "created")
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = f"/tasks/{new_task['id']}"
    return resp

@router.post("/api/tasks/{task_id}/delete")
async def task_delete(task_id: str):
    from core import cancellation, sessions, store
    from core.worker import cleanup_task
    from fastapi.responses import Response

    if not store.get_task(task_id):
        return Response(status_code=404)
    tree = store.task_tree(task_id)   # root + fan-out descendants
    for tid in tree:
        # Revoking the token only makes the worker's tool calls fail; the
        # subprocess keeps running against a task that no longer exists, and
        # keeps spending. Signal it to stop as well.
        cancellation.request(tid)
        sessions.revoke_task(tid)
    store.delete_task(task_id)        # cascades the whole subtree in the DB
    for tid in tree:
        try:
            cleanup_task(tid, deep=True)
        except Exception as e:
            logger.warning("cleanup failed for task %s during delete: %s", tid, e)
            pass
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = "/tasks"   # navigate back to the board
    return resp
