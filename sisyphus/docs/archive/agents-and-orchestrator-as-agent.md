> **Archived 2026-08-20** — implemented in full (configurable agents, orchestrator-as-agent, task-level agent selection).

# Plan — Configurable Agents + orchestrator-as-agent (all AI via local CLI)

> **Status: IMPLEMENTED (all 3 phases).** Phase 1 — DB-backed Agent entity +
> Agents UI. Phase 2 — orchestrator is an agent; decisions are MCP tool calls,
> no raw model HTTP anywhere in core. Phase 3 — task-level agent selection
> (single-agent mode). 317 tests green. Sections below are the design record.

## Motivation

Two goals, one underlying change:

1. **No raw HTTP to any LLM.** Today every worker already runs through a local
   CLI (`opencode`), but the *orchestrator's* decision is a raw
   `urllib` POST to an OpenAI-compatible `/chat/completions`
   (`core/orchestrator.py:_decide_via_llm_sync`). That is the one place we talk
   to a model directly. It must go through a CLI like everything else.

2. **A multica.ai-style surface.** The user wants to (a) configure which CLI
   *runtimes* exist (today only opencode — this is the `runners` concept, which
   already exists), and (b) configure each **Agent** separately — its role,
   persona, tools, and which runtime it uses. And crucially: the **orchestrator
   should itself be an Agent**, so that creating a task can either pick a
   specific agent to handle it, or leave it to the orchestrator agent to
   dispatch.

## The unifying insight

> **The orchestrator is just another Agent.** Its "decision" is a tool call.

Right now roles (explorer/engineer/reviewer) are a hardcoded dict and the
orchestrator is bespoke code with a raw HTTP call. If we (1) promote roles to a
configurable **Agent** entity and (2) make the orchestrator an Agent whose
*actions are MCP tools*, then killing the raw HTTP (goal 1) and the
multica-style config (goal 2) become **the same work**: everything is "spawn an
agent (a local CLI session) scoped to an MCP toolset, and let it call one tool."

This reuses the entire existing MCP-worker contract. Nothing new in *how* an
agent runs — only *what* an agent is and *which* toolset it gets.

## Guiding principles (hold across all phases)

- **All AI runs through a local CLI runner.** No direct model HTTP anywhere.
- **Guards stay in daemon code, not in the agent.** Pass budget, circuit
  breaker, worker-in-flight, children-pending, and the done-gate remain
  deterministic Python. The agent only *chooses*; the daemon *validates and
  executes*. This is what keeps the loop safe from runaway cost and thrash.
- **Reuse the MCP contract.** Decisions are tool calls on the same `/mcp/agent`
  endpoint, scoped by the session's agent — identical machinery to workers.
- **Preserve the test seam.** `handle_event(decide_fn=…)` stays: production
  `_decide` spawns the orchestrator agent, tests inject a scripted `Decision`.
  All existing orchestrator tests keep passing unchanged.

---

## Phase 1 — Agents as a configurable entity

Promote the hardcoded `core/roles.py` dict to a first-class, DB-backed,
UI-managed entity, exactly mirroring how `runners` already works.

### Data model — a new `agents` table

```
agents {
  id, name,               -- "engineer", "explorer", "reviewer", "orchestrator", + custom
  persona TEXT,           -- the system-prompt persona text
  tools   TEXT(json),     -- allowlist: subset of the tool catalog
  runner_id TEXT NULL,    -- which CLI runtime (FK runners.id); NULL = default runner
  model TEXT,             -- optional per-agent model override
  kind TEXT,              -- "worker" | "orchestrator" (see Phase 2)
  enabled INT, is_builtin INT,
  created_at, updated_at
}
```

Seed the built-ins on init (`explorer`, `engineer`, `reviewer` as `kind=worker`;
`orchestrator` as `kind=orchestrator` — used in Phase 2). `is_builtin` marks
them so the UI can protect their identity while still allowing edits.

### The tool catalog

`web/agent_mcp.py`'s `TOOLS` dict is already the canonical catalog. An agent's
`tools` list must be a subset of its keys. Expose the catalog (name +
description + which `kind` it belongs to) via an API endpoint so the UI can
render checkboxes. Two families live in the catalog:

- **worker tools**: `get_context, comment, attach, commit, open_mr, finish,
  approve, request_changes, propose_subtasks`
- **orchestrator (decision) tools**: added in Phase 2 (`spawn_worker,
  spawn_task, ask_human, complete_task, fail_task, note`)

### Migrate consumers off `roles.py`

- `core/roles.py` → thin compatibility shim over `agents` (or delete once
  callers move). `get_role`/`role_tools` become `get_agent`/`agent_tools`
  reading the table.
- `core/worker.py`: `get_role(role)` → `get_agent(name)`; persona/tools/runner
  come from the agent row. `spawn_worker(task_id, role, …)` → the `role`
  argument becomes an **agent name**. The chosen agent's `runner_id` is passed
  down to `launch_agent` (finally using the per-runner selection that already
  exists in the launcher signature).
- `web/agent_mcp.py`: `role_tools(session["role"])` → `agent_tools(session["agent"])`.
- `core/sessions.py`: `mint(task_id, role, …)` → `mint(task_id, agent, …)`
  (rename field; keep behaviour).

### CRUD + API + MCP + UI

- `core/agents.py` — `list_agents/get_agent/add_agent/update_agent/
  delete_agent/agent_tools`, plus seeding (mirror `core/runners.py`).
- `web/api.py` — `/api/v1/agents` CRUD + `/api/v1/agents/tool-catalog`.
- `mcp_server.py` — `sisyphus_*_agent` control tools (mirror the runner tools).
- UI — an **Agents** page (like `/runners`): list agents, edit persona, tick
  tools, pick a runtime (runner) + model. Present runners on their page as
  "CLI runtimes".

### Key files
`core/agents.py` (new), `core/roles.py`, `core/worker.py`, `web/agent_mcp.py`,
`core/sessions.py`, `core/db.py` (schema), `web/api.py`, `web/routes.py`,
`mcp_server.py`, `web/templates/agents*.html` (new), `base.html` (nav).

### Verification
Built-ins seed and behave exactly as the old roles; a custom agent (e.g.
`docs-writer` with only `get_context, comment, finish`) can be created, is
tool-scoped correctly on `/mcp/agent`, and runs a task. All existing worker/MCP
tests pass after the role→agent rename.

---

## Phase 2 — Orchestrator as an agent (removes the raw HTTP)

Make the decision come from a local CLI session instead of a raw HTTP call,
**without** touching the guards, the `Decision`/`_execute` machinery, or the
tests.

### Decision tools (new MCP tools, scoped to `kind=orchestrator` agents)

Add to `web/agent_mcp.py`'s `TOOLS`, one per orchestrator action. Each handler
**records the chosen `Decision` for the current pass and ends the session** — it
does *not* execute the action itself (execution stays in `_execute`, so all the
disk/worktree/session logic remains in one place):

- `spawn_worker(agent, instruction)` — note: now names an **agent**, not a role.
- `spawn_task(subtasks)` — fan-out (unchanged semantics).
- `ask_human(question)`
- `complete_task(why)` / `fail_task(reason)`
- `note(text)` — pure thinking.

The orchestrator agent's flow is identical to a worker's: `get_context` to read
the thread, then exactly one decision tool — the mirror of a worker's `finish`.

### Swap only the `_decide` transport

`handle_event` is unchanged: it runs every guard, then calls `_decide`, then
`_execute`. Only `_decide` changes:

- **Before:** `_decide_via_llm_sync` → raw HTTP → `Decision`.
- **After:** mint a session for the task's orchestrator agent, spawn its CLI
  (runner) in a **scratch dir** (no worktree — it doesn't touch code), await
  completion, and read back the `Decision` its decision-tool call recorded. If
  the session ends without recording one (crash/timeout), fall back to the
  existing `ask_human` "decision service unreachable" path.

Because `_decide` still returns a `Decision`, the done-gate, circuit breaker,
pass budget, `_execute`, and every scripted test keep working untouched.

### The done-gate as a tool precondition

`complete_task` gets a precondition (matching the "when lives in the tool"
philosophy): if neither a reviewer has approved nor all children are done, the
tool refuses and tells the orchestrator agent to spawn a reviewer first. The
daemon's existing auto-override (`handle_event` converting a premature `done`
into a reviewer spawn) is kept as the deterministic backstop.

### Available-agents awareness

The orchestrator agent's prompt lists the currently-enabled `kind=worker`
agents (name + persona summary) so it knows what it can `spawn_worker(agent=…)`.
Adding an agent in the UI immediately widens the orchestrator's options — the
multica-style extensibility.

### Cost/latency — and its mitigation

An opencode session per decision is heavier than one HTTP call. Mitigations,
all already in place or free here:
- The guards short-circuit most events (worker-in-flight, children-pending →
  `noop` **before** spawning the orchestrator agent).
- The orchestrator is now an Agent → assign it a **fast/cheap runner** (its own
  `runner_id`), independent of the strong runner engineers use.
- The pass budget bounds the worst case.

### Key files
`core/orchestrator.py` (`_decide` transport, delete `_decide_via_llm_sync`
HTTP), `web/agent_mcp.py` (decision tools + a way to record the pass Decision),
`core/worker.py` (a "run an agent to completion, no worktree" spawn path shared
with workers), `core/roles/agents` (the orchestrator agent + its toolset).

### Verification
No `urllib`/`http` model call remains (`grep`). A real task runs end-to-end with
the decision coming from opencode. All scripted orchestrator tests pass. Point
the orchestrator agent at a different runner and confirm the decision uses it.

---

## Phase 3 — Task-level agent selection

Let task creation choose who handles the task.

### Model + routing

- `tasks` gains a nullable `agent` column (the handling agent's name).
- On the `created` event:
  - **`agent` set** → spawn that agent directly as the worker (single-agent
    mode); no orchestration. Its `finish` completes the task.
  - **`agent` empty** → spawn the **orchestrator agent** (dispatcher) — today's
    behaviour.

### Single-agent semantics (recommended default)

A picked agent handles the task solo: it runs, does its work with its own tools
(which may include `commit`/`open_mr`), and its `finish`/`approve` ends the
task. No automatic reviewer (there's no orchestrator in the loop) — if the user
wants coordination/review, they leave it on **Auto (orchestrator)**. This keeps
the two modes conceptually clean: *Auto = coordinated*, *pick = direct*.

### UI

New Task form gains an **Agent** dropdown: default `Auto (orchestrator)` plus
every enabled worker agent. (Complements the repo dropdown from Bucket B #3.)

### Key files
`core/store.py` (`agent` column + `create_task` arg), `core/orchestrator.py`
(`created` routing), `web/api.py` + `web/routes.py` (accept `agent`),
`web/templates/tasks_new.html`, `task_detail_content.html` (show handling agent).

### Verification
Creating a task with an agent runs only that agent (no orchestrator messages on
the thread). Creating without one dispatches via the orchestrator as before.

---

## Cross-cutting concerns

- **Safety / prompt-injection.** The thread is our own data, but decisions are
  still bounded by daemon-side guards + tool preconditions, not by trusting the
  agent. An agent cannot exceed its tool allowlist (server-enforced by token).
- **Migration.** `roles.py` retires into the `agents` table with built-ins
  seeded; sessions store `agent` instead of `role`. One additive migration each
  for `agents` (new table) and `tasks.agent` (Phase 3).
- **Testing.** The `decide_fn` seam means Phase 2 changes zero orchestrator
  test bodies. Phase 1/3 add agent-CRUD, tool-scoping, and routing tests.
- **Backwards behaviour.** With only the seeded agents and no task-level agent
  pick, the system behaves exactly as today — just with the decision coming
  from a CLI instead of HTTP.

## Sequencing

**1 → 2 → 3**, each shippable on its own:
- **Phase 1** gives the configurable Agents surface (no behaviour change).
- **Phase 2** removes the raw HTTP (the firm requirement) — depends on Phase 1
  (the orchestrator is an agent).
- **Phase 3** adds task-level agent choice — depends on Phase 1.

## Open decisions (defaults chosen; confirm or override)

1. **Single-agent mode = solo** (picked agent finishes the task; no auto-review)
   vs. "orchestrator restricted to one agent". *Default: solo.*
2. **Done-gate** kept as a deterministic auto-override *and* a tool precondition
   (belt-and-suspenders) vs. tool-precondition only. *Default: both.*
3. **Orchestrator's default runner** — seed it pointing at a fast/cheap runner,
   or the same default runner as workers? *Default: same default, user re-points
   via the Agents UI.*

## Verification (whole effort)

1. `pytest -q` green throughout; new agent/routing tests added per phase.
2. `grep` proves no direct model HTTP call remains after Phase 2.
3. Live: (a) create a custom agent in the UI; (b) run a task on **Auto** and see
   the orchestrator (now a CLI agent) dispatch it; (c) run a task with a **picked
   agent** and see only that agent act; (d) point the orchestrator agent at a
   different runtime and confirm it's used.
