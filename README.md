# ⚡ Sisyphus

**A self-hosted orchestrator for autonomous AI coding agents.**

Give it a goal in plain words. It plans, dispatches specialist agents into
isolated git worktrees, has a *different* agent review the result, and opens a
merge request that only lands when a check the daemon ran itself agrees.

Runs on your own machine — no container, no hosted backend, no model API key in
the core.

[![CI](https://github.com/onelenyk/agentsbox/actions/workflows/ci.yml/badge.svg)](https://github.com/onelenyk/agentsbox/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.14-blue)
![Tests](https://img.shields.io/badge/tests-1%2C852%20%2B%20browser%20e2e-brightgreen)
![Runs on](https://img.shields.io/badge/runs-on%20your%20machine-lightgrey)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)

```
goal ──▶ orchestrator ──▶ explorer ──▶ engineer ──▶ (qa) ──▶ reviewer ──▶ merge request
           (one decision per event, state lives in an append-only thread)
```

---

> **Status, 2026-10-11: feature-frozen.** The system works and builds a good
> part of itself (numbers [below](#where-it-stands)), but on its author's real
> work — a sprint of about fifty bugs in a mobile codebase — one agent in a
> terminal, kept going in a loop, got further than the full pipeline did. No new
> features are planned. What proved useful on its own is listed under
> [Where it stands](#where-it-stands).

## Why it exists

**The problem.** Working with one coding agent feels faster than it is. You
write a prompt, it works for twenty minutes, you review, find five issues,
explain them, it tries again — and the loop costs an hour of *your* attention
per task. The agent never gets a second pair of eyes, and you are the second
pair of eyes.

**The idea.** Mirror a human team rather than a single assistant. An engineer
writes the code; a *separate* reviewer who did not write it decides whether it
is good; a QA agent drives the build on a real device; the orchestrator only
decides **who works next**, never whether the work is good. The chain of
verification is the product.

**The constraint.** No raw model HTTP in the core. Every AI decision — including
the orchestrator's own — is a tool call made from a local CLI runner you are
already logged into ([opencode](https://opencode.ai) or
[Claude Code](https://claude.com/claude-code)). No SDK, no API key baked into
the code, no vendor lock-in: changing the model is a config change.

## What it does

- **Plans and delegates.** One goal becomes a task; the orchestrator reads the
  task thread and emits exactly one action per event — spawn a worker, fan out
  child tasks per repo, ask a human, or finish.
- **Isolates every worker.** Each task gets a fresh bare clone; each agent run
  gets its own git worktree off it, so work never branches from a stale snapshot
  and can never touch the real source repo.
- **Reviews with a different agent.** A task is done only when a reviewer
  approves it. The orchestrator never certifies its own quality.
- **Gates the merge on evidence.** The repo's own `check:` command is run by the
  daemon in a throwaway worktree; the exit code is recorded against that exact
  commit. A failing check — or a passing one recorded against a commit the
  branch has moved past — refuses the merge. Overriding is one click, and the
  thread records that it was one.
- **Tests on real hardware.** Android emulators, iOS simulators and connected
  phones are leased to a `qa` agent that builds, installs and drives the app,
  attaches screenshots, and answers PASS / FAIL as its first word. A FAIL goes
  back to the engineer and blocks `done`.
- **Works proactively.** Autopilots fire on a cron schedule or an inbound
  webhook (Jira / GitHub / GitLab) and create tasks on their own.
- **Publishes where you work.** A local repo gets an in-app merge request; a
  repo cloned from GitHub or GitLab gets a real PR/MR, with a poller following
  what happens to it afterwards.
- **Reports its own health.** A per-repo build · tests · coverage card, read
  from the JUnit and JaCoCo/Kover/Cobertura XML the tools write — never from
  what an agent said.

## Stack

| Layer | Technology | Notes |
|---|---|---|
| Language / runtime | **Python 3.14** | the version CI tests; `ruff` target is `py314` |
| Web | **FastAPI 0.140 + Uvicorn 0.51** | one daemon process, four surfaces |
| UI | **Jinja2 3.1 + htmx** | 62 templates, own CSS design system, no JS framework |
| Data | **SQLite (WAL)** | raw `sqlite3`, no ORM; consistent snapshots via `VACUUM INTO` |
| Config / types | **Pydantic 2.13 + PyYAML 6** | `${VAR}`-expanded YAML; secrets live elsewhere |
| Scheduling | **APScheduler 3.11** | cron / interval for autopilot ticks and daily jobs |
| CLI | **Click 8.4** | `sisyphus daemon` / `runners` / `dev` |
| Agent protocol | **MCP (JSON-RPC 2.0 over HTTP + stdio)** | 25 tools, scoped per role; per-run bearer token |
| Model runners | **opencode / Claude Code CLI** | pluggable profile layer: argv, env, MCP config per CLI |
| Integrations | **GitHub, GitLab, Jira REST · webhooks · OpenRouter** | PR/MR publishing, forge polling, JQL intake |
| Process isolation | **git worktrees · macOS Seatbelt · egress proxy** | write-deny on real repos, allowlisted `HOME`, opt-in host allowlist |
| Credentials | **dedicated keychain vault** | workers cannot read it, by sandbox rule |
| Tests | **pytest 9 · pytest-asyncio · Playwright 1.62 · coverage 7** | unit / component / process / e2e tiers |
| CI | **GitHub Actions (macOS)** | `ruff` + coverage floors (`core` ≥ 88%, `web` ≥ 74%) |
| Desktop / ops | **Swift WebKit shell · launchd** | native window over the same UI, start at login |

**Size:** ~18.9k LOC in `core/` (64 modules), ~5.0k in `web/`, ~1.85k tests
collected.

## Architecture at a glance

```
                        ┌──────────────── daemon (uvicorn) ───────────────┐
  Web UI (htmx) ──┐     │                                                 │
  JSON API      ──┼──▶  │  event queue → dispatch_forever (one pass/ev)   │
  MCP server    ──┤     │        │                                        │
  Autopilot tick──┘     │        ▼                                        │
                        │   orchestrator (an Agent) ── decide ONE action  │
                        │        │  spawn_worker / spawn_task / ask_human │
                        │        ▼            / complete / fail / note    │
                        │   worker  ── runner subprocess in a git         │
                        │   (agent)    worktree, MCP tools, usage booked  │
                        └─────────────────────────────────────────────────┘
        SQLite: tasks · messages · agents · runners · usage · merge_requests
                · autopilots · model_classes · devices · repo_health_runs
```

- **Two domain entities, no state machine.** A `Task` is a goal plus an
  append-only `Thread`. The thread is the only memory, so a daemon restart does
  not resume a state machine — the next event just re-reads the same thread.
- **The orchestrator is a cold function**: `(thread + event) → one action`. It
  holds no state between passes and posts its rationale before acting.
- **The orchestrator is itself an agent.** Its decisions are MCP tool calls it
  makes from a runner session; there is no direct model HTTP to short-circuit.
- **Model routing is config, not code.** Two classes — `orchestrator` (a strong
  model, used little) and `worker` (a cheap one, used a lot) — each an ordered
  list of runner + model. A spawn takes the first option whose provider still
  has room in its subscription windows; when one plan runs dry the work moves to
  the next without a human switching runners.
- **Spend is measured, not estimated.** Tokens (input / output / reasoning /
  cache read / cache write) and dollars are parsed from the runner's own JSON
  output into a per-run ledger.

## Guardrails

Restart recovery, total and idle timeouts, a global worker-concurrency
semaphore, per-task and daily budget caps, consecutive-failure and pass caps, a
circuit breaker on repeated actions, an in-flight guard against double spawns,
a startup preflight, and `/health` exposing `ready` / `degraded`. Every failure
path parks the task *visibly* — never a silent stall.

## Safety model for untrusted agent output

An agent is untrusted code that can write files and run commands, so:

- **Write isolation** — Seatbelt profile denies writes to every registered repo
  source and to credential directories, while re-allowing the workspace.
- **Read isolation** (opt-in) — a worker may read only its worktree, the
  workspace, and allowlisted caches, which closes prompt-injection exfiltration
  of secrets from other projects. Verified with Claude Code; opencode does not
  run under it yet.
- **Egress filtering** (opt-in) — traffic is forced through a CONNECT proxy with
  a host allowlist, port 443 only.
- **Credential vault** — a separate keychain file that the sandbox denies to
  every worker; the daemon opens it outside the sandbox.
- **No force, ever** — merges are dry-run first, land by adding a ref, and close
  reversibly. Nothing is deleted.

## Where it stands

The north star was **Sisyphus builds Sisyphus** — the system doing its own
development. [`tools/goal_report.py`](sisyphus/tools/goal_report.py) measures
the distance from git history and the daemon's own database, never from a
keyboard. Last 30 days of the build sprint, counted on 2026-10-11:

```
SELF-BUILD    38.3%   46 of 120 trunk commits, through 34 Sisyphus merges
autonomy      89.7%   26 of 29 delivering tasks needed no human turn
evidence      96.7%   29 of 30 merges cite approval + a recorded check
reliability   99.1%   1 of 111 terminal tasks failed
```

Those numbers are from Sisyphus working on its own repository, where the tasks
were scoped by the person who built it. Pointed at someone else's backlog, the
pipeline lost to a single agent in a terminal: every hand-off between agents
was a place to lose context, and repairing the pipeline took the time meant for
the bugs. The orchestrator also spent a strong model's budget on each task just
to decide who goes next.

Re-run `python3 sisyphus/tools/goal_report.py` for the current window; the
script keeps no state of its own, so it cannot drift from the record.

What held up, and is worth lifting out on its own:

- **QA on a device** — [`core/devices.py`](sisyphus/core/devices.py) and the
  `qa` role.
- **Repo health** — [`core/health.py`](sisyphus/core/health.py): build, tests
  and coverage from the reports, on a throwaway clone.
- **The merge gate** — [`core/verify.py`](sisyphus/core/verify.py): a check the
  daemon ran itself, tied to the commit it ran against.
- **The worker sandbox** — [`core/sandbox.py`](sisyphus/core/sandbox.py): a
  Seatbelt profile and an allowlisted `HOME`.

## Not in scope

No RAG, embeddings, vector store or fine-tuning — context is the task thread
plus the repo's own `AGENTS.md` / `CLAUDE.md`, not a retrieval pipeline. No
container per worker (the sandbox backend is macOS Seatbelt; Linux and Docker
backends are unimplemented). No multi-tenant or hosted mode: it is one daemon
on one machine, for one person.

## Quick start

Requires Python 3.14, git, and one runner CLI on your `PATH`
([opencode](https://opencode.ai) or
[Claude Code](https://claude.com/claude-code)). An API key is optional — both
use their own login. If you route models through OpenRouter, its key goes in the
UI under **Integrations → Keys & tokens**.

```bash
cd sisyphus
pip install -r requirements.txt
cp .env.example .env                  # optional: non-credential env vars
cp config.example.yaml config.yaml    # optional: budgets, worker cap, repos
python sisyphus.py daemon start
open http://localhost:7677            # Mission Control
```

Then **Repos → Add** your repository — it appears under **Projects** in the
sidebar. Open it, say in plain words what should get done, press Enter. On
macOS, `python sisyphus.py daemon install` keeps it alive across logins via
launchd, or open `desktop/Sisyphus.app`, a native window over the same UI.

## Surfaces

| Surface | Where | For |
|---|---|---|
| **Web UI** | `http://localhost:7677` | day-to-day operation |
| **JSON API** | `/api/v1/*` | scripting and integrations (bearer auth) |
| **MCP server** | `python mcp_server.py` | letting an external agent drive Sisyphus |
| **CLI** | `python sisyphus.py …` | daemon, runners, dev hooks |

The MCP server is a thin client of the daemon — it holds no state and forwards
every tool call over HTTP. Agent-facing MCP (the tools a *worker* gets) is served
inside the daemon and scoped per `(task, role)`.

## Layout

```
sisyphus/
  core/        domain + engine — orchestrator, worker, merge requests, sandbox
  web/         FastAPI app: htmx UI, JSON API, agent MCP tools
  docs/        architecture, the north star, ADRs, and an honest ledger of gaps
  desktop/     Sisyphus.app — a WebKit shell that owns the daemon
  tests/       unit tests in four tiers + a Playwright browser e2e
  sisyphus.py  CLI entrypoint      mcp_server.py  MCP stdio server
```

```bash
cd sisyphus && python3 -m pytest -q   # unit suite (1,852 tests)
pytest tests/e2e -q                   # browser e2e (needs requirements-dev.txt)
python -m ruff check core web tools tests
```

## Docs

- [Full manual](sisyphus/README.md) — every feature, configuration key and knob.
- [Architecture](sisyphus/docs/ARCHITECTURE.md) — domain model and engine.
- [The goal](sisyphus/docs/GOAL.md) — the north star and the metrics that measure it.
- [ADRs](sisyphus/docs/adr) — why the design is the way it is.
- [Known gaps](sisyphus/docs/known-gaps.md) — honest ledger of what is still broken.

---

MIT — see [LICENSE](LICENSE).
