# Unify CLI and Web Through Shared Operations Layer

## TL;DR

> **Quick Summary**: Extract all card/pipeline operation logic from CLI (`sisyphus.py`) and Web (`routes.py`) into a single `core/operations.py` module. Both interfaces become thin wrappers calling the same code — fixing 18 behavioral divergences, 5 critical bugs, and eliminating ~200 lines of duplicated logic.
>
> **Deliverables**:
> - `core/operations.py` — unified operations API
> - Refactored `sisyphus.py` — thin Click wrappers
> - Refactored `web/routes.py` — thin FastAPI wrappers
>
> **Estimated Effort**: Medium
> **Parallel Execution**: YES — 2 waves
> **Critical Path**: Task 1 → Task 2 → Task 4 → Task 10 → Task 11

---

## Context

### Original Request
CLI and Web currently have independent implementations of card creation, running, resuming, and canceling. They share `core/pipeline.py`, `core/state.py`, `core/launcher.py` but duplicate all orchestration logic — with different bugs, different behaviors, and no shared contract.

### Interview Summary
**Key Discussions**:
- CLI is intended as the PRIMARY interaction mode; Web should extend it
- 52 behavioral differences identified (18 operational)
- 5 critical bugs across both paths

**Research Findings**:
- `sisyphus.py` hardcodes `/app/pipelines/` — fails on macOS
- CLI `resume` resets step states before re-running; Web does NOT (creates duplicate steps)
- CLI `cancel` kills opencode processes; Web does NOT (agent keeps running)
- Neither side passes `cancel_event` through `run_pipeline`
- CLI `--detach` creates card but never runs pipeline (broken)

---

## Work Objectives

### Core Objective
Create `core/operations.py` as the single-source-of-truth for all card lifecycle operations, then refactor both CLI and Web to be thin wrappers.

### Concrete Deliverables
- `sisyphus/core/operations.py` — new module (~150 lines)
- `sisyphus/sisyphus.py` — refactored CLI (~150 lines, down from 413)
- `sisyphus/web/routes.py` — refactored Web (~180 lines, down from 313)
- Updated tests to cover unified operations

### Definition of Done
- [ ] `core/operations.py` handles: create, create_and_run, run, resume, cancel, get_detail, resolve_path
- [ ] CLI `sisyphus run` → `operations.create_and_run()`
- [ ] CLI `sisyphus resume` → `operations.resume_card()`
- [ ] CLI `sisyphus cancel` → `operations.cancel_card()`
- [ ] Web `POST /api/cards` → `operations.create_card_only()` or `operations.create_and_run_async()`
- [ ] Web `POST /api/cards/{id}/run` → `operations.run_card()`
- [ ] Web `POST /api/cards/{id}/resume` → `operations.resume_card()`
- [ ] Web `POST /api/cards/{id}/cancel` → `operations.cancel_card()`
- [ ] Pipeline path works on both macOS and Docker
- [ ] Cancel kills subprocesses (both paths)
- [ ] Resume resets step states before re-running (both paths)
- [ ] All 45 existing tests pass
- [ ] New tests cover operations module

### Must Have
- Unified create/run/resume/cancel logic
- Portable pipeline path resolution
- CancelEvent wiring through pipeline
- Step state reset on resume (match CLI behavior)
- Process kill on cancel (match CLI behavior)

### Must NOT Have (Guardrails)
- Don't remove the Backlog/Kanban workflow
- Don't change how `run_pipeline` works internally
- Don't break the test suite
- Don't add authentication (out of scope)
- Don't change the DB schema

---

## Verification Strategy

### Test Decision
- **Infrastructure exists**: YES
- **Automated tests**: Tests-after
- **Framework**: pytest

### QA Policy
Every task includes agent-executed QA scenarios using curl for API verification and CLI for command verification.

---

## Execution Strategy

### Parallel Execution Waves

```
Wave 1 (Start Immediately — foundation):
├── Task 1: core/operations.py — create, run, resume, cancel, resolve_path [deep]
├── Task 2: Update tests for operations module [quick]

Wave 2 (After Wave 1 — refactor interfaces, MAX PARALLEL):
├── Task 3: Refactor CLI sisyphus.py to use operations [deep]
├── Task 4: Refactor Web routes.py to use operations [deep]

Wave 3 (Integration):
├── Task 5: Wire cancel_event through pipeline [deep]
├── Task 6: End-to-end verification + regression tests [unspecified-high]
```

Critical Path: Task 1 → Task 3,4 (parallel) → Task 6
Max Concurrent: 3 (Wave 2)

---

## TODOs

- [x] 1. Create `core/operations.py` — unified operations module

  **What to do**:
  - Create `sisyphus/core/operations.py` with these functions:

    ```python
    def resolve_pipeline_path(name: str) -> Path:
        """Portable: macOS sisyphus/pipelines/ | Docker /app/pipelines/"""
        # Try local first, fall back to /app/

    def create_card_only(pipeline_name, platforms, description, jira_url=None) -> dict:
        """Create card in backlog. No pipeline execution."""

    def create_and_run(pipeline_name, platforms, description, jira_url=None, repo_url=None) -> dict:
        """Create card + run pipeline synchronously (for CLI). Returns result dict."""

    async def create_and_run_async(pipeline_name, platforms, description, jira_url=None, repo_url=None) -> str:
        """Create card + fire pipeline in background (for Web). Returns card_id."""

    async def run_card(card_id, repo_url=None) -> None:
        """Run pipeline for existing card. Sets status to running, fires bg task."""

    async def resume_card(card_id) -> None:
        """Resume failed card. Resets failed/pending steps via transition_step(), re-runs pipeline."""

    async def cancel_card(card_id) -> None:
        """Cancel running card. Kills opencode processes, updates card+run status, sets cancel_event."""

    def get_card_detail(card_id) -> dict:
        """Full detail: card + latest run + steps + resources + repo_name."""

    def list_cards_filtered(pipeline=None, status=None, platform=None) -> list:
        """Filtered card listing for CLI."""
    ```

  - `create_and_run()`: calls `create_card_only()` then `asyncio.run(run_pipeline(...))`
  - `create_and_run_async()`: calls `create_card_only()` then `asyncio.create_task(_run_bg_async(...))`
  - `run_card()`: sets card to "running", resolves repo if none provided, fires `_run_bg_async`
  - `resume_card()`: finds failed/pending steps, resets each via `transition_step("running")`, then runs pipeline (matches CLI behavior)
  - `cancel_card()`: finds running steps, kills opencode via `pgrep -f opencode.*{card_id}` + SIGTERM/SIGKILL, updates DB, creates cancel_event
  - `resolve_pipeline_path()`: tries `Path(__file__).parent.parent / "pipelines" / f"{name}.yaml"` first; if missing, tries `/app/pipelines/{name}.yaml`
  - Move `_kill_opencode_for_card` from `sisyphus.py` into operations
  - Move `_run_bg` logic from `routes.py` into `_run_bg_async` internal function

  **Must NOT do**:
  - Don't change `run_pipeline()` signature (it stays as-is)
  - Don't modify `core/state.py`, `core/launcher.py`, `core/worktree.py`

  **Recommended Agent Profile**:
  - **Category**: `deep`
    - Reason: Complex module design with multiple integration points, async/sync bridging, error handling
  - **Skills**: [`python`]
  - **Skills Evaluated but Omitted**: None

  **Parallelization**:
  - **Can Run In Parallel**: NO
  - **Parallel Group**: Wave 1 (sequential foundation)
  - **Blocks**: Tasks 3, 4, 5
  - **Blocked By**: None (can start immediately)

  **References**:
  - `sisyphus/sisyphus.py:44-71` — CLI `run` command logic to port into `create_and_run()`
  - `sisyphus/sisyphus.py:262-317` — CLI `resume` command (step reset logic to port)
  - `sisyphus/sisyphus.py:206-258` — CLI `cancel` command (process kill logic to port)
  - `sisyphus/web/routes.py:226-256` — Web create/run/resume/cancel endpoints to refactor
  - `sisyphus/core/pipeline.py:39-78` — `run_pipeline()` signature and early return flow
  - `sisyphus/core/state.py:33-53` — `create_card()` signature
  - `sisyphus/core/repos.py:119-125` — `get_repo_by_platform()` for auto-resolving repo_url

  **Acceptance Criteria**:
  - [ ] `resolve_pipeline_path("investigate")` returns correct path on macOS
  - [ ] `create_card_only()` returns card dict with status "backlog"
  - [ ] `create_and_run()` blocks until pipeline completes, returns result dict
  - [ ] `create_and_run_async()` returns card_id immediately, pipeline runs in background
  - [ ] `resume_card()` resets failed steps to "running" before re-executing
  - [ ] `cancel_card()` updates card to "failed", run to "failed", and kills opencode processes
  - [ ] Module imports cleanly: `from core.operations import *`

  **QA Scenarios**:

  ```
  Scenario: Create card only (backlog)
    Tool: Bash (python3 REPL)
    Steps:
      1. python3 -c "from core.operations import create_card_only; c = create_card_only('investigate', ['android'], 'test'); print(c['status'])"
    Expected Result: Prints "backlog"
    Evidence: .sisyphus/evidence/task-1-create-backlog.txt

  Scenario: Create and run synchronously
    Tool: Bash (python3 REPL)
    Steps:
      1. python3 -c "from core.operations import create_and_run; r = create_and_run('investigate', ['android'], 'sync test'); print(r['status'])"
    Expected Result: Prints "completed" or "failed"
    Evidence: .sisyphus/evidence/task-1-create-run.txt

  Scenario: Cancel kills processes
    Tool: Bash (python3 REPL)
    Steps:
      1. Create card, note card_id
      2. python3 -c "from core.operations import cancel_card; cancel_card('card-...')"
      3. Verify card status is "failed" and no opencode processes with card_id remain
    Expected Result: Card status "failed", pgrep shows no opencode for card_id
    Evidence: .sisyphus/evidence/task-1-cancel.txt
  ```

  **Commit**: YES (groups with Task 2)
  - Message: `feat(ops): create unified operations module`
  - Files: `sisyphus/core/operations.py`

- [x] 3. Refactor CLI `sisyphus.py` to thin wrappers

  **What to do**:
  - Replace inline logic in each Click command with calls to `core.operations`:

    | Command | Old (inline) | New (operations call) |
    |---|---|---|
    | `sisyphus run` | create_card + asyncio.run(run_pipeline) | `operations.create_and_run()` |
    | `sisyphus run --detach` | create_card only (broken) | `asyncio.run(operations.create_and_run_async())` |
    | `sisyphus resume` | query steps + transition_step + asyncio.run(run_pipeline) | `asyncio.run(operations.resume_card())` |
    | `sisyphus cancel` | query steps + transition_step + pgrep kill | `operations.cancel_card()` |
    | `sisyphus status` | get_card + get_steps + format | `operations.get_card_detail()` |
    | `sisyphus list` | list_cards + filter | `operations.list_cards_filtered()` |

  - Remove:
    - `_kill_opencode_for_card()` function (moved to operations)
    - `_show_card_detail()` function (use `operations.get_card_detail()`)
    - `_show_all_cards()` → use `operations.list_cards_filtered()`
  - Keep:
    - `logs` command (filesystem log reading — no DB equivalent)
    - `cleanup` command (filesystem cleanup — no DB equivalent)
    - `scheduler` group (status only — already thin)
    - `_show_resources()` helper
  - Remove the `/app/pipelines/` hardcode from `run` command — use `operations.resolve_pipeline_path()`
  - Fix `--detach`: use `asyncio.run(operations.create_and_run_async())` for actual background execution

  **Must NOT do**:
  - Don't change `logs`, `cleanup`, or `scheduler` commands
  - Don't change Click decorators or argument names
  - Don't remove `init_db()` / `recover_orphaned_steps()` / `start_scheduler()` from CLI group

  **Recommended Agent Profile**:
  - **Category**: `deep`
    - Reason: Refactoring CLI with careful async/sync bridging, matching existing argument parsing
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: YES (with Task 4)
  - **Parallel Group**: Wave 2 (with Task 4)
  - **Blocks**: Task 6
  - **Blocked By**: Task 1

  **References**:
  - `sisyphus/sisyphus.py:44-71` — `run` command to refactor
  - `sisyphus/sisyphus.py:262-317` — `resume` command to refactor
  - `sisyphus/sisyphus.py:206-258` — `cancel` command to refactor
  - `sisyphus/sisyphus.py:88-101` — `_show_all_cards` to refactor
  - `sisyphus/sisyphus.py:103-157` — `_show_card_detail` to refactor
  - `sisyphus/core/operations.py` — target API (Task 1 output)

  **Acceptance Criteria**:
  - [ ] `sisyphus run --desc "test"` creates card, runs pipeline, prints result
  - [ ] `sisyphus run --desc "test" --detach` creates card, runs pipeline in background
  - [ ] `sisyphus resume <card_id>` resets steps and re-runs
  - [ ] `sisyphus cancel <card_id>` kills processes and updates DB
  - [ ] `sisyphus status` shows all cards
  - [ ] `sisyphus status <card_id>` shows detail with steps
  - [ ] `sisyphus logs <card_id>` still works
  - [ ] `sisyphus cleanup` still works

  **QA Scenarios**:

  ```
  Scenario: CLI run creates and executes pipeline
    Tool: interactive_bash (tmux)
    Steps:
      1. cd /Users/lenyk/workproject/agentsbox/sisyphus
      2. WORKSPACE_ROOT=./workspace python3 sisyphus.py run --desc "qa test" --pipeline investigate
    Expected Result: Creates card, runs pipeline, prints "Pipeline completed" or "Pipeline failed"
    Evidence: .sisyphus/evidence/task-3-cli-run.txt

  Scenario: CLI detach runs in background
    Tool: interactive_bash (tmux)
    Steps:
      1. WORKSPACE_ROOT=./workspace python3 sisyphus.py run --desc "bg test" --detach
      2. sleep 3
      3. WORKSPACE_ROOT=./workspace python3 sisyphus.py status <card_id>
    Expected Result: Card shows status "running" or "completed"
    Evidence: .sisyphus/evidence/task-3-cli-detach.txt

  Scenario: CLI cancel kills agent
    Tool: interactive_bash (tmux)
    Steps:
      1. Create card, note card_id
      2. WORKSPACE_ROOT=./workspace python3 sisyphus.py cancel <card_id>
      3. ps aux | grep opencode | grep <card_id>
    Expected Result: No opencode processes for card_id
    Evidence: .sisyphus/evidence/task-3-cli-cancel.txt
  ```

  **Commit**: YES
  - Message: `refactor(cli): use unified operations module`
  - Files: `sisyphus/sisyphus.py`

- [x] 4. Refactor Web `routes.py` to thin wrappers

  **What to do**:
  - Replace inline logic in FastAPI endpoints with calls to `core.operations`:

    | Endpoint | Old (inline) | New (operations call) |
    |---|---|---|
    | `POST /api/cards` | create_card + optional _run_bg | `operations.create_card_only()` or `operations.create_and_run_async()` |
    | `POST /api/cards/{id}/run` | get_card + update_status + _run_bg | `operations.run_card(card_id)` |
    | `POST /api/cards/{id}/resume` | get_card + update_status + _run_bg | `operations.resume_card(card_id)` |
    | `POST /api/cards/{id}/cancel` | query steps + transition_step + update card/run | `operations.cancel_card(card_id)` |
    | `GET /api/cards/{id}/detail` | get_card + query run + get_steps + list_resources | Use `operations.get_card_detail(card_id)` for data fetching |
    | `GET /api/cards/board` | Direct SQL query | Keep existing query (board rendering is Web-specific) |
    | `GET /dashboard` | list_cards + count | Keep existing (stats are Web-specific) |

  - Remove:
    - `_run_bg()` function (moved to operations as internal `_run_bg_async`)
    - All `asyncio.create_task(_run_bg(...))` calls
    - Duplicate step querying logic
  - Keep:
    - Template rendering (Jinja2 is Web-only)
    - Board query with enriched stats (Web-specific presentation)
    - Repos CRUD endpoints (no CLI equivalent)
    - Agents/Pipelines/History/Runtime pages (Web-only views)
  - Pass `gitlab_repo_url` from card's platform to `run_card()` and `resume_card()` via `get_repo_by_platform()`

  **Must NOT do**:
  - Don't change template rendering
  - Don't change repos/agents/pipelines/history/runtime endpoints
  - Don't change Kanban board structure

  **Recommended Agent Profile**:
  - **Category**: `deep`
    - Reason: Refactoring async FastAPI routes, careful integration with HTMX events and template rendering
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: YES (with Task 3)
  - **Parallel Group**: Wave 2 (with Task 3)
  - **Blocks**: Task 6
  - **Blocked By**: Task 1

  **References**:
  - `sisyphus/web/routes.py:226-256` — create/run/resume/cancel endpoints to refactor
  - `sisyphus/web/routes.py:175-220` — board and detail endpoints
  - `sisyphus/web/routes.py:265-271` — `_run_bg()` to remove
  - `sisyphus/core/operations.py` — target API (Task 1 output)
  - `sisyphus/web/templates/board_content.html` — board rendering (keep as-is)

  **Acceptance Criteria**:
  - [ ] `POST /api/cards` with `run_now=0` creates card in backlog, no pipeline runs
  - [ ] `POST /api/cards` with `run_now=1` creates card, pipeline runs in background
  - [ ] `POST /api/cards/{id}/run` starts pipeline for existing card
  - [ ] `POST /api/cards/{id}/resume` resets steps and re-runs
  - [ ] `POST /api/cards/{id}/cancel` kills processes and updates statuses
  - [ ] Board refreshes correctly after cardCreated/cardUpdated events
  - [ ] Toast notifications still appear
  - [ ] All 45 existing tests pass

  **QA Scenarios**:

  ```
  Scenario: Web create card in backlog
    Tool: Bash (curl)
    Steps:
      1. curl -X POST http://localhost:7677/api/cards -d "pipeline=investigate&platforms=android&description=web+qa+test&run_now=0"
      2. curl http://localhost:7677/api/cards/board | grep -c "backlog"
    Expected Result: Backlog count increases by 1, card appears in Backlog column
    Evidence: .sisyphus/evidence/task-4-web-backlog.txt

  Scenario: Web create and run
    Tool: Bash (curl)
    Steps:
      1. curl -X POST http://localhost:7677/api/cards -d "pipeline=investigate&platforms=android&description=run+now+test&run_now=1"
      2. sleep 5
      3. curl http://localhost:7677/api/cards/board | grep -c "running\|completed"
    Expected Result: Card appears in Running or Completed column (not Backlog)
    Evidence: .sisyphus/evidence/task-4-web-run-now.txt

  Scenario: Web run backlog card
    Tool: Bash (curl)
    Steps:
      1. Create card in backlog, note card_id
      2. curl -X POST http://localhost:7677/api/cards/{card_id}/run
      3. curl http://localhost:7677/api/cards | python3 -c "import sys,json; d=json.load(sys.stdin); print([c for c in d if c['id']=='{card_id}'][0]['status'])"
    Expected Result: Card status is "running" or "completed" (not "backlog")
    Evidence: .sisyphus/evidence/task-4-web-run.txt

  Scenario: Web cancel kills agent process
    Tool: Bash (curl + ps)
    Steps:
      1. Create and run card, note card_id
      2. curl -X POST http://localhost:7677/api/cards/{card_id}/cancel
      3. ps aux | grep opencode | grep {card_id} || echo "No processes found"
    Expected Result: No opencode processes for card_id
    Evidence: .sisyphus/evidence/task-4-web-cancel.txt
  ```

  **Commit**: YES
  - Message: `refactor(web): use unified operations module`
  - Files: `sisyphus/web/routes.py`

- [x] 5. Wire `cancel_event` through pipeline for graceful cancellation

  **What to do**:
  - In `core/operations.py`, create an `asyncio.Event` per running card
  - Store in a module-level dict: `_cancel_events: dict[str, asyncio.Event] = {}`
  - `cancel_card()` sets the event for that card_id
  - `_run_bg_async()` passes `cancel_event` to `run_pipeline()`
  - `run_pipeline()` already accepts `cancel_event` parameter — it just needs to be checked between steps
  - Add a check in the wave loop: `if cancel_event and cancel_event.is_set(): return {"status": "cancelled", ...}`
  - Clean up event from dict when pipeline completes

  **Must NOT do**:
  - Don't change `run_pipeline()` signature — it already accepts `cancel_event`
  - Don't break existing pipeline behavior for non-cancelled runs

  **Recommended Agent Profile**:
  - **Category**: `deep`
    - Reason: Async coordination between cancel trigger and running pipeline, cleanup logic
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: NO
  - **Parallel Group**: Wave 3 (sequential — depends on Task 4)
  - **Blocks**: Task 6
  - **Blocked By**: Tasks 1, 4

  **References**:
  - `sisyphus/core/pipeline.py:39-44` — `cancel_event` parameter in signature
  - `sisyphus/core/pipeline.py:117` — wave loop where cancel check should go
  - `sisyphus/core/launcher.py` — `cancel_event` already passed to `launch_agent`

  **Acceptance Criteria**:
  - [ ] Calling `cancel_card()` sets the event
  - [ ] Running pipeline detects event and cancels within 5 seconds
  - [ ] Pipeline returns `{"status": "cancelled"}` when cancelled
  - [ ] Event is cleaned up after pipeline completes or is cancelled
  - [ ] Cancelled card shows "failed" status with "cancelled" error

  **QA Scenarios**:

  ```
  Scenario: Cancel mid-pipeline stops execution
    Tool: Bash (python3 REPL)
    Steps:
      1. Create and run card in background (note card_id)
      2. Sleep 2 seconds
      3. Cancel card
      4. Check card status is "failed"
      5. Check run error contains "cancelled"
    Expected Result: Card "failed", run error "cancelled", no more than 1-2 steps executed
    Evidence: .sisyphus/evidence/task-5-cancel-mid.txt
  ```

  **Commit**: YES (groups with Task 4)
  - Message: `feat(ops): wire cancel_event through pipeline`
  - Files: `sisyphus/core/operations.py`, `sisyphus/core/pipeline.py`

- [x] 6. End-to-end verification and regression

  **What to do**:
  - Run full test suite: `python3 -m pytest sisyphus/tests/ -v`
  - Start web server: verify Kanban board loads, cards can be created/run/cancelled
  - Run CLI commands: verify all commands work from terminal
  - Check for regressions in repos, agents, pipelines, history, runtime pages
  - Verify the Backlog workflow: create → appears in Backlog → click Run → moves to Running → completes
  - Verify Create & Run workflow: create with run_now=1 → appears in Running → completes
  - Verify resume: failed card → Resume → step resets → pipeline re-runs
  - Verify cancel: running card → Cancel → processes killed → card marked failed

  **Must NOT do**:
  - Don't skip any existing test
  - Don't merge if any test fails

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: Comprehensive end-to-end verification across both interfaces
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: NO
  - **Parallel Group**: Wave 3 (sequential — final verification)
  - **Blocks**: None
  - **Blocked By**: Tasks 3, 4, 5

  **References**:
  - `sisyphus/tests/` — full test suite
  - `sisyphus/web/templates/` — all templates to verify rendering

  **Acceptance Criteria**:
  - [ ] `python3 -m pytest sisyphus/tests/ -v` → all pass (including new test_operations.py)
  - [ ] Web server starts on http://localhost:7677 without errors
  - [ ] Kanban shows 5 columns: Backlog, Running, Created, Completed, Failed
  - [ ] Creating card via "Add to Backlog" puts it in Backlog column
  - [ ] Creating card via "Create & Run" puts it in Running/Completed
  - [ ] Clicking "▶ Run" on backlog card moves it to Running
  - [ ] Clicking "Resume" on failed card resets and re-runs
  - [ ] Clicking "Cancel" on running card kills processes and marks as failed
  - [ ] CLI `sisyphus run` creates + executes pipeline
  - [ ] CLI `sisyphus resume` resets steps and re-runs
  - [ ] CLI `sisyphus cancel` kills processes
  - [ ] Dashboard, Agents, Pipelines, History, Runtime pages all load
  - [ ] Repos CRUD still works

  **QA Scenarios**: Run all scenarios from Tasks 1-5 again end-to-end.

  **Commit**: YES
  - Message: `test: end-to-end verification after CLI-Web unification`
  - Files: None (verification only)

---

## Final Verification Wave (MANDATORY — after ALL implementation tasks)

- [x] F1. **Plan Compliance Audit** — `oracle`
  Read the plan end-to-end. Verify `core/operations.py` exists and exports all 9 required functions. Verify `sisyphus.py` calls operations (no inline pipeline logic). Verify `routes.py` calls operations (no `_run_bg` or inline step logic). Check all "Must Have" items present. Check all "Must NOT Have" items absent.
  Output: `Must Have [N/N] | Must NOT Have [N/N] | Tasks [N/N] | VERDICT: APPROVE/REJECT`

- [x] F2. **Code Quality Review** — `unspecified-high`
  Run `python3 -m pytest sisyphus/tests/ -q`. Check for: `as any`/`@ts-ignore`, empty catches, console.log in templates, commented-out code, unused imports. Verify no duplicated logic between CLI and Web.
  Output: `Tests [N pass/N fail] | Lint [PASS/FAIL] | Duplication [CLEAN/N issues] | VERDICT`

- [x] F3. **Real Manual QA** — `unspecified-high`
  Start web server, test all card lifecycle flows: create backlog → run → complete, create & run, resume, cancel. Run CLI equivalents. Verify process kill on cancel. Check resources, steps, durations display correctly.
  Output: `Scenarios [N/N pass] | CLI [N/N] | Web [N/N] | VERDICT`

- [x] F4. **Scope Fidelity Check** — `deep`
  Verify no scope creep: only `core/operations.py`, `sisyphus.py`, `routes.py`, and tests modified. No DB schema changes. No template changes. No `run_pipeline` signature changes.
  Output: `Tasks [N/N compliant] | Contamination [CLEAN/N issues] | Unaccounted [CLEAN/N files] | VERDICT`

---

## Commit Strategy

- **1+2**: `feat(ops): create unified operations module with tests` — `sisyphus/core/operations.py`, `sisyphus/tests/test_operations.py`
- **3**: `refactor(cli): use unified operations module` — `sisyphus/sisyphus.py`
- **4+5**: `refactor(web): use unified operations + wire cancel_event` — `sisyphus/web/routes.py`, `sisyphus/core/operations.py`, `sisyphus/core/pipeline.py`
- **6**: `test: end-to-end verification after CLI-Web unification` — no file changes

---

## Success Criteria

### Verification Commands
```bash
# All tests pass
WORKSPACE_ROOT=./sisyphus/workspace python3 -m pytest sisyphus/tests/ -q

# CLI works
cd sisyphus && WORKSPACE_ROOT=./workspace python3 sisyphus.py status

# Web server boots
cd sisyphus && WORKSPACE_ROOT=./workspace python3 -m uvicorn web.app:app --port 7677

# API endpoints respond
curl http://localhost:7677/api/cards/board | grep "backlog"
```

### Final Checklist
- [x] `core/operations.py` exists with all 9 functions
- [x] CLI and Web share identical create/run/resume/cancel behavior
- [x] Pipeline path works on macOS (not hardcoded `/app/`)
- [x] Cancel kills subprocesses (both paths)
- [x] Resume resets step states (both paths)
- [x] All tests pass (45+ existing, new operations tests)
- [x] Backlog workflow intact
- [x] No DB schema changes

