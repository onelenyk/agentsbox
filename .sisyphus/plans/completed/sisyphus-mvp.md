# Sisyphus MVP — Personal AI Agent Orchestration System

## TL;DR

> **Quick Summary**: Build Sisyphus — a single Docker container that orchestrates 8 AI agents through GitLab-based SDLC pipelines. Replaces Multica Cloud with a simpler, more reliable subprocess-based architecture. CLI-only MVP (~32h, ~1,600 lines).
>
> **Deliverables**:
> - Docker container with orchestrator, agents, tools, and scheduler
> - 2 pipelines (jira-sdlc 6-step, investigate 1-step)
> - 8 agents with per-agent model config
> - 3 tools (fetch_jira, describe_image, run_grep)
> - 3 skills (Jira Reader, GitLab Workflow, SDLC Handoff — ported from Multica)
> - CLI: run, status, logs, cancel, resume
>
> **Estimated Effort**: Medium (~32 hours, ~1,600 lines Python)
> **Parallel Execution**: YES — 5 waves, max 5 concurrent tasks
> **Critical Path**: Docker + Config → SQLite → Pipeline Engine → CLI

---

## Context

### Original Request
User wants to replace Multica Cloud with a personal agent orchestration system. Multica provides only Kanban UI + @-mention + cron — 80% of the logic is manually written. Own system gives full control, easier debugging, and independence from cloud dependencies.

### Interview Summary

**Key Discussions**:

1. **Architecture**: Single Docker container, no Docker-in-Docker. Agents are subprocesses (asyncio). Git worktree for parallel isolation. SQLite + filesystem for state. CLI-first, UI later.

2. **Agent output format** (extensively debated): Two files per agent — `result.json` (5 fields: status, verdict, summary, risk, error) + `analysis.md` (free markdown). NO `next` field — orchestrator uses pipeline YAML DAG. JSON is minimal (flat, 5-6 fields) to minimize LLM formatting errors.

3. **Platform resolution**: Set at card creation. Tech Lead can refine (add platforms). Priority: user > Tech Lead > default.

4. **Tools vs Skills**: tools/ = deterministic Python scripts (fetch_jira, describe_image, run_grep). skills/ = agent instructions. Tools run BEFORE agent via pipeline YAML `setup` block.

5. **Loop mechanism**: Reviewer returns `verdict: "changes_requested"` → orchestrator loops back to engineer. Cap at 3 iterations. No agent knows about the loop — orchestrator handles it.

6. **Orchestrator design**: Python for 95% (deterministic DAG walk), AI fallback for 5% (status classification of unclear output). Stateless between steps — state lives in SQLite + filesystem.

7. **Secrets**: `.env` file, not in git. Injected as env vars into agent subprocess.

8. **Scheduler**: APScheduler inside container. External cron NOT used. Container is self-contained.

**Research Findings**:
- Multica daemon is a persistent Go process with 7+ background loops, WebSocket wakeup, and PostgreSQL state. Sisyphus avoids all this complexity by being single-process, single-container.
- Multica agents are ALSO subprocesses (identical model). The complexity is in the distributed coordination, not the agent model.
- Git worktree is reliable for parallel agent isolation (confirmed in Multica's `execenv.Prepare`).

### Metis Review

**Identified Gaps** (addressed in plan):

| Gap | Resolution |
|-----|------------|
| Orchestrator state machine not explicit | Added FSM diagram with explicit transitions: pending→running→completed/failed/timeout/cancelled |
| Resource contention under parallel agents | Snapshot taken at step START. Parallel siblings share parent resources, NOT each other's |
| `result.json` valid values not enumerated | `status`: completed/failed/timeout/cancelled. `verdict`: approved/changes_requested (Reviewer only) |
| Reviewer loop termination not defined | Cap at 3 iterations. After 3 changes_requested → pipeline fails with max_review_loops_exceeded |
| Git worktree lifecycle underspecified | Bare clone per pipeline RUN. Worktrees per STEP. Cleanup after step completes or fails |
| Secret injection mechanism not specified | Env vars per agent subprocess. Scoped per agent via YAML config |
| "Plan mode" scope ambiguous | Clarified: agents can READ and ANALYZE code. They WRITE only to resources/. They NEVER modify repo code. Tools ARE executable |
| Crash recovery not defined | On orchestrator restart: all in_progress steps → error with reason orchestrator_crash. User resumes manually |
| Edge cases: concurrent runs, zombies, disk space | Addressed: per-run bare clones, wait() on all subprocesses, 10MB file cap, `sisyphus cleanup` command |

---

## Work Objectives

### Core Objective
Build a self-contained Docker-based system that orchestrates AI agents through code analysis pipelines. Replace Multica's cloud-dependent architecture with a deterministic, file-based state machine.

### Concrete Deliverables
- `Dockerfile` + `docker-compose.yaml` for one-container deployment
- `sisyphus.py` — CLI entry point (Click/Typer)
- `core/` — pipeline engine, state machine, agent launcher, resource manager, scheduler
- `agents/*.yaml` — 8 agent definitions with per-agent model + opencode config
- `tools/` — 3 deterministic Python scripts
- `skills/` — 3 skill markdown files (ported from Multica)
- `pipelines/` — 2 pipeline YAML definitions
- SQLite schema for cards, runs, resources_index
- Mock agents for integration testing

### Definition of Done
- [ ] `docker compose up` starts container, orchestrator healthcheck passes
- [ ] `sisyphus run jira-sdlc --jira SLN-12345` completes all 6 steps using mock agents
- [ ] `sisyphus run investigate --desc "Find hardcoded colors"` completes 1 step
- [ ] Reviewer loop: 3 changes_requested → pipeline fails with max_review_loops_exceeded
- [ ] Parallel engineers (android + ios) run simultaneously without git conflicts
- [ ] Timeout: agent exceeds 300s → step marked timeout, pipeline proceeds
- [ ] Crash recovery: kill orchestrator mid-pipeline → restart → all in_progress steps marked error
- [ ] Container healthcheck verifies opencode binary availability
- [ ] All acceptance criteria from Metis review pass

### Must Have
- Single Docker container, self-contained
- 8 agents with per-agent model configuration
- 2 pipelines (jira-sdlc, investigate)
- CLI: run, status, logs, cancel, resume
- Git worktree isolation for parallel agents
- SQLite + filesystem state
- Append-only resource model
- Reviewer loop with 3-iteration cap
- Step-level timeouts (configurable, default 300s)
- Container healthcheck
- Orphan worktree cleanup on startup

### Must NOT Have (Guardrails)
- NO web server, HTTP endpoint, REST API, or UI of any kind (v2)
- NO Docker-in-Docker or docker.sock access
- NO persistent agent sessions — agents die after every step
- NO inter-agent communication beyond resources/ directory
- NO dynamic agent discovery or plugin systems
- NO multi-repository per pipeline run (MVP: single repo per run)
- NO real-time push notifications (WebSocket, SSE) — CLI polling only
- NO notification hooks (Slack, Discord, email)
- NO agent-to-agent direct messaging
- NO Jira webhook
- NO execution mode — agents NEVER modify repo code (plan mode only)
- NO auto-pipeline classifier (manual pipeline selection at card creation)
- NO session resumption across runs
- NO submodule or Git-LFS support (fail gracefully with clear error)

---

## Verification Strategy

> **ZERO HUMAN INTERVENTION** — ALL verification is agent-executed. No exceptions.

### Test Decision
- **Infrastructure exists**: YES (pytest can be added to Docker image)
- **Automated tests**: Tests-after (mock agents + integration tests written after core is built)
- **Framework**: pytest
- **Approach**: Mock agents (simple Python scripts that write predetermined result.json) test orchestrator logic. Real opencode agents tested in integration.

### QA Policy
Every task includes agent-executed QA scenarios using mock agents (bash commands in container).

- **Pipeline engine**: Mock agents that write specific result.json files → verify SQLite state transitions
- **Agent launcher**: Subprocess spawn + capture stdout/stderr + timeout + cancel → verify process tree
- **CLI**: Run commands in container → verify output + SQLite state
- **Git worktree**: Create parallel worktrees → verify isolation (modify same file in both, no conflict)

---

## Execution Strategy

### Parallel Execution Waves

> Target: 5-7 tasks per wave. Fewer than 3 per wave (except final) = under-splitting.

```
Wave 1 (Start Immediately — foundation):
├── Task 1: Project scaffolding + config.yaml [quick]
├── Task 2: Dockerfile + docker-compose + healthcheck [quick]
├── Task 3: SQLite schema + state machine [quick]
├── Task 4: Resource model (run_dir, read/write resources) [quick]
└── Task 5: Agents YAML definitions (8 agents) [quick]

Wave 2 (After Wave 1 — core engine, MAX PARALLEL):
├── Task 6: Pipeline YAML validator [quick]
├── Task 7: Orchestrator state machine (FSM) [unspecified-high]
├── Task 8: Prompt templates (Jinja2) [quick]
├── Task 9: Agent launcher (subprocess, timeout, cancel) [unspecified-high]
└── Task 10: Git worktree manager (bare clone, add, remove, cleanup) [unspecified-high]

Wave 3 (After Wave 2 — pipeline + integration):
├── Task 11: Pipeline engine (DAG walker) [unspecified-high]
├── Task 12: Parallel execution (asyncio.gather) [quick]
├── Task 13: Loop mechanism (Reviewer → engineer, 3-iteration cap) [unspecified-high]
├── Task 14: Tools (fetch_jira, describe_image, run_grep) [unspecified-high]
└── Task 15: Skills (3 markdown files, ported from Multica) [writing]

Wave 4 (After Wave 3 — interface):
├── Task 16: CLI: run, status [unspecified-high]
├── Task 17: CLI: logs, cancel, resume, list, cleanup [unspecified-high]
├── Task 18: APScheduler + cron jobs [quick]
└── Task 19: Pipeline YAML definitions (jira-sdlc, investigate) [quick]

Wave 5 (After Wave 4 — quality):
├── Task 20: Mock agent framework [unspecified-high]
├── Task 21: Integration test: jira-sdlc with mock agents [unspecified-high]
└── Task 22: Integration test: investigate + edge cases [unspecified-high]

Wave FINAL (After ALL tasks — verification):
├── F1: Plan Compliance Audit (oracle)
├── F2: Code Quality Review (unspecified-high)
├── F3: Real QA — run all acceptance criteria (unspecified-high)
└── F4: Scope Fidelity Check (deep)
→ Present results → Get explicit user okay
```

**Critical Path**: Task 1 → Task 5 → Task 10 → Task 9 → Task 11 → Task 16 → Task 17
**Parallel Speedup**: ~65% faster than sequential (Waves 2-4 have 5 parallel tasks each)
**Max Concurrent**: 5 (Waves 2, 3, 4)

---

## TODOs

### Wave 1 — Foundation (Tasks 1-5, all parallel, ~6h total)

- [x] 1. Project scaffolding + config.yaml

  **What to do**:
  - Create directory structure: `sisyphus/` with `core/`, `agents/`, `tools/`, `skills/`, `pipelines/`, `workspace/`
  - Create `requirements.txt`: PyYAML, jinja2, click, pydantic, apscheduler, structlog
  - Create `config.yaml` with all settings:
    ```yaml
    workspace_root: /workspace
    default_model: deepseek
    default_timeout: 300
    max_review_loops: 3
    log_level: info
    jira:
      base_url: "${JIRA_BASE_URL}"
      email: "${JIRA_EMAIL}"
      token: "${JIRA_API_TOKEN}"
    gitlab:
      base_url: "${GITLAB_BASE_URL}"
      token: "${GITLAB_TOKEN}"
    openrouter:
      api_key: "${OPENROUTER_API_KEY}"
      base_url: "https://openrouter.ai/api/v1"
    scheduler:
      jobs: []
    ```
  - Create `.env.example` with all required secrets (values empty)
  - Create `.gitignore`: `.env`, `workspace/`, `*.pyc`, `__pycache__/`, `sisyphus.db`
  - Create `README.md` with setup instructions and quick start
  - Create `ARCHITECTURE.md` with high-level design overview

  **Must NOT do**:
  - NO web framework imports (Flask, FastAPI)
  - NO frontend dependencies

  **Recommended Agent Profile**:
  > Select category + skills based on task domain.
  - **Category**: `quick`
  - **Skills**: `[]`

  **Parallelization**:
  - **Parallel Group**: Wave 1 (with Tasks 2, 3, 4, 5)
  - **Blocks**: None — foundation for everything
  - **Blocked By**: None

  **References**:
  - `.sisyphus/drafts/sisyphus-final.md` — final design decisions (config structure, agent format)
  - `MULTICA_SETUP.md:152-162` — infrastructure references for config values

  **Acceptance Criteria**:
  - [ ] `ls sisyphus/` shows: `core/`, `agents/`, `tools/`, `skills/`, `pipelines/`, `workspace/`, `config.yaml`, `requirements.txt`, `.env.example`, `.gitignore`, `README.md`, `ARCHITECTURE.md`
  - [ ] `pip install -r requirements.txt` succeeds in container
  - [ ] `python -c "import yaml; yaml.safe_load(open('config.yaml'))"` parses without error
  - [ ] `.gitignore` covers `.env`, `workspace/`, `*.pyc`, `sisyphus.db`

  **QA Scenarios**:

  ```
  Scenario: Config file parses correctly
    Tool: Bash (docker exec)
    Preconditions: Container running, config.yaml exists
    Steps:
      1. docker exec sisyphus python -c "import yaml; c = yaml.safe_load(open('/app/config.yaml')); print(c['default_timeout'])"
    Expected Result: Output is "300"
    Failure Indicators: yaml.YAMLError, KeyError, or output is not "300"
    Evidence: .sisyphus/evidence/task-1-config-parse.txt

  Scenario: Directory structure is complete
    Tool: Bash (docker exec)
    Preconditions: Container built
    Steps:
      1. docker exec sisyphus ls -d /app/core /app/agents /app/tools /app/skills /app/pipelines /workspace
    Expected Result: All 6 directories listed without errors
    Failure Indicators: "No such file or directory" for any path
    Evidence: .sisyphus/evidence/task-1-dir-structure.txt
  ```

  **Evidence to Capture**:
  - [ ] task-1-config-parse.txt — config.yaml parse output
  - [ ] task-1-dir-structure.txt — ls output of directory structure

  **Commit**: YES
  - Message: `feat(sisyphus): project scaffolding, config, README, ARCHITECTURE`
  - Files: `config.yaml`, `.env.example`, `.gitignore`, `requirements.txt`, `README.md`, `ARCHITECTURE.md`

---

- [x] 2. Dockerfile + docker-compose + healthcheck

  **What to do**:
  - Create `Dockerfile`:
    - Base: `python:3.12-slim`
    - Install system deps: git, curl, ca-certificates
    - Install opencode CLI (or detect from host — document in README)
    - Install glab CLI for GitLab API access
    - Copy requirements.txt, install Python deps
    - Copy all source code
    - Create /workspace directory
    - ENTRYPOINT: `["python", "/app/sisyphus.py"]`
    - HEALTHCHECK: `python -c "import subprocess; subprocess.run(['opencode', '--version'], capture_output=True)"`
  - Create `docker-compose.yaml`:
    - Service: sisyphus
    - Volume: `./workspace:/workspace`
    - Env file: `.env`
    - Restart: unless-stopped
  - Ensure opencode binary is either installed in image OR documented as host dependency mounted via volume

  **Must NOT do**:
  - NO docker.sock mount
  - NO docker-in-docker
  - NO multiple services in compose (single service only)
  - NO port mappings (no web server in MVP)

  **Recommended Agent Profile**:
  - **Category**: `quick`
  - **Skills**: `[]`

  **Parallelization**:
  - **Parallel Group**: Wave 1 (with Tasks 1, 3, 4, 5)
  - **Blocks**: Task 20 (mock agents need container), Task 21 (integration test)
  - **Blocked By**: None

  **References**:
  - `.sisyphus/drafts/sisyphus-final.md` — Docker design decisions
  - `MULTICA_SETUP.md:152-162` — daemon config, binary paths

  **Acceptance Criteria**:
  - [ ] `docker compose build` succeeds
  - [ ] `docker compose up -d` starts container, `docker compose ps` shows "healthy"
  - [ ] `docker exec sisyphus python -c "import yaml, jinja2, click, pydantic"` — all imports succeed
  - [ ] `docker exec sisyphus git --version` — git available
  - [ ] Healthcheck: container shows "healthy" within 30s of startup

  **QA Scenarios**:

  ```
  Scenario: Container builds and starts healthy
    Tool: Bash
    Preconditions: Docker daemon running, .env file with placeholder values
    Steps:
      1. cd sisyphus && docker compose build
      2. docker compose up -d
      3. sleep 5
      4. docker compose ps --format json | python -c "import json,sys; d=json.load(sys.stdin); print(d['Health'])"
    Expected Result: Output is "healthy"
    Failure Indicators: Build failure, container exits, health status is "unhealthy" or "starting" after 30s
    Evidence: .sisyphus/evidence/task-2-healthy.txt

  Scenario: Container has all required CLI tools
    Tool: Bash
    Preconditions: Container running and healthy
    Steps:
      1. docker exec sisyphus which git && echo "git:ok"
      2. docker exec sisyphus which curl && echo "curl:ok"
      3. docker exec sisyphus python -c "import asyncio; print('asyncio:ok')"
    Expected Result: All three tools found: git:ok, curl:ok, asyncio:ok
    Failure Indicators: "which: no git in ..." or ImportError
    Evidence: .sisyphus/evidence/task-2-tools.txt
  ```

  **Evidence to Capture**:
  - [ ] task-2-healthy.txt — health status output
  - [ ] task-2-tools.txt — tool availability check

  **Commit**: YES
  - Message: `feat(sisyphus): Dockerfile, docker-compose, healthcheck`
  - Files: `Dockerfile`, `docker-compose.yaml`

---

- [x] 3. SQLite schema + state machine

  **What to do**:
  - Create `core/db.py` — SQLite initialization and connection management
    - Use WAL mode for concurrent reads
    - Create tables on first connection
  - Create schema with three tables:
    ```sql
    CREATE TABLE cards (
      id TEXT PRIMARY KEY,
      pipeline_name TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'created',
      platforms TEXT DEFAULT '["android"]',
      description TEXT,
      jira_url TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );

    CREATE TABLE runs (
      id TEXT PRIMARY KEY,
      card_id TEXT NOT NULL REFERENCES cards(id),
      status TEXT NOT NULL DEFAULT 'running',
      started_at TEXT NOT NULL,
      finished_at TEXT,
      error TEXT
    );

    CREATE TABLE steps (
      id TEXT PRIMARY KEY,
      run_id TEXT NOT NULL REFERENCES runs(id),
      agent_name TEXT NOT NULL,
      step_order INTEGER NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending',
      started_at TEXT,
      completed_at TEXT,
      result_json TEXT,
      error TEXT
    );

    CREATE TABLE resources (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      run_id TEXT NOT NULL REFERENCES runs(id),
      step_id TEXT NOT NULL REFERENCES steps(id),
      filename TEXT NOT NULL,
      resource_type TEXT NOT NULL DEFAULT 'agent_output',
      created_at TEXT NOT NULL
    );
    ```
  - Create `core/state.py` — CRUD operations:
    - `create_card(pipeline_name, platforms, description)` → card
    - `create_run(card_id)` → run
    - `create_step(run_id, agent_name, step_order)` → step
    - `update_step_status(step_id, status, result_json, error)` — handles transitions
    - `get_card(id)` / `get_run(id)` / `get_steps(run_id)` — queries
    - `list_cards(status_filter)` — for CLI status command
    - `register_resource(run_id, step_id, filename, rtype)` — add to index
  - Create `core/schemas.py` — Pydantic models for result.json validation:
    ```python
    class BaseResult(BaseModel):
        status: Literal["completed", "failed", "timeout", "cancelled"]
        verdict: Optional[Literal["approved", "changes_requested"]] = None
        summary: str
        risk: Optional[Literal["low", "medium", "high"]] = None
        error: Optional[str] = None
    ```
  - Define FSM transitions:
    ```
    pending → running (step started)
    running → completed (agent returned status=completed)
    running → failed (agent returned status=failed)
    running → timeout (agent exceeded step timeout)
    running → cancelled (user cancelled)
    running → error (orchestrator crash on restart)
    ```

  **Must NOT do**:
  - NO ORM (raw SQLite only — keep it simple)
  - NO async DB (aiosqlite) — SQLite is fast enough for single-user
  - NO migration framework — schema is created fresh on first run

  **Recommended Agent Profile**:
  - **Category**: `quick`
  - **Skills**: `[]`

  **Parallelization**:
  - **Parallel Group**: Wave 1 (with Tasks 1, 2, 4, 5)
  - **Blocks**: Task 7 (FSM), Task 11 (pipeline engine), Task 16 (CLI)
  - **Blocked By**: None

  **References**:
  - `.sisyphus/drafts/sisyphus-final.md` — result.json schema, FSM transitions
  - Metis review: explicit state transitions enumeration

  **Acceptance Criteria**:
  - [ ] `python -c "from core.db import init_db; init_db()"` creates `sisyphus.db` with all 4 tables
  - [ ] `python -c "from core.state import create_card; c = create_card('investigate', ['android'], 'test')"` returns card with status 'created'
  - [ ] `python -c "from core.schemas import BaseResult; BaseResult(status='completed', summary='ok')"` validates without error
  - [ ] `python -c "from core.schemas import BaseResult; BaseResult(status='invalid')"` raises ValidationError
  - [ ] Invalid status transition (e.g., completed → running) raises error
  - [ ] WAL mode enabled: `PRAGMA journal_mode` returns "wal"

  **QA Scenarios**:

  ```
  Scenario: Database is created with all tables on first run
    Tool: Bash (docker exec)
    Preconditions: Container running, no sisphus.db exists
    Steps:
      1. docker exec sisyphus python -c "from core.db import init_db; init_db()"
      2. docker exec sisyphus sqlite3 /workspace/sisyphus.db ".tables"
    Expected Result: Output contains "cards runs steps resources" (all 4 tables)
    Failure Indicators: FileNotFoundError, sqlite3.OperationalError, missing tables
    Evidence: .sisyphus/evidence/task-3-schema.txt

  Scenario: Invalid result.json is rejected by Pydantic
    Tool: Bash (docker exec)
    Preconditions: Container running, schemas.py exists
    Steps:
      1. docker exec sisyphus python -c "
    from core.schemas import BaseResult
    try:
        BaseResult(status='invalid_status', summary='test')
        print('NO_ERROR')
    except Exception as e:
        print(f'REJECTED: {type(e).__name__}')
    "
    Expected Result: Output contains "REJECTED: ValidationError"
    Failure Indicators: Output is "NO_ERROR" — schema not validating
    Evidence: .sisyphus/evidence/task-3-validation.txt

  Scenario: Card lifecycle: create → create run → create step → update step → query
    Tool: Bash (docker exec)
    Preconditions: DB initialized
    Steps:
      1. docker exec sisyphus python -c "
    from core.db import init_db; init_db()
    from core.state import create_card, create_run, create_step, update_step_status, get_steps
    card = create_card('investigate', ['android'], 'test card')
    run = create_run(card['id'])
    step = create_step(run['id'], 'android-engineer', 1)
    update_step_status(step['id'], 'completed', '{\"status\":\"completed\",\"summary\":\"ok\"}')
    steps = get_steps(run['id'])
    print(f'card:{card[\"status\"]} run:{run[\"status\"]} step:{steps[0][\"status\"]}')
    "
    Expected Result: Output: "card:created run:running step:completed"
    Failure Indicators: Any exception or incorrect status values
    Evidence: .sisyphus/evidence/task-3-lifecycle.txt
  ```

  **Evidence to Capture**:
  - [ ] task-3-schema.txt — table listing
  - [ ] task-3-validation.txt — Pydantic rejection
  - [ ] task-3-lifecycle.txt — full lifecycle test

  **Commit**: YES
  - Message: `feat(sisyphus): SQLite schema, state machine, Pydantic schemas`
  - Files: `core/db.py`, `core/state.py`, `core/schemas.py`

---

- [x] 4. Resource model

  **What to do**:
  - Create `core/resources.py`:
    - `init_run_dir(run_id)` → creates directory structure:
      ```
      /workspace/runs/{run_id}/
        resources/
        logs/
      ```
    - `save_resource(run_id, step_id, filename, content)` → writes file, registers in SQLite resources table
    - `list_resources(run_id)` → returns all files in resources/ with metadata (from SQLite index)
    - `read_resource(run_id, filename)` → reads and returns file content
    - `get_available_resources(run_id, step_order)` → returns resources from steps with order < current step (snapshot at step start)
      - Excludes resources from same-order steps (parallel siblings can't read each other's output)
    - `save_agent_log(run_id, step_id, content)` → writes to logs/ directory
  - Create resource directory structure convention:
    ```
    /workspace/runs/{run_id}/
      resources/
        01-pm/              ← step order prefix for ordering
          result.json
          analysis.md
        02-assistant/
          result.json
          analysis.md
      logs/
        01-pm.log
        02-assistant.log
    ```

  **Must NOT do**:
  - NO resource deletion or modification (append-only — once written, immutable)
  - NO resource directory scanning (use SQLite index for listing — faster)

  **Recommended Agent Profile**:
  - **Category**: `quick`
  - **Skills**: `[]`

  **Parallelization**:
  - **Parallel Group**: Wave 1 (with Tasks 1, 2, 3, 5)
  - **Blocks**: Task 9 (agent launcher writes output), Task 11 (pipeline reads resources)
  - **Blocked By**: Task 3 (needs resources table in SQLite)

  **References**:
  - `.sisyphus/drafts/sisyphus-final.md` — resource model design, append-only principle
  - Metis review: parallel sibling resource isolation requirement

  **Acceptance Criteria**:
  - [ ] `init_run_dir("test-run")` creates `/workspace/runs/test-run/resources/` and `logs/`
  - [ ] `save_resource("test-run", "step-1", "test.md", "# Hello")` writes file AND registers in SQLite
  - [ ] `list_resources("test-run")` returns list with filename, step_id, type
  - [ ] `get_available_resources("test-run", step_order=3)` returns only resources from steps with order < 3
  - [ ] Two parallel steps (order=2) cannot read each other's resources

  **QA Scenarios**:

  ```
  Scenario: Resource append-only isolation between parallel steps
    Tool: Bash (docker exec)
    Preconditions: DB initialized, run created
    Steps:
      1. docker exec sisyphus python -c "
    from core.resources import init_run_dir, save_resource, get_available_resources
    init_run_dir('r1')
    save_resource('r1', 's1', 'data_a.md', 'content A', step_order=1)
    save_resource('r1', 's2a', 'data_b.md', 'content B', step_order=2)
    save_resource('r1', 's2b', 'data_c.md', 'content C', step_order=2)
    step2a_resources = get_available_resources('r1', step_order=2, current_step_id='s2a')
    step3_resources = get_available_resources('r1', step_order=3)
    print(f'step2a sees: {len(step2a_resources)} files')
    print(f'step3 sees: {len(step3_resources)} files')
    "
    Expected Result:
      step2a sees: 1 files (only data_a.md from step 1)
      step3 sees: 3 files (all from steps 1 and 2)
    Failure Indicators: step2a sees 3 files (parallel sibling isolation broken)
    Evidence: .sisyphus/evidence/task-4-isolation.txt

  Scenario: Resource registration in SQLite index
    Tool: Bash (docker exec)
    Preconditions: Resources saved
    Steps:
      1. docker exec sisyphus sqlite3 /workspace/sisyphus.db "SELECT filename, resource_type FROM resources WHERE run_id='r1'"
    Expected Result: 3 rows: data_a.md, data_b.md, data_c.md with type 'agent_output'
    Failure Indicators: Empty result or missing rows
    Evidence: .sisyphus/evidence/task-4-sqlite-index.txt
  ```

  **Evidence to Capture**:
  - [ ] task-4-isolation.txt — parallel step isolation test
  - [ ] task-4-sqlite-index.txt — SQLite resource index

  **Commit**: YES
  - Message: `feat(sisyphus): resource model with append-only isolation`
  - Files: `core/resources.py`

---

- [x] 5. Agents YAML definitions (8 agents)

  **What to do**:
  - Create `agents/pm.yaml`:
    ```yaml
    name: "PM"
    model: "deepseek"
    role: "Product Manager — coordinates SDLC pipeline"
    access: read
    skills: [jira-reader]
    opencode_config:
      tools: [bash, read]
      max_tokens: 8000
      timeout: 600
    output_fields:
      status: required
      summary: required
      risk: optional
      platforms: optional
    ```
  - Create `agents/pm-assistant.yaml`:
    ```yaml
    name: "PM Assistant"
    model: "deepseek"
    role: "Gathers context from Jira and screenshots"
    access: read
    skills: [jira-reader]
    opencode_config:
      tools: [bash, read]
      max_tokens: 8000
      timeout: 600
    ```
  - Create `agents/tech-lead.yaml`:
    ```yaml
    name: "Tech Lead"
    model: "deepseek"
    role: "Technical analysis — determines affected platforms"
    access: read
    skills: [sdlc-handoff]
    opencode_config:
      tools: [bash, read, grep, glob]
      max_tokens: 12000
      timeout: 900
    output_fields:
      status: required
      summary: required
      risk: optional
      platforms: optional  # ← Tech Lead can override platforms
    ```
  - Create `agents/android-engineer.yaml`, `agents/ios-engineer.yaml`, `agents/kmp-engineer.yaml` (3 files, similar structure):
    ```yaml
    name: "Android Engineer"
    model: "deepseek"
    role: "Android code analysis — creates Fix Plan"
    access: analyze
    skills: [gitlab-workflow, sdlc-handoff]
    opencode_config:
      tools: [bash, read, grep, glob]
      max_tokens: 16000
      timeout: 1800
      idle_timeout: 600
    output_fields:
      status: required
      summary: required
      risk: optional
    ```
  - Create `agents/code-reviewer.yaml`:
    ```yaml
    name: "Code Reviewer"
    model: "deepseek"
    role: "Reviews Fix Plan — approves or requests changes"
    access: read
    skills: [gitlab-workflow, sdlc-handoff]
    opencode_config:
      tools: [bash, read, grep, glob]
      max_tokens: 12000
      timeout: 1200
    output_fields:
      status: required
      verdict: required  # ← Reviewer MUST produce verdict
      summary: required
      risk: optional
    ```
  - Create `agents/qa.yaml`:
    ```yaml
    name: "QA Engineer"
    model: "deepseek"
    role: "Validates Fix Plan completeness and consistency"
    access: read
    skills: [sdlc-handoff]
    opencode_config:
      tools: [bash, read]
      max_tokens: 8000
      timeout: 600
    output_fields:
      status: required
      summary: required
      risk: optional
    ```
  - Create `agents/__init__.py` — `load_agent(name) → dict` loader

  **Must NOT do**:
  - NO hardcoded API keys in agent YAML (use env var references)
  - NO execution mode access for any agent in MVP

  **Recommended Agent Profile**:
  - **Category**: `quick`
  - **Skills**: `[]`

  **Parallelization**:
  - **Parallel Group**: Wave 1 (with Tasks 1, 2, 3, 4)
  - **Blocks**: Task 16 (CLI uses agent configs)
  - **Blocked By**: None

  **References**:
  - `MULTICA_SETUP.md:35-49` — existing agent definitions (roles, models, skills)
  - `.sisyphus/drafts/sisyphus-final.md` — per-agent model config, access levels

  **Acceptance Criteria**:
  - [ ] All 8 YAML files exist in `agents/`
  - [ ] `load_agent("pm")` returns dict with name, model, access, skills
  - [ ] `load_agent("code-reviewer")` has `output_fields.verdict: required`
  - [ ] `load_agent("android-engineer")` has `access: analyze`, NOT `access: edit`
  - [ ] All agents have `model`, `access`, `opencode_config.timeout` defined
  - [ ] Invalid agent name → `load_agent("nonexistent")` raises clear error

  **QA Scenarios**:

  ```
  Scenario: All agent YAMLs parse correctly
    Tool: Bash (docker exec)
    Preconditions: Container running, agents/ populated
    Steps:
      1. docker exec sisyphus python -c "
    from pathlib import Path
    import yaml
    agents_dir = Path('/app/agents')
    for f in sorted(agents_dir.glob('*.yaml')):
        a = yaml.safe_load(f.read_text())
        print(f'{a[\"name\"]}: model={a[\"model\"]} access={a[\"access\"]} skills={len(a.get(\"skills\",[]))}')
    "
    Expected Result: 8 agents listed with model, access, skills count
    Failure Indicators: yaml.YAMLError on any file, KeyError on required fields
    Evidence: .sisyphus/evidence/task-5-agents.txt

  Scenario: Reviewer agent requires verdict field
    Tool: Bash (docker exec)
    Preconditions: Agent YAMLs loaded
    Steps:
      1. docker exec sisyphus python -c "
    from agents import load_agent
    reviewer = load_agent('code-reviewer')
    has_verdict = 'verdict' in reviewer.get('output_fields', {})
    print(f'verdict_required: {has_verdict}')
    "
    Expected Result: "verdict_required: True"
    Failure Indicators: "verdict_required: False" or KeyError
    Evidence: .sisyphus/evidence/task-5-reviewer-verdict.txt
  ```

  **Evidence to Capture**:
  - [ ] task-5-agents.txt — all agents parsed
  - [ ] task-5-reviewer-verdict.txt — verdict field check

  **Commit**: YES
  - Message: `feat(sisyphus): 8 agent YAML definitions with per-agent config`
  - Files: `agents/*.yaml`, `agents/__init__.py`

---

### Wave 2 — Core Engine (Tasks 6-10, all parallel after Wave 1, ~12h total)

- [x] 6. Pipeline YAML validator

  **What to do**:
  - Create `core/pipeline_validator.py` with `validate_pipeline(yaml_path)`
  - Validate at parse time: required fields, valid agent references, no circular deps in DAG, all tools/skills exist, unique step ids
  - Tests: valid pipeline passes, circular dep caught, unknown agent caught, empty steps caught

  **Must NOT do**: NO runtime-only validation, NO silent failures

  **Recommended Agent Profile**: `quick`, skills `[]`
  **Parallelization**: Wave 2 (with 7,8,9,10), Blocks: Task 11, Blocked By: None

  **Acceptance Criteria**:
  - [ ] Valid pipeline → 0 errors. Circular dep → error "circular". Unknown agent → "unknown_agent". Duplicate ids → detected. Empty steps → caught

  **QA Scenarios**:

  ```
  Scenario: Valid pipeline passes, circular dep caught
    Tool: Bash (docker exec)
    Steps:
      1. docker exec sisyphus python -c "
from core.pipeline_validator import validate_pipeline_string
yaml = 'name: t\nsteps:\n  - id: s1\n    agent: android-engineer\n  - id: s2\n    agent: code-reviewer\n    depends_on: s1'
print(f'valid: {len(validate_pipeline_string(yaml))==0}')
cyaml = 'name: t\nsteps:\n  - id: s1\n    agent: a\n    depends_on: s2\n  - id: s2\n    agent: b\n    depends_on: s1'
print(f'circular: {len(validate_pipeline_string(cyaml))>0}')
"
    Expected Result: valid: True, circular: True
    Evidence: .sisyphus/evidence/task-6-validator.txt
  ```

  **Commit**: YES — `feat(sisyphus): pipeline YAML validator` → `core/pipeline_validator.py`

---

- [x] 7. Orchestrator state machine (FSM)

  **What to do**:
  - Create `core/fsm.py` — step transition enforcement: pending→running→completed/failed/timeout/cancelled
  - `transition_step(step_id, new_status, result_json, error)` validates and updates SQLite
  - Empty result_json → auto "failed" with "no_output"
  - Create `core/recovery.py` — `recover_orphaned_steps()` on startup: all "running" → "error" with reason "orchestrator_crash"

  **Must NOT do**: NO auto-retry of crashed steps

  **Recommended Agent Profile**: `unspecified-high`, skills `[]`
  **Parallelization**: Wave 2 (with 6,8,9,10), Blocks: Task 11, Blocked By: Task 3 (SQLite)

  **Acceptance Criteria**:
  - [ ] pending→running: OK. completed→running: rejected. running→cancelled: OK. Terminal step: no further transitions. Crash recovery: running steps → error

  **QA Scenarios**:

  ```
  Scenario: Valid transitions pass, invalid rejected, crash recovery works
    Tool: Bash (docker exec)
    Steps: Launch full lifecycle test via Python in container
    Expected Result: pending→running True, completed→running False, recovery marks running as error
    Evidence: .sisyphus/evidence/task-7-fsm.txt, .sisyphus/evidence/task-7-recovery.txt
  ```

  **Commit**: YES — `feat(sisyphus): FSM with crash recovery` → `core/fsm.py`, `core/recovery.py`

---

- [x] 8. Prompt templates (Jinja2)

  **What to do**:
  - Create `core/templates.py` + `templates/*.j2` (base, pm, engineer, reviewer, qa)
  - `render_prompt(agent_def, card, step, resources)` returns complete prompt
  - Every prompt includes: role, task, access level, output format (result.json + analysis.md), SISYPHUS_OUTPUT_DIR
  - Reviewer prompt explicitly requires `"verdict": "approved"` or `"changes_requested"`
  - Prompt does NOT mention "next" or "who to hand off to"

  **Must NOT do**: NO hardcoded prompts in Python, NO prompt that says "decide what to do"

  **Recommended Agent Profile**: `quick`, skills `[]`
  **Parallelization**: Wave 2 (with 6,7,9,10), Blocks: Task 9, Blocked By: Task 5

  **Acceptance Criteria**:
  - [ ] Every prompt contains "result.json" and "analysis.md" and SISYPHUS_OUTPUT_DIR. Reviewer prompt has "verdict". No prompt has "next"

  **QA Scenarios**:

  ```
  Scenario: Prompts have output format, no "next" references
    Tool: Bash (docker exec)
    Steps: Render prompts for all agent types, verify key strings present/absent
    Expected Result: result.json present in all, next absent in all, verdict present in reviewer
    Evidence: .sisyphus/evidence/task-8-prompts.txt
  ```

  **Commit**: YES — `feat(sisyphus): Jinja2 prompt templates` → `core/templates.py`, `templates/*.j2`

---

- [x] 9. Agent launcher (subprocess, timeout, cancel)

  **What to do**:
  - Create `core/launcher.py` — `async def launch_agent(agent_def, prompt, worktree, output_dir, timeout) → AgentResult`
  - Spawns opencode subprocess with env vars (SISYPHUS_OUTPUT_DIR, secrets from agent config)
  - Captures stdout/stderr, saves to logs/
  - Implements timeout via asyncio.wait_for → SIGTERM → 5s → SIGKILL
  - Implements cancellation via asyncio.Event
  - After exit: reads result.json, validates against Pydantic, accepts/rejects
  - No result.json → "failed" with "no_output". Invalid JSON → "failed" with "schema_validation_failed"
  - 10MB cap on output. Zombie-free: always calls proc.wait()

  **Must NOT do**: NO shell=True. NO infinite waits. NO leaked subprocesses.

  **Recommended Agent Profile**: `unspecified-high`, skills `[]`
  **Parallelization**: Wave 2 (with 6,7,8,10), Blocks: Task 11, 12, Blocked By: Task 8 (prompts), Task 3 (schemas)

  **Acceptance Criteria**:
  - [ ] Timeout kills agent. Cancel terminates subprocess. Valid result.json → completed. Invalid → schema_validation_failed. Exit ≠0 → failed. No zombies

  **QA Scenarios**:

  ```
  Scenario: Timeout kills hung agent within 5s of limit
    Tool: Bash (docker exec)
    Steps: Launch agent with 3s timeout against sleep-30 script, measure elapsed time
    Expected Result: status: timeout, elapsed: 3-4s (not 30s)
    Evidence: .sisyphus/evidence/task-9-timeout.txt, .sisyphus/evidence/task-9-invalid-json.txt
  ```

  **Commit**: YES — `feat(sisyphus): agent launcher with timeout/cancel/validation` → `core/launcher.py`

---

- [x] 10. Git worktree manager

  **What to do**:
  - Create `core/worktree.py`: init_bare_clone, create_worktree, remove_worktree, cleanup_orphans, set_access
  - Bare clone per pipeline run (not shared between runs). Worktree per step.
  - Worktree path: `/workspace/repos/{repo}/worktrees/{run_id}/{step_id}`
  - set_access: read→555, analyze→755, edit→755 (edit not in MVP)
  - Cleanup: normal (after step), orphan (on startup via Task 7), failed (even if step failed)
  - No submodule/LFS support (fail with clear error)

  **Must NOT do**: NO shared bare clones between runs. NO leftover worktrees.

  **Recommended Agent Profile**: `unspecified-high`, skills `[]`
  **Parallelization**: Wave 2 (with 6,7,8,9), Blocks: Task 11, 14, Blocked By: None

  **Acceptance Criteria**:
  - [ ] Create → exists. Remove → gone. Read access → chmod 555. Parallel worktrees: isolated. Orphans: cleaned on startup

  **QA Scenarios**:

  ```
  Scenario: Parallel worktrees isolated, creation/removal works
    Tool: Bash (docker exec)
    Steps: Create two worktrees in parallel, modify same file in each, verify no cross-contamination
    Expected Result: Each worktree has its own content. Remove succeeds. Cleanup removes orphans.
    Evidence: .sisyphus/evidence/task-10-worktree.txt
  ```

  **Commit**: YES — `feat(sisyphus): git worktree manager` → `core/worktree.py`

---

### Wave 3 — Pipeline + Integration (Tasks 11-15, all parallel after Wave 2, ~10h total)

- [x] 11. Pipeline engine (DAG walker)

  **What to do**:
  - Create `core/pipeline.py` — `async def run_pipeline(pipeline_yaml, card)`
  - Validates pipeline YAML (Task 6) before any execution
  - Topological sort of DAG steps based on depends_on
  - For each DAG "wave" (set of steps with resolved dependencies):
    - Creates git worktree if needed (Task 10)
    - Runs setup tools from pipeline YAML `setup` block
    - Collects available resources (Task 4) — snapshot at wave start
    - Launches all ready agents in parallel (Task 12)
    - Waits for all to complete
    - Reads result.json from each, updates FSM (Task 7)
    - If Reviewer returns changes_requested and loop_count < 3 → re-add engineer step (Task 13)
    - If Reviewer returns changes_requested and loop_count >= 3 → pipeline fails
    - If any agent failed/timeout → pipeline fails (stop execution)
    - After each wave: cleanup worktrees for completed/failed steps (Task 10)
  - On completion: mark run as done/failed in SQLite
  - Platform resolution: card platforms → Tech Lead can override in result.json → default ["android"]

  **Must NOT do**:
  - NO skipping failed steps (fail-fast). NO ignoring timeouts.

  **Recommended Agent Profile**: `unspecified-high`, skills `[]`
  **Parallelization**: Wave 3 (with 12,13,14,15), Blocks: Task 16 (CLI), Blocked By: Tasks 7,9,10

  **Acceptance Criteria**:
  - [ ] 6-step jira-sdlc pipeline completes in correct DAG order (verify SQLite step ordering)
  - [ ] Failed step stops pipeline (no further steps executed)
  - [ ] Timeout step → pipeline marked failed
  - [ ] Tools from setup block run before agent
  - [ ] Resources snapshot taken at wave start, not live
  - [ ] Pipeline YAML invalid → rejected before any step runs

  **QA Scenarios**:

  ```
  Scenario: Full pipeline with mock agents completes in correct order
    Tool: Bash (docker exec)
    Steps: Run jira-sdlc pipeline with mock agents that write result.json files
      → docker exec sisyphus python -c "
    import asyncio
    from core.pipeline import run_pipeline
    from core.db import init_db; init_db()
    from core.state import create_card, get_steps
    # mock agents directory with scripts that write result.json
    card = create_card('test-pipeline', ['android'], 'integration test')
    asyncio.run(run_pipeline('pipelines/test-pipeline.yaml', card))
    steps = get_steps(card['id'])
    for s in steps:
        print(f'{s[\"agent_name\"]}: {s[\"status\"]}')
    "
    Expected Result: All steps completed in order (pm, assistant, tech-lead, engineer, reviewer, qa)
    Evidence: .sisyphus/evidence/task-11-pipeline.txt
  ```

  **Commit**: YES — `feat(sisyphus): DAG pipeline engine` → `core/pipeline.py`

---

- [x] 12. Parallel execution (asyncio.gather)

  **What to do**:
  - Extend `core/pipeline.py` with `async def _run_parallel(steps, card)`:
    - Creates asyncio.Task for each step's `launch_agent()` call
    - Uses `asyncio.gather(*tasks, return_exceptions=True)` to run all in parallel
    - Each task independently: creates worktree, renders prompt, launches agent, captures result
    - Collects all results, updates FSM for each
    - If any task raised exception → captured in result, doesn't crash orchestrator
  - Parallel step resource isolation: each step gets snapshot of resources from steps before this wave
    - Parallel siblings (same wave) cannot read each other's output
    - They DO share the same parent resources (steps from previous waves)

  **Must NOT do**: NO sequential fallback for parallel steps. NO shared mutable state between tasks.

  **Recommended Agent Profile**: `quick`, skills `[]`
  **Parallelization**: Wave 3 (with 11,13,14,15), Blocks: None, Blocked By: Task 9 (launcher)

  **Acceptance Criteria**:
  - [ ] 3 parallel agents complete without blocking each other
  - [ ] Parallel agents on same repo use different worktrees → no git conflicts
  - [ ] If one parallel agent fails, others continue (return_exceptions=True)
  - [ ] All parallel results collected before next DAG wave starts

  **QA Scenarios**:

  ```
  Scenario: Three parallel agents run without conflicts
    Tool: Bash (docker exec)
    Steps: Launch 3 mock agents in parallel → verify all 3 completed, each in own worktree
    Expected Result: All 3 status=completed, worktrees differ, git states isolated
    Evidence: .sisyphus/evidence/task-12-parallel.txt
  ```

  **Commit**: YES — `feat(sisyphus): parallel agent execution with asyncio` → `core/pipeline.py` (same file as Task 11)

---

- [x] 13. Loop mechanism (Reviewer → engineer)

  **What to do**:
  - Extend `core/pipeline.py` with loop handling:
    - After Reviewer step completes and verdict == "changes_requested":
      - Increment loop counter for this step
      - If loop_count < max_review_loops (3):
        - Add reviewer's analysis.md as resource for engineer
        - Reset engineer step status to pending
        - Re-run engineer step (same agent, extended context)
        - Then re-run reviewer
      - If loop_count >= max_review_loops:
        - Mark pipeline as failed with error "max_review_loops_exceeded"
        - Stop pipeline execution
    - If verdict == "approved": proceed to next step normally
  - Loop count tracked in SQLite steps table (add `loop_count` column)

  **Must NOT do**: NO infinite loops. NO bypassing the 3-iteration cap.

  **Recommended Agent Profile**: `unspecified-high`, skills `[]`
  **Parallelization**: Wave 3 (with 11,12,14,15), Blocks: None, Blocked By: Task 11 (pipeline engine)

  **Acceptance Criteria**:
  - [ ] Reviewer changes_requested → engineer re-runs (loop 1)
  - [ ] Reviewer changes_requested again → engineer re-runs (loop 2)
  - [ ] Reviewer approved on 3rd review → pipeline continues to QA
  - [ ] Reviewer changes_requested 4 times → pipeline fails with max_review_loops_exceeded
  - [ ] Engineer in loop gets reviewer's previous feedback as resource

  **QA Scenarios**:

  ```
  Scenario: 3 changes_requested then pipeline fails
    Tool: Bash (docker exec)
    Steps: Run pipeline with mock reviewer always returning changes_requested
    Expected Result: Pipeline status "failed", error "max_review_loops_exceeded", 3 re-runs of engineer
    Evidence: .sisyphus/evidence/task-13-loop.txt
  ```

  **Commit**: YES — `feat(sisyphus): reviewer loop with 3-iteration cap` → `core/pipeline.py`

---

- [x] 14. Tools (fetch_jira, describe_image, run_grep)

  **What to do**:
  - Create `tools/fetch_jira.py`:
    - Input: Jira URL (from card or env), output: resources/NN_jira_data.json
    - Uses JIRA_EMAIL + JIRA_API_TOKEN from env
    - Fetches issue fields + linked issues
    - Returns JSON with key, summary, description, status, linked_issues
  - Create `tools/describe_image.py`:
    - Input: image file path(s), output: resources/NN_screenshot_analysis.md
    - Encodes image as base64, sends to OpenRouter Gemini Flash
    - Prompt: "Describe this screenshot. Focus on UI elements, colors, text, and anything that looks incorrect."
    - Returns text description
  - Create `tools/run_grep.py`:
    - Input: pattern + directory, output: resources/NN_grep_results.txt
    - Runs `grep -rn` in worktree, caches result
    - Returns file:line matches
  - Each tool is standalone (can run via CLI: `python tools/fetch_jira.py URL`)
  - Tools write output to stdout (or to specified file)
  - Orchestrator invokes tools via subprocess (same launcher, shorter timeout: 60s)

  **Must NOT do**:
  - NO tool calling another tool (tools are independent)
  - NO tool modifying any file outside its output

  **Recommended Agent Profile**: `unspecified-high`, skills `[]`
  **Parallelization**: Wave 3 (with 11,12,13,15), Blocks: Task 16, Blocked By: None

  **Acceptance Criteria**:
  - [ ] `python tools/fetch_jira.py SLN-12345` outputs valid JSON with fields
  - [ ] `python tools/describe_image.py screenshot.png` outputs text description
  - [ ] `python tools/run_grep.py "BrandManager" /path` outputs matching lines
  - [ ] Tools fail gracefully with clear error if credentials missing

  **QA Scenarios**:

  ```
  Scenario: fetch_jira outputs valid JSON
    Tool: Bash (docker exec)
    Steps: docker exec sisyphus python tools/fetch_jira.py --help (verify tool exists and works)
    Expected Result: Help text or valid JSON output
    Evidence: .sisyphus/evidence/task-14-tools.txt
  ```

  **Commit**: YES — `feat(sisyphus): 3 tools (fetch_jira, describe_image, run_grep)` → `tools/*.py`

---

- [x] 15. Skills (3 markdown files, ported from Multica)

  **What to do**:
  - Create `skills/jira-reader.md` — port from MULTICA_SETUP.md Jira Reader v4
  - Create `skills/gitlab-workflow.md` — port from GitLab Workflow v6 (sync tools only)
  - Create `skills/sdlc-handoff.md` — port from SDLC Handoff v3 (output templates)
  - Adapt: remove @-mention references, replace with "write result.json + analysis.md"
  - Adapt: remove Multica-specific commands (multica issue create, etc.)
  - Adapt: add SISYPHUS_OUTPUT_DIR reference
  - Keep: grep/read/explore workflow, Staging branch checkout, Fix Plan template

  **Must NOT do**: NO new skills (only port existing 3). NO @-mention or Multica references.

  **Recommended Agent Profile**: `writing`, skills `[]`
  **Parallelization**: Wave 3 (with 11,12,13,14), Blocks: Task 19 (pipelines reference skills), Blocked By: None

  **Acceptance Criteria**:
  - [ ] All 3 skill files exist in skills/
  - [ ] No "@mention", "multica", or "multica issue" in any file
  - [ ] All files mention "result.json" and "analysis.md" as output format
  - [ ] GitLab Workflow: "git checkout Staging && git pull" is first step

  **QA Scenarios**:

  ```
  Scenario: Skills contain no Multica references, mention result.json
    Tool: Bash (docker exec)
    Steps: grep for Multica-specific terms across all skill files
    Expected Result: Zero matches for "@mention" and "multica issue". Positive matches for "result.json"
    Evidence: .sisyphus/evidence/task-15-skills.txt
  ```

  **Commit**: YES — `feat(sisyphus): 3 skills ported from Multica` → `skills/*.md`

---

### Wave 4 — Interface (Tasks 16-19, parallel after Wave 3, ~6h total)

- [x] 16. CLI: run, status

  **What to do**:
  - Create `sisyphus.py` — Click CLI entry point
  - `sisyphus run PIPELINE_NAME`:
    - --jira URL | --desc TEXT | --pipeline NAME | --platforms android,ios
    - Reads pipeline YAML, validates
    - Creates card + run in SQLite
    - Starts pipeline execution (async)
    - Prints: "Card {id} created. Pipeline: {name}. Step 1/{N}: {agent}..."
    - Waits for pipeline completion (or runs in background with --detach)
  - `sisyphus status [CARD_ID]`:
    - Without ID: table of all cards with status, pipeline, current step
    - With ID: detailed view — all steps, statuses, durations, summary from result.json
    - --verbose: show resource list for each step
  - `sisyphus list`:
    - --pipeline X — filter by pipeline
    - --status X — filter by status
    - --platform X — filter by platform

  **Must NOT do**: NO web server. NO background daemon (unless --detach flag). NO colorful emoji spam.

  **Recommended Agent Profile**: `unspecified-high`, skills `[]`
  **Parallelization**: Wave 4 (with 17,18,19), Blocks: None, Blocked By: Task 11 (pipeline engine)

  **Acceptance Criteria**:
  - [ ] `sisyphus run investigate --desc "test"` creates card and starts pipeline
  - [ ] `sisyphus status` shows table with columns: Card, Pipeline, Step, Agent, Status
  - [ ] `sisyphus status CARD-001` shows detailed view with per-step info
  - [ ] `sisyphus list --status failed` filters correctly
  - [ ] CLI help text: `sisyphus --help` shows all commands

  **QA Scenarios**:

  ```
  Scenario: Full CLI workflow: run → status → detailed view
    Tool: Bash (docker exec)
    Steps:
      1. docker exec sisyphus sisyphus run investigate --desc "find hardcoded colors test" --platforms android
      2. docker exec sisyphus sisyphus status
      3. docker exec sisyphus sisyphus status <card_id_from_step2>
    Expected Result: Pipeline starts, status shows card, detailed view shows step info
    Evidence: .sisyphus/evidence/task-16-cli.txt
  ```

  **Commit**: YES — `feat(sisyphus): CLI run + status commands` → `sisyphus.py`

---

- [x] 17. CLI: logs, cancel, resume, list, cleanup

  **What to do**:
  - Extend `sisyphus.py`:
  - `sisyphus logs CARD_ID [--step NAME] [--follow]`:
    - Prints agent stdout/stderr from logs/ directory
    - --step: filter by step name
    - --follow: tail -f mode for running steps
  - `sisyphus cancel CARD_ID`:
    - Sets cancel event → agent launcher terminates subprocess
    - Marks step as "cancelled" in FSM
    - Prints: "Cancelled. Step {name} terminated."
  - `sisyphus resume CARD_ID`:
    - Reads SQLite state, finds last failed/timeout step
    - Re-runs from that step (not from beginning)
    - Uses same run directory (resources preserved)
  - `sisyphus cleanup`:
    - Removes worktrees for completed/failed runs
    - Removes run directories older than --keep-days (default 7)
    - Prints summary: "Removed N worktrees, M run directories. Freed X MB."

  **Must NOT do**: NO automatic resume (manual only). NO deleting active runs.

  **Recommended Agent Profile**: `unspecified-high`, skills `[]`
  **Parallelization**: Wave 4 (with 16,18,19), Blocks: None, Blocked By: Task 16 (CLI base)

  **Acceptance Criteria**:
  - [ ] `sisyphus logs CARD_ID` shows agent output
  - [ ] `sisyphus cancel CARD_ID` terminates running agent, marks cancelled
  - [ ] `sisyphus resume CARD_ID` restarts from failed step
  - [ ] `sisyphus cleanup --keep-days 0` removes all old run directories

  **QA Scenarios**:

  ```
  Scenario: Cancel terminates running agent
    Tool: Bash (docker exec)
    Steps: Start long pipeline (sleep mock), cancel it, verify step status = cancelled
    Expected Result: Step status "cancelled", agent process gone
    Evidence: .sisyphus/evidence/task-17-cancel.txt
  ```

  **Commit**: YES — `feat(sisyphus): CLI logs, cancel, resume, cleanup` → `sisyphus.py`

---

- [x] 18. APScheduler + cron jobs

  **What to do**:
  - Add APScheduler to orchestrator startup in `sisyphus.py`:
    - Initialize `AsyncIOScheduler` on startup
    - Load jobs from `config.yaml` scheduler.jobs section
    - Jobs can be: interval (every N minutes), cron (specific times)
  - Default job: "orphan-cleanup" — runs every 60 min, calls `cleanup_orphans()` from Task 10
  - Optionally: "stuck-card-check" — finds cards in "created" status > 1h, logs warning
  - Scheduler runs in same Python process (no separate daemon)

  **Must NOT do**: NO external cron. NO separate scheduler process.

  **Recommended Agent Profile**: `quick`, skills `[]`
  **Parallelization**: Wave 4 (with 16,17,19), Blocks: None, Blocked By: Task 1 (config)

  **Acceptance Criteria**:
  - [ ] APScheduler starts when container starts
  - [ ] Orphan cleanup job runs on schedule (verify via logs)
  - [ ] Adding job to config.yaml → picked up on restart

  **QA Scenarios**:

  ```
  Scenario: Scheduler starts and logs job registration
    Tool: Bash (docker exec)
    Steps: Check container logs for scheduler initialization messages
    Expected Result: Log contains "APScheduler started" and job names
    Evidence: .sisyphus/evidence/task-18-scheduler.txt
  ```

  **Commit**: YES — `feat(sisyphus): APScheduler for cron jobs` → `sisyphus.py`, `config.yaml`

---

- [x] 19. Pipeline YAML definitions (jira-sdlc, investigate)

  **What to do**:
  - Create `pipelines/jira-sdlc.yaml`:
    ```yaml
    name: jira-sdlc
    description: "Full SDLC: Jira → Fix Plan"
    steps:
      - id: pm
        agent: pm
        setup:
          - tool: fetch_jira
        depends_on: []
      - id: assistant
        agent: pm-assistant
        depends_on: pm
      - id: tech-lead
        agent: tech-lead
        depends_on: assistant
      - id: engineer
        agent: android-engineer
        depends_on: tech-lead
      - id: reviewer
        agent: code-reviewer
        depends_on: engineer
      - id: qa
        agent: qa
        depends_on: reviewer
    ```
  - Create `pipelines/investigate.yaml`:
    ```yaml
    name: investigate
    description: "Single-agent code investigation"
    steps:
      - id: engineer
        agent: android-engineer
        depends_on: []
    ```
  - Both validated by pipeline validator (Task 6)

  **Must NOT do**: NO pipeline definitions embedded in code. NO hardcoded agent selection.

  **Recommended Agent Profile**: `quick`, skills `[]`
  **Parallelization**: Wave 4 (with 16,17,18), Blocks: Task 21 (integration test), Blocked By: Task 15 (skills must exist)

  **Acceptance Criteria**:
  - [ ] `pipelines/jira-sdlc.yaml` passes validator
  - [ ] `pipelines/investigate.yaml` passes validator
  - [ ] jira-sdlc has 6 steps with correct depends_on chain
  - [ ] investigate has 1 step with no dependencies

  **QA Scenarios**:

  ```
  Scenario: Both pipelines pass validation
    Tool: Bash (docker exec)
    Steps: docker exec sisyphus python -c "
    from core.pipeline_validator import validate_pipeline
    jira_errs = validate_pipeline('pipelines/jira-sdlc.yaml')
    inv_errs = validate_pipeline('pipelines/investigate.yaml')
    print(f'jira-sdlc errors: {len(jira_errs)}')
    print(f'investigate errors: {len(inv_errs)}')
    "
    Expected Result: Both lines show "errors: 0"
    Evidence: .sisyphus/evidence/task-19-pipelines.txt
  ```

  **Commit**: YES — `feat(sisyphus): pipeline definitions (jira-sdlc, investigate)` → `pipelines/*.yaml`

---

### Wave 5 — Quality (Tasks 20-22, parallel after Wave 4, ~6h total)

- [x] 20. Mock agent framework

  **What to do**:
  - Create `tests/mock_agents/` — simple Python scripts that simulate agents:
    - `mock_pm.py` — writes result.json with status=completed, platforms=["android"]
    - `mock_engineer.py` — writes result.json + analysis.md with findings
    - `mock_reviewer_approved.py` — writes verdict=approved
    - `mock_reviewer_changes.py` — writes verdict=changes_requested
    - `mock_agent_timeout.py` — sleeps 30 seconds
    - `mock_agent_crash.py` — exit(1), no result.json
    - `mock_agent_invalid_json.py` — writes {"garbage": true}
  - Each mock: accepts --output-dir and --status flags
  - Framework: `tests/conftest.py` with fixtures for DB, card, run setup
  - Test runner: `pytest tests/`

  **Must NOT do**: NO real LLM calls in mock tests. NO external API dependencies.

  **Recommended Agent Profile**: `unspecified-high`, skills `[]`
  **Parallelization**: Wave 5 (with 21,22), Blocks: None, Blocked By: Task 9 (needs launcher to be testable)

  **Acceptance Criteria**:
  - [ ] All 7 mock agents exist and accept --output-dir flag
  - [ ] `python tests/mock_agents/mock_engineer.py --output-dir /tmp/test` creates result.json + analysis.md
  - [ ] `pytest tests/` discovers and runs tests (even if they fail due to missing integration)

  **QA Scenarios**:

  ```
  Scenario: Mock agent writes valid output
    Tool: Bash (docker exec)
    Steps: docker exec sisyphus python tests/mock_agents/mock_engineer.py --output-dir /tmp/mock_test
    Expected Result: /tmp/mock_test/result.json exists, json.loads succeeds, status=completed
    Evidence: .sisyphus/evidence/task-20-mock.txt
  ```

  **Commit**: YES — `feat(sisyphus): mock agent framework for testing` → `tests/mock_agents/*.py`, `tests/conftest.py`

---

- [x] 21. Integration test: jira-sdlc with mock agents

  **What to do**:
  - Create `tests/test_jira_sdlc.py`:
    - Override agent launcher to use mock agents instead of real opencode
    - Test: full 6-step pipeline completes, all steps status=completed
    - Test: reviewer approved → pipeline proceeds to QA → done
    - Test: reviewer changes_requested once → engineer re-runs → reviewer approves → QA → done
    - Test: reviewer changes_requested 4 times → pipeline fails with max_review_loops_exceeded
    - Test: engineer timeout → pipeline fails, no further steps
    - Test: parallel engineers (android + ios) — both complete, results merged
    - Verify: SQLite state correct after each test
    - Verify: resource isolation between parallel steps

  **Must NOT do**: NO real opencode calls. NO real network calls.

  **Recommended Agent Profile**: `unspecified-high`, skills `[]`
  **Parallelization**: Wave 5 (with 20,22), Blocks: None, Blocked By: Tasks 11,13,19,20

  **Acceptance Criteria**:
  - [ ] `pytest tests/test_jira_sdlc.py -v` shows all tests pass
  - [ ] Full pipeline test: 6 steps, correct order, all completed
  - [ ] Loop test: 3 re-runs then fail
  - [ ] Timeout test: step timeout → pipeline failed
  - [ ] Parallel test: 2 engineers run, both complete

  **QA Scenarios**:
  - Runs as part of pytest suite — see acceptance criteria above
  - Evidence captured from pytest output

  **Evidence**: .sisyphus/evidence/task-21-integration.txt (pytest output)

  **Commit**: YES — `feat(sisyphus): jira-sdlc integration tests` → `tests/test_jira_sdlc.py`

---

- [x] 22. Integration test: investigate + edge cases

  **What to do**:
  - Create `tests/test_investigate.py`:
    - Test: single-step investigate pipeline completes
    - Test: invalid pipeline YAML → rejected at parse time
    - Test: agent writes no result.json → step failed, error "no_output"
    - Test: agent writes invalid JSON → step failed, error "schema_validation_failed"
    - Test: two concurrent pipeline runs → no state corruption
    - Test: crash recovery → orchestrator restart, running steps → error
    - Test: orphan worktree cleanup on startup
  - Create `tests/test_edge_cases.py`:
    - Test: cancel mid-execution → subprocess killed
    - Test: resume after failed step → continues from that step
    - Test: 10MB output cap → truncated, warning logged

  **Must NOT do**: NO real network calls. NO database corruption allowed.

  **Recommended Agent Profile**: `unspecified-high`, skills `[]`
  **Parallelization**: Wave 5 (with 20,21), Blocks: None, Blocked By: Tasks 7,9,12,17

  **Acceptance Criteria**:
  - [ ] `pytest tests/test_investigate.py tests/test_edge_cases.py -v` — all pass
  - [ ] Invalid YAML test: rejected before execution
  - [ ] No-output test: step failed, error recorded
  - [ ] Concurrent runs test: both complete without corruption
  - [ ] Crash recovery test: running steps → error on restart
  - [ ] Cancel test: subprocess terminated, status cancelled
  - [ ] Resume test: pipeline continues from last failed step

  **QA Scenarios**:
  - Runs as part of pytest suite
  - Evidence captured from pytest output

  **Evidence**: .sisyphus/evidence/task-22-edge-cases.txt (pytest output)

  **Commit**: YES — `feat(sisyphus): investigate + edge case tests` → `tests/test_investigate.py`, `tests/test_edge_cases.py`

---

## Final Verification Wave (MANDATORY — after ALL implementation tasks)

> 4 review agents run in PARALLEL. ALL must APPROVE. Present consolidated results to user and get explicit "okay" before completing.
> **Do NOT auto-proceed after verification. Wait for user's explicit approval.**

- [x] F1. **Plan Compliance Audit** — `oracle`
  Read the plan end-to-end. For each "Must Have": verify implementation exists. For each "Must NOT Have": search codebase for forbidden patterns — reject with file:line if found. Check evidence files exist in `.sisyphus/evidence/`. Compare deliverables against plan.
  Output: `Must Have [N/N] | Must NOT Have [N/N] | Tasks [22/22] | VERDICT: APPROVE/REJECT`

- [x] F2. **Code Quality Review** — `unspecified-high`
  Run `python -m pytest tests/ -v`. Review all Python files for: bare excepts, print() in production code, commented-out code, unused imports. Check AI slop: excessive comments, over-abstraction, generic names.
  Output: `Tests [N pass/N fail] | Lint [PASS/FAIL] | Files [N clean/N issues] | VERDICT`

- [x] F3. **Real Manual QA** — `unspecified-high`
  Start from clean container. Execute EVERY QA scenario from EVERY task. Test cross-task integration. Test edge cases: concurrent runs, timeout, cancel, crash recovery.
  Output: `Scenarios [N/N pass] | Integration [N/N] | Edge Cases [N tested] | VERDICT`

- [x] F4. **Scope Fidelity Check** — `deep`
  For each task: read "What to do", compare with actual implementation. Verify 1:1 — everything in spec was built, nothing beyond spec was built. Check "Must NOT do" compliance. Detect cross-task contamination. Flag unaccounted changes.
  Output: `Tasks [22/22 compliant] | Contamination [CLEAN/N issues] | Unaccounted [CLEAN/N files] | VERDICT`

---

## Commit Strategy

| Wave | Tasks | Commit Message Pattern |
|------|-------|----------------------|
| 1 | 1 | `feat(sisyphus): project scaffolding, config, README, ARCHITECTURE` |
| 1 | 2 | `feat(sisyphus): Dockerfile, docker-compose, healthcheck` |
| 1 | 3 | `feat(sisyphus): SQLite schema, state machine, Pydantic schemas` |
| 1 | 4 | `feat(sisyphus): resource model with append-only isolation` |
| 1 | 5 | `feat(sisyphus): 8 agent YAML definitions with per-agent config` |
| 2 | 6-10 | Individual commits per task (see task commit messages) |
| 3 | 11-15 | Individual commits per task |
| 4 | 16-19 | Individual commits per task |
| 5 | 20-22 | Individual commits per task |

---

## Success Criteria

### Verification Commands
```bash
# Container health
docker compose ps  # Expected: "healthy"

# CLI help
docker exec sisyphus sisyphus --help  # Expected: run, status, logs, cancel, resume, list, cleanup

# Run pipeline
docker exec sisyphus sisyphus run investigate --desc "test" --platforms android
# Expected: "Card {id} created. Pipeline: investigate. Step 1/1: android-engineer..."

# Check status
docker exec sisyphus sisyphus status
# Expected: table with card, pipeline, step, agent, status

# Run tests
docker exec sisyphus python -m pytest tests/ -v
# Expected: all tests pass (22+ test functions)
```

### Final Checklist
- [ ] All 22 tasks implemented
- [ ] Container starts healthy
- [ ] CLI all commands work
- [ ] jira-sdlc pipeline completes with mock agents
- [ ] investigate pipeline completes with mock agents
- [ ] Reviewer loop: 3 changes_requested → fails with max_review_loops_exceeded
- [ ] Timeout: agent killed, pipeline fails
- [ ] Parallel agents: run without git conflicts
- [ ] Crash recovery: running steps → error on restart
- [ ] All Must NOT Have absent (no web server, no Docker-in-Docker, no execution mode)
- [ ] All Must Have present (8 agents, 2 pipelines, CLI, SQLite, worktree, resources)
- [ ] All tests pass
- [ ] All evidence files in `.sisyphus/evidence/`
