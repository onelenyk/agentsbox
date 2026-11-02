# Architecture — domain model & engine

Sisyphus is deliberately small at its core: **two entities, no state machines**.
Everything else is a thin layer around them.

## 1. Domain entities

### Task
`{ id, goal, repo, status, parent_id, agent, created_at, updated_at }`

- `status ∈ {active, waiting, done, failed}` — the only lifecycle.
- `repo` optional → a **repo-less** task runs read-only in a scratch dir.
- `agent` optional → pin one to run **solo** (orchestrator skipped).
- `parent_id` → **fan-out** child (one child per repo under a parent).

### Thread (messages)
An append-only list of `{ seq, author, kind, text, attachment, created_at }` per
task. The thread is the **single source of truth** — there is no separate run
state. Kinds include `note`, `question`, `answer`, `worker_started`,
`worker_finished`, `commit`, `result`, `mr`, `external`.

> Continuity comes entirely from re-reading the thread. A daemon restart doesn't
> "resume a state machine" — the next event just triggers a fresh pass over the
> same memory (`orchestrator.reconcile_on_boot`).

## 2. The orchestrator — a cold function

```
handle_event(task, event) := read thread → decide ONE action → post rationale → execute
```

The orchestrator is **not a live process** and holds no state. Every event runs
exactly one pass. The decision is one of:

| action | meaning |
|---|---|
| `spawn_worker(agent, instruction)` | run a worker agent (explorer/engineer/qa/reviewer/…) |
| `spawn_task(subtasks)` | fan out — one child task per `{repo, goal}` |
| `ask_human(question)` | park as `waiting` until a human replies |
| `complete` | done — **only** after a reviewer approved (or all children done) |
| `fail(reason)` | cannot proceed |
| `note(text)` | record a thought, take another pass |

**The orchestrator is itself an Agent.** Its decisions are MCP tool calls it
makes from a local runner session (`web/agent_mcp.py`), read back and executed.
There is *no direct model HTTP* — the orchestrator decides exactly the way a
worker works. It also **never judges quality itself**: "is this good/done?" is
expressed as `spawn_worker(role="reviewer")`, whose verdict returns as the next
`worker_finished` event.

## 3. Workers & runtimes

- A **worker** is one run of an **agent** (`core/agents.py`: `{name, persona,
  tools[], runner, kind}`) as a **runner-CLI subprocess** (`core/launcher.py`) in
  an **isolated git worktree** (`core/worktree.py`), granted a scoped set of MCP
  tools. Each task gets a **fresh per-task bare clone** from the repo source, so
  work never branches from a stale snapshot.
- A **runtime/runner** (`core/runners.py`) is a CLI-backend config: binary, model,
  api-key env, base URL. `core/runner_profiles.py` knows each supported CLI's
  shape — argv, env, MCP config format, tool-name prefix — currently **opencode**
  and **claude-cli** (Claude Code). `core/runner_detect.py` resolves the binary
  to an absolute path (PATH + common install dirs) so it works under a minimal
  PATH (e.g. launchd), and `core/runner_smoke.py` proves a runner can call an
  MCP tool before it is trusted with real work.
- Every run's token/cost is captured from the runner CLI's JSON output
  (`core/usage.py`) — real spend, no estimates.

## 4. The event loop

```
emit(task_id, event) → asyncio.Queue → dispatch_forever() → handle_event()
```

`dispatch_forever` is the daemon's single background consumer (started once in
`web/routes.py::startup`, guarded against double-start). Workers are launched as
background tasks behind a **global concurrency semaphore**; a task's next step is
driven by the `worker_finished` event its worker emits when done. Events also
come from the API (`created`, `human_replied`), webhooks (`external`), and the
autopilot loop.

## 5. Guardrails (why it doesn't run away)

- **Restart recovery** — `reconcile_on_boot` re-drives active tasks orphaned by a
  restart.
- **Timeouts** — total + idle (no output for N s) in the launcher.
- **Concurrency cap** — `max_concurrent_workers` semaphore.
- **Budget** — per-task and daily `$` caps park the task as `waiting` when hit; a
  human reply resumes past it.
- **Loop breakers** — consecutive-failure cap, pass budget, circuit breaker on a
  repeated action, and an in-flight guard (no double-spawn).
- **Preflight** — startup checks the DB + default runner binary; `/health` exposes
  `ready/degraded`.
- **Never a silent stall** — any pass exception parks the task visibly.

## 6. Local Merge Requests

`core/mergerequests.py` is a tiny in-app forge over real git:

- **mergeability** — dry-run merge in a throwaway worktree of the per-task bare
  clone → `{ok, mergeable, conflicts[]}`.
- **merge** into a chosen target — in place if it's the current branch (and clean),
  else via a temporary worktree so the working tree is never disturbed; aborts on
  conflict.
- **land** — fetch the branch into the real repo (adds a ref, touches nothing else).
- **close** — mark closed. Reversible, never force, never delete.

## 7. Autopilot — proactive work

`core/autopilot.py`: named automations with a **trigger**:

- **schedule** — cron (via apscheduler) or interval; a tick loop fires due ones and
  advances `next_run`. Boot recomputes `next_run` from now (missed fires are
  skipped, not replayed).
- **webhook** — an incoming `/api/v1/webhooks/{platform}` event matching a
  platform + optional event filter fires the autopilot.

Firing renders a `{{payload}}` goal template → `store.create_task` →
`emit("created")`. Autopilot tasks flow through the *same* guardrails as any task.
`skip_if_running` stops monitors piling up.

## 8. Devices, QA and repo health

- **Devices** (`core/devices.py`) are discovered live from `adb`, `emulator`,
  `xcrun simctl` and `devicectl`; only the human's decisions are stored. A role
  that declares `devices` (today: `qa`) gets one leased before its worker
  starts and returned in a `finally`. With none free, the worker waits.
  Emulators Sisyphus booted stay warm and are stopped after
  `devices.idle_minutes`; one the human launched is left alone. Stale leases
  are cleared at boot.
- **QA** is a worker role, not a separate engine. Its verdict is the first word
  of its result. The orchestrator sends a FAIL back to the engineer with the
  findings and will not complete over it; `repos[].qa` makes the run mandatory
  before review.
- **Repo health** (`core/health.py`) runs a per-repo profile — build, test,
  optional coverage — on a throwaway shallow clone with the setup hook applied,
  and parses JUnit and JaCoCo/Kover/Cobertura XML itself. It runs on the
  human's button and daily after that; a repo never run is never started by
  the loop.

## 9. Data model (SQLite, `workspace/sisyphus.db`)

| table | holds |
|---|---|
| `tasks` | the task rows |
| `messages` | the append-only threads |
| `runners` | CLI-backend configs |
| `agents` | personas + tool allowlists + kind |
| `usage` | per-run token/cost ledger |
| `merge_requests` | local MR entity |
| `autopilots` | scheduled/webhook automations |
| `model_classes` | ordered runner + model options per class |
| `provider_limits`, `limit_waits` | subscription windows, and tasks parked on them |
| `devices`, `device_leases`, `device_boots` | which devices agents may use, who holds one, what we started |
| `repo_health_runs`, `repo_health_proposals` | health card runs, and profiles awaiting confirmation |
| `integration_events`, `jira_sync` | what was done in Jira and the forges |

WAL mode; each operation opens and closes its own connection. Backups
(`core/backup.py`) snapshot the whole file consistently via `VACUUM INTO`.

## 10. Surfaces share one core

Web UI (`web/routes.py`, htmx), JSON API (`web/api.py`, `/api/v1`), the MCP server
(`mcp_server.py`, a thin HTTP client of the daemon), and the CLI (`sisyphus.py`)
all drive the same `core/` — the daemon just exposes it.
