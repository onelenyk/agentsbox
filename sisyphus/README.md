# Sisyphus

**A self-hosted orchestrator for autonomous AI coding agents.**

Give it a goal; it plans, dispatches specialist agents (explorer → engineer →
reviewer), runs them in isolated git worktrees through a local CLI runner
(opencode or Claude Code), and drives the work to done — with a review loop, a
machine-verified merge-request flow, budget guards, and a live "Mission Control"
web UI. It can also run **proactively**: scheduled or webhook-triggered
*autopilots* create tasks on their own.

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
  chosen branch, land, close — reversible, never force. Merging is gated on the
  review verdict (with an explicit override).
- **Machine-verified checks** — if a repo configures a `check:` command, the
  daemon runs it in a throwaway worktree against every MR and records the exit
  code, so an approval cites a number no agent authored.
- **Forge publishing & polling** — a repo cloned from GitHub/GitLab gets real
  PRs/MRs (pushed and opened via the API, per-repo or global token), and a
  poller follows what happens to them — no inbound webhook needed.
- **Pluggable runners** — opencode and Claude Code CLI behind one profile layer
  (argv, env, MCP wiring per CLI); auto-detect finds installed CLIs, and a smoke
  test proves a runner can actually call a tool before it is trusted with work.
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

**Requirements:** Python 3.9+, git, and at least one runner CLI on your PATH —
[opencode](https://opencode.ai) or [Claude Code](https://claude.com/claude-code)
(**Runners → Detect** finds installed ones). An API key is optional: opencode can
use its own logged-in providers (the model prefix picks the provider, e.g.
`deepseek/…`), and Claude Code uses its OAuth login. Set `OPENROUTER_API_KEY`
only if you route models through OpenRouter.

```bash
# 1. install deps
pip install -r requirements.txt

# 2. configure secrets (loaded at startup; .env is gitignored; all optional)
cp .env.example .env
$EDITOR .env          # API/forge tokens if you use them

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
- **Ship changes** — when an engineer opens a merge request, review the diff
  (with the repo's own `check:` verdict already attached) and **merge into a
  chosen branch**, **land** the branch, or **close** it — all local. Repos cloned
  from GitHub/GitLab publish real PRs/MRs instead.
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
  - `forge_poll` — `enabled` + `interval_seconds`: follow published PRs/MRs by
    polling (the loopback daemon can never receive a webhook)
  - `default_timeout`, `max_review_loops`, `log_level`, `api.token`, `api.allow_localhost`
- **per-repo keys** (under `repos:`):
  - `check:` — shell command the daemon runs against every MR in a throwaway
    worktree; its exit code becomes the MR's recorded verdict
  - `setup:` — copy files / run commands into each fresh worktree (things git
    does not carry, e.g. `local.properties`)
  - `platform:` (`local`/`github`/`gitlab`, else inferred from the source URL)
    and `token:` — a per-repo forge credential that beats the global one

Data lives under `workspace/` (SQLite db, per-task bare clones, backups, logs) — gitignored.

---

## Project layout

```
core/          domain + engine
  store.py         tasks + threads (SQLite)
  orchestrator.py  the cold decision loop + guardrails
  worker.py        spawn agents in isolated worktrees
  launcher.py      run the runner subprocess (timeouts, usage, orphan records)
  runner_profiles.py  per-CLI shape: argv, env, MCP config (opencode, claude-cli)
  runner_smoke.py  prove a runner can call a tool before trusting it with work
  agents.py        agent registry (persona/tools/runner/kind)
  runners.py       CLI-backend configs + runner_detect.py (binary resolution)
  mergerequests.py local merge-request entity + git actions
  verify.py        run the repo's check: command against an MR, record verdict
  providers.py     publish a branch: local MR, or a real GitHub/GitLab PR
  forge_poll.py    follow published PRs/MRs by polling the forge
  repo_check.py    connection check: reachable → authenticated → can push
  sandbox.py       macOS Seatbelt profile for workers (repo + creds write-deny)
  orphans.py       reap worker subprocesses that outlive the daemon
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
pytest -q                 # ~1050 unit tests
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
