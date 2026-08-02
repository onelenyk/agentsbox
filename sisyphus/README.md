# Sisyphus

**A self-hosted orchestrator for autonomous AI coding agents.**

Give it a goal; it plans, dispatches specialist agents (explorer → engineer →
reviewer), runs them in isolated git worktrees through a local CLI runner
(opencode), and drives the work to done — with a review loop, a local
merge-request flow, budget guards, and a live "Mission Control" web UI. It can
also run **proactively**: scheduled or webhook-triggered *autopilots* create
tasks on their own.

It runs directly on the host — no container. No hosted backend, and no raw model
HTTP in the core: every AI decision, including the orchestrator's, runs through
your local runner.

---

## Highlights

- **Autonomous SDLC loop** — orchestrator coordinates explorer/engineer/reviewer;
  a task is done only after a reviewer approves.
- **One decision per pass** — the orchestrator is a *cold function*: `(thread +
  event) → one action`. State lives entirely in the append-only thread, so a
  daemon restart just resumes.
- **Multi-repo fan-out** — one parent task splits into child tasks per repo and
  rolls their results back up.
- **Repo-less tasks** — research / planning / Q&A run read-only in a scratch dir.
- **Local Merge Requests** — mergeability dry-run (🟢/🔴 conflicts), merge into a
  chosen branch, land, close — reversible, never force.
- **Autopilot** — named automations fire on a **schedule** (cron/interval) or a
  **webhook** (jira/github/gitlab) and create tasks; `{{payload}}` templating.
- **Guardrails** — restart recovery, total/idle timeouts, a global worker-concurrency
  cap, per-task & daily **budget caps**, circuit breakers, and a startup preflight.
- **Observability** — Mission Control dashboard, per-task token/cost, statistics,
  rotating logs, a live health heartbeat, and browser notifications.
- **Backups** — one-click SQLite snapshots (`VACUUM INTO`) with a reversible restore.
- **Four surfaces** — Web UI, JSON API (`/api/v1`), MCP server (agent tools), and a
  `sisyphus` CLI.

---

## Architecture at a glance

```
                        ┌──────────────── daemon (uvicorn) ───────────────┐
  Web UI (htmx) ──┐     │                                                 │
  JSON API      ──┼──▶  │  event queue → dispatch_forever (one pass/ev)   │
  MCP tools     ──┤     │        │                                        │
  Autopilot tick──┘     │        ▼                                        │
                        │   orchestrator (an Agent) ── decide ONE action  │
                        │        │  spawn_worker / spawn_task / ask_human  │
                        │        ▼            / complete / fail / note     │
                        │   worker  ── opencode subprocess in a git        │
                        │   (agent)    worktree, MCP tools, usage recorded │
                        └─────────────────────────────────────────────────┘
        SQLite (workspace/sisyphus.db): tasks · messages · runners · agents
                       · usage · merge_requests · autopilots
```

- **Task** `{id, goal, repo, status, parent_id, agent}` + a **Thread** of
  append-only messages — the only two domain entities.
- **Agents** are DB-backed `{name, persona, tools[], runner, kind}` (worker or
  orchestrator). **Runtimes** are CLI-backend configs (opencode, model, key).
- The orchestrator's *decisions are themselves MCP tool calls* it makes as an
  agent — there is no direct model HTTP anywhere.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the domain model in depth.

---

## Quick start

**Requirements:** Python 3.9+, git, and the [opencode](https://opencode.ai) CLI on
your PATH (or any configured runner). An `OPENROUTER_API_KEY` (or your runner's
key) for the model.

```bash
# 1. install deps
pip install -r requirements.txt

# 2. configure secrets (loaded at startup; .env is gitignored)
cp .env.example .env
$EDITOR .env          # set OPENROUTER_API_KEY

# 3. run the daemon
python sisyphus.py daemon start      # or: python -m uvicorn web.app:app --port 7677

# 4. open Mission Control
open http://localhost:7677
```

`WORKSPACE_ROOT` (SQLite db, per-task clones, backups, logs) defaults to
`./workspace` next to the repo. Then **Repos → Add** your git repo, and
**Tasks → + New Task** — pick a template or describe it in plain words and hit
**✨ AI structure**.

### Always-on (macOS / launchd)

```bash
python sisyphus.py daemon install    # start at login, restart on crash
python sisyphus.py daemon status     # live? supervised? version
python sisyphus.py daemon logs -f
```

Secrets come from `.env` (launchd has no shell env). See [`deploy/README.md`](deploy/README.md).

---

## Using it

- **Create a task** — a goal + optional repo + optional pinned agent. No repo =
  read-only research. Pinned agent = solo (orchestrator skipped).
- **Watch it run** — the task thread shows every orchestrator decision and worker
  result; the dashboard shows agents at work and cost.
- **Ship changes** — when an engineer opens a merge request, review the diff and
  **merge into a chosen branch**, **land** the branch, or **close** it — all local.
- **Automate** — **Autopilot → + New** for a `0 3 * * *` "scan repos for stuck MRs"
  or a jira `issue_created` → "Analyze {{ issue.key }}".
- **Stay safe** — **Backups → Create backup** before experiments; restore always
  keeps a safety copy.

---

## Surfaces

| Surface | Where | For |
|---|---|---|
| **Web UI** | `http://localhost:7677` | day-to-day operation (Mission Control) |
| **JSON API** | `/api/v1/*` | scripting, integrations (bearer-auth, loopback-exempt) |
| **MCP server** | `python mcp_server.py` | letting an external agent (e.g. an AI chat) drive Sisyphus |
| **CLI** | `python sisyphus.py …` | daemon control, runners, dev hooks |

The MCP server is a **thin client** of the daemon — it holds no state and forwards
every tool call over HTTP (`SISYPHUS_DAEMON_URL`, default `http://127.0.0.1:7677`).
Tools: `sisyphus_create_task`, `sisyphus_list_tasks`, `sisyphus_get_task`,
`sisyphus_reply_task`, plus repos/runners CRUD and `sisyphus_server_{start,status,stop}`.

### CLI

```
sisyphus daemon   start | stop | restart | status | logs [-f] | install | uninstall
sisyphus runners  list | add | edit | delete | health | detect
sisyphus dev      install-hooks        # pre-push hook that runs the test suite
```

---

## Configuration

- **`.env`** — secrets & env (`OPENROUTER_API_KEY`, `SISYPHUS_API_TOKEN`, forge
  tokens). Shell env wins over `.env`; `${VAR}` is expanded in `config.yaml`.
- **`config.yaml`** — knobs:
  - `max_concurrent_workers` (default 2) — global worker cap
  - `task_budget_usd` (default 5.0) / `daily_budget_usd` (0 = off) — budget guards
  - `autopilot_tick_seconds` (30) — schedule check cadence
  - `default_timeout`, `max_review_loops`, `log_level`, `api.token`, `api.allow_localhost`

Data lives under `workspace/` (SQLite db, per-task bare clones, backups, logs) — gitignored.

---

## Project layout

```
core/          domain + engine
  store.py         tasks + threads (SQLite)
  orchestrator.py  the cold decision loop + guardrails
  worker.py        spawn agents in isolated worktrees
  launcher.py      run the opencode subprocess (timeouts, usage)
  agents.py        agent registry (persona/tools/runner/kind)
  runners.py       CLI-backend configs + runner_detect.py (binary resolution)
  mergerequests.py local merge-request entity + git actions
  autopilot.py     scheduled/webhook automations + tick loop
  ai_assist.py     one-shot "structure this task" helper
  repo_context.py  what the AI reads in a repo (AGENTS.md, CLAUDE.md, …)
  usage.py stats.py backup.py preflight.py env.py logging_setup.py webhooks.py …
web/           FastAPI app
  routes.py        htmx UI routes        api.py    JSON API (/api/v1)
  agent_mcp.py     agent decision tools  auth.py   API token guard
  templates/  static/   the Mission Control UI
mcp_server.py  MCP stdio server (thin client of the daemon)
sisyphus.py    CLI entrypoint
deploy/        launchd LaunchAgent template
docs/          architecture + feature notes
```

---

## Testing

```bash
pytest -q                 # ~407 unit tests
pytest tests/e2e -q       # Playwright browser e2e — needs:
                          #   pip install -r requirements-dev.txt
                          #   python -m playwright install chromium
```

Browser e2e auto-skips if Playwright isn't installed.

---

## Design principles

- **Two entities, no state machines** — Task + Thread; continuity from re-reading
  the thread.
- **The orchestrator never self-certifies quality** — "is this good?" is delegated
  to a reviewer agent, whose verdict is the next event.
- **No raw model HTTP in the core** — all AI (workers *and* the orchestrator) runs
  through the local runner.
- **Never a silent stall** — every failure path parks the task visibly (waiting)
  so a human can resume it.
