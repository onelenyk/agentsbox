# Plan — Multi-repo task fan-out (parent → children)

> **Status: IMPLEMENTED.** All layers shipped — `parent_id` data model,
> `spawn_task` + roll-up + guards in the orchestrator, cascade delete,
> `propose_subtasks` planner tool, and the subtasks/breadcrumb/board UI. 276
> tests green; verified live. Sections below are the design record.

## Motivation

A single unit of intent often spans several codebases. The driving example: a
Jira ticket describing a bug/feature that must be implemented in **both** an iOS
repo and an Android repo. Today the model is **one repo per task**, so this can
only be done by hand — create two unrelated tasks, track them yourself, and
mentally roll their results back up to the ticket.

This plan adds **task fan-out**: a coordination (repo-less) parent task that
holds the ticket's intent + a shared plan, spawns one repo-backed **child task**
per repo, and aggregates their results.

## The organizing insight

> **A child task is to a parent what a worker is to a task.**

The orchestrator already spawns a worker and waits for `worker_finished`, then
re-decides. A parent spawns a child *task* and waits for `child_finished`, then
re-decides. Same shape, one level up. This keeps the system's core rule intact —
**2 entities (Task, Thread), no state machines** — because we are *not* adding a
new execution engine. A child is just a normal task that drives its own existing
loop (its own engineer, commit, MR/diff, reviewer). The only new thing is a link
between tasks and the ~30 lines of orchestrator logic that treat "children" the
way it already treats "a worker."

Symmetry that guides every decision below:

| Task → Worker | Parent → Child |
|---|---|
| `spawn_worker` action | `spawn_task` action |
| worker writes `worker_finished` on the task thread | child writes `child_finished` on the **parent** thread |
| `_worker_in_flight` → wait, don't spawn another | `_children_pending` → wait, don't finish |
| `done` gated on reviewer approval | parent `done` gated on **all children done** |

---

## 1. Data model — one nullable field

```
tasks { id, goal, repo, status, parent_id?, created_at, updated_at }
                                  ^^^^^^^^^ new: nullable, references tasks.id
```

- `parent_id = NULL` → a normal or parent task.
- `parent_id` set → a child. At most one parent per task ⇒ a **tree**, no join
  table. Still just the Task entity — no new entity, no state machine.

**Key files.** `core/db.py` (schema + an `ALTER TABLE` migration shim for
existing DBs), `core/store.py`:
- `create_task(goal, repo="", status="active", parent_id=None)` — new arg.
- `list_children(parent_id) -> list[dict]` — `SELECT * FROM tasks WHERE parent_id = ?`.
- `get_task` already returns the whole row, so `parent_id` rides along for free.

---

## 2. Fan-out — how a parent produces children

Two steps, each reusing existing machinery.

**a) The parent plans.** The parent (repo-less, runs in its scratch dir) spawns a
**planner worker** (the `explorer` role, read-only) that reads the ticket text
plus — optionally — the two repos, and proposes the split via a new role-scoped
MCP tool:

```
propose_subtasks(items=[
  {"repo": "ios-app",     "goal": "Implement dark-mode toggle per PROJ-123"},
  {"repo": "android-app", "goal": "Implement dark-mode toggle per PROJ-123"},
])
```

This posts one structured message (`kind="plan"`) to the parent thread. Using a
structured tool — instead of asking the orchestrator LLM to parse prose into a
clean list — follows the existing "preconditions/'when' live inside the tool"
philosophy and gives the orchestrator reliable structured data.

**Key files.** `web/agent_mcp.py` (new `_propose_subtasks` handler + TOOLS
entry), `core/roles.py` (add `propose_subtasks` to the explorer/planner
tool-list, or mint a dedicated `planner` role).

**b) The orchestrator fans out.** New action in the orchestrator vocabulary:

```json
{"action": "spawn_task",
 "subtasks": [{"repo": "ios-app", "goal": "..."}, {"repo": "android-app", "goal": "..."}],
 "why": "fan out PROJ-123 to both platforms"}
```

Executing it: for each item, `create_task(goal, repo, parent_id=<parent>)` and
`emit(child_id, "created")`. Each child now runs the **normal** loop entirely on
its own. Spawning N children in one pass is still "one action" — the
one-action-per-pass rule holds.

**Key files.** `core/orchestrator.py`:
- Extend `Action` literal + `Decision` model (`subtasks: Optional[list[Subtask]]`).
- Extend `SYSTEM_PROMPT` with the `spawn_task` shape and when to use it
  (after a planner has proposed a split).
- `_execute` handles `spawn_task`.

---

## 3. Roll-up — children reporting back

Exactly one insertion point. `orchestrator._terminate(task_id, status)` is the
single place every task reaches a terminal status. Add:

```python
task = store.get_task(task_id)
if task and task.get("parent_id"):
    store.post_message(task["parent_id"], "system", kind="child_finished",
                       text=f"[{status}] child '{task['goal']}' ({task['repo']})")
    emit(task["parent_id"], "child_finished")
```

So when the iOS child finishes it writes to the **parent's** thread and wakes the
parent — the mirror image of a worker writing `worker_finished`. The parent's
thread accumulates all `child_finished` messages, so its orchestrator sees the
full picture on the next pass with no extra queries.

**Key files.** `core/orchestrator.py` (the `_terminate` hook), `Event.type` doc
gains `child_finished`.

---

## 4. Parent orchestrator logic — two small additions

Mirror the worker guards in `handle_event`:

```python
def _children_pending(task_id) -> bool:
    kids = store.list_children(task_id)
    return bool(kids) and any(c["status"] not in ("done", "failed") for c in kids)
```

- After fan-out, while `_children_pending` → **noop and wait** (just like
  `_worker_in_flight`). The `child_finished` events drive re-evaluation; no
  polling, no `continue` re-emit for this state.
- When all children are terminal → the parent aggregates from its thread and
  decides: all `done` → `done`; any `failed` → `ask_human`, or `spawn_task` a
  single fix-child on the failed repo.

**Done-gate branch.** A coordinator parent writes no code, so its `done` must NOT
require a reviewer. Change the gate to:

```python
if decision.action == "done" and not (_reviewer_approved(task_id)
                                       or _all_children_done(task_id)):
    ...spawn reviewer...
```

i.e. a task with children is "done" when its children are done; a leaf task keeps
the existing reviewer-approval gate.

**Key files.** `core/orchestrator.py` (`_children_pending`, `_all_children_done`,
the two guard edits).

---

## 5. UI

All read from `store.list_children(parent_id)` — no new plumbing.

- **Parent detail** (`task_detail_content.html`): a "Subtasks" section — one row
  per child with its status badge, linking to the child's page; a summary chip
  `Subtasks 1/2 done`.
- **Child detail:** a breadcrumb `↑ Part of: <parent goal>` linking to the parent.
- **Board** (`tasks_list.html`): parent card shows the `1/2 done` chip; children
  can be hidden from the top-level board (filter `parent_id IS NULL`) so the
  board stays a list of top-level work, with children reachable by drilling in.

**Key files.** `web/routes.py` (pass `children` / `parent` into the detail
context; filter the board list), `task_detail_content.html`, `tasks_list.html`.

---

## 6. Edge cases (decide up front)

- **Cleanup / cascade.** Deleting a parent cascade-deletes its children (deep):
  extend `delete_task` / `cleanup_task` to walk `list_children`. A child's disk
  (worktree, bare clone, scratch, attachments) is reclaimed per child as today.
- **Partial failure.** iOS done, Android failed → parent surfaces "1/2 failed"
  and either asks the human or spawns one fix-child on the failed repo. The
  orchestrator decides from the thread; no special status needed.
- **Depth.** The tree *can* nest (a child could fan out again), but cap at **one
  level** initially — `spawn_task` only allowed on a task that itself has no
  `parent_id` — to avoid runaway trees. Lift later if wanted.
- **Pass budget.** The parent's `MAX_PASSES` budget must not fail a parent that
  is legitimately just waiting on slow children. `_children_pending` → `noop`
  passes should not count toward the runaway budget (or count them but raise the
  budget for tasks with children).
- **Orphan reconcile.** `reconcile_on_boot` already re-drives `active` tasks; a
  parent waiting on children is `active` with `child_finished` as its last-seen
  driver, so a restart re-emitting is harmless (it re-reads and waits again).

---

## Work summary

| Piece | Files | Size |
|---|---|---|
| `parent_id` column + migration + `list_children` + `create_task` arg | `core/db.py`, `core/store.py` | Small |
| `propose_subtasks` planner tool | `web/agent_mcp.py`, `core/roles.py` | Small |
| `spawn_task` action + `_children_pending` + `_all_children_done` + roll-up in `_terminate` + done-gate | `core/orchestrator.py` | **Medium — the core** |
| Cascade delete for parents | `core/store.py`, `core/worker.py` | Small |
| Subtasks section + breadcrumb + board filter | `web/routes.py`, `task_detail_content.html`, `tasks_list.html` | Small |
| Tests: fan-out, roll-up, pending-guard, done-gate, cascade, UI | `tests/` | Medium |

The only genuinely new logic is the handful of orchestrator methods that treat a
child task the way the orchestrator already treats a worker. Everything else —
the child's own engineer/commit/MR/reviewer loop, worktrees, providers,
attachments, the thread — is reused unchanged.

## Verification

1. `pytest -q` green, including new fan-out/roll-up/cascade tests.
2. Live: create a repo-less parent → planner proposes 2 subtasks → orchestrator
   fans out → two repo-backed children each commit + produce their MR/diff →
   both report `child_finished` → parent aggregates → `done`. Confirm the parent
   detail page shows both children and each child links back to the parent.
3. Partial-failure path: force one child to fail; confirm the parent surfaces
   "1/2 failed" and asks the human rather than silently completing.
