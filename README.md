# ⚡ Sisyphus

**A self-hosted orchestrator for autonomous AI coding agents.**
Give it a goal — it plans, dispatches specialist agents into isolated git
worktrees, reviews the result, and opens a merge request.

[![CI](https://github.com/onelenyk/agentsbox/actions/workflows/ci.yml/badge.svg)](https://github.com/onelenyk/agentsbox/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Tests](https://img.shields.io/badge/tests-1150-brightgreen)
![Runs on](https://img.shields.io/badge/runs-on%20your%20machine-lightgrey)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)

---

Sisyphus runs on your host — no container, no hosted backend, and **no raw model
HTTP anywhere in the core**. Every AI decision, including the orchestrator's own,
goes through a local runner CLI ([opencode](https://opencode.ai) or
[Claude Code](https://claude.com/claude-code)) that you already have logged in.

```
  goal ──▶ orchestrator ──▶ explorer ──▶ engineer ──▶ reviewer ──▶ merge request
             (one decision per event, state lives in an append-only thread)
```

A task is done only when a reviewer approves it. A merge is allowed only when
that approval exists **and** the repo's own `check:` command passed in a
throwaway worktree — a number no agent authored. Overriding the gate is one
click, and the thread records that it was one.

## Highlights

- **Autonomous SDLC loop** — orchestrator coordinates explorer / engineer /
  reviewer; the orchestrator never certifies its own quality.
- **Cold decision function** — `(thread + event) → one action`. No state machine,
  so a daemon restart just resumes where it stopped.
- **Isolated workers** — each agent runs in its own git worktree, under a macOS
  Seatbelt profile and a private `HOME` built from an allowlist, so a worker
  carries what we chose rather than whatever the machine has installed.
- **Merge requests, local or real** — mergeability dry-run, merge / land / close,
  never force. Repos cloned from GitHub or GitLab publish actual PRs/MRs and a
  poller follows what happens to them.
- **Autopilots** — named automations fire on a cron schedule or a webhook
  (jira / github / gitlab) and create tasks on their own.
- **Mission Planner & Board Copilot** — plan a big goal into a DAG of draft tasks
  and release them deliberately; ask the board questions in plain words.
- **Guardrails** — worker concurrency cap, per-task and daily budget caps, total
  and idle timeouts, circuit breakers, restart recovery, startup preflight.
- **Four surfaces** — web UI, JSON API, MCP server, and a CLI.

## Quick start

Requires Python 3.10+, git, and one runner CLI on your `PATH`. An API key is
optional — opencode uses its own logged-in providers, Claude Code its OAuth login.

```bash
cd sisyphus
pip install -r requirements.txt
cp .env.example .env            # optional: forge / API tokens
cp config.example.yaml config.yaml   # optional: budgets, worker cap, repos
python sisyphus.py daemon start
open http://localhost:7677      # Mission Control
```

Then **Repos → Add** your repository and **Tasks → + New** — describe the goal in
plain words and hit **✨ AI structure**. On macOS, `python sisyphus.py daemon
install` keeps it alive across logins via launchd.

## Surfaces

| Surface | Where | For |
|---|---|---|
| **Web UI** | `http://localhost:7677` | day-to-day operation |
| **JSON API** | `/api/v1/*` | scripting and integrations |
| **MCP server** | `python mcp_server.py` | letting an external agent drive Sisyphus |
| **CLI** | `python sisyphus.py …` | daemon, runners, dev hooks |

## Layout

```
sisyphus/
  core/        domain + engine — orchestrator, worker, merge requests, sandbox
  web/         FastAPI app: htmx UI, JSON API, agent decision tools
  docs/        architecture, the north star, and an honest ledger of gaps
  tests/       1150 unit tests + a Playwright browser e2e
  sisyphus.py  CLI entrypoint      mcp_server.py  MCP stdio server
```

```bash
cd sisyphus && pytest -q          # unit suite
pytest tests/e2e -q               # browser e2e (needs requirements-dev.txt)
```

## Where it is going

The north star is **Sisyphus builds Sisyphus** — the system doing its own
development. `tools/goal_report.py` measures the distance from git history and
the daemon's own database, never from a keyboard:

```
SELF-BUILD    22.0%   40 of 182 trunk commits, through 26 Sisyphus merges
autonomy      67.6%   23 of 34 delivering tasks needed no human turn
reliability   92.4%   8 of 105 terminal tasks failed
```

Read next: [full manual](sisyphus/README.md) ·
[architecture](sisyphus/docs/ARCHITECTURE.md) ·
[the goal](sisyphus/docs/GOAL.md) ·
[known gaps](sisyphus/docs/known-gaps.md)

---

MIT — see [LICENSE](LICENSE).
