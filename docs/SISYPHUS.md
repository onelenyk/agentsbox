# Sisyphus — Personal AI Agent Orchestration System

> Built in one session. July 3-4, 2026. ~50 tasks, ~40 files, ~3,000 lines of code.

---

## Table of Contents

1. [What We Built](#what-we-built)
2. [Architecture Decisions](#architecture-decisions)
3. [How It Works](#how-it-works)
4. [Data Flow](#data-flow)
5. [Problems We Avoided](#problems-we-avoided)
6. [Technical Details](#technical-details)
7. [How to Run](#how-to-run)
8. [How to Extend](#how-to-extend)
9. [Known Limitations](#known-limitations)

---

## What We Built

Sisyphus is a personal AI agent orchestration system that replaces Multica Cloud. It runs as a Python process (optionally in Docker), spawning opencode agents as subprocesses within isolated git worktrees.

### Core Features

| Layer | What | Files |
|-------|------|-------|
| **Orchestration Engine** | DAG pipeline walker, FSM, parallel execution, reviewer loop | `core/pipeline.py`, `core/fsm.py` |
| **State Management** | SQLite for metadata, filesystem for content | `core/db.py`, `core/state.py` |
| **Agent Lifecycle** | Subprocess spawn → timeout → cancel → result parsing | `core/launcher.py` |
| **Git Isolation** | Bare clone + worktree per step (parallel agents) | `core/worktree.py` |
| **Resource Model** | Append-only resources, step-order snapshot isolation | `core/resources.py` |
| **Web UI** | FastAPI + HTMX + Mermaid.js, dark theme | `web/` |
| **Git Repos** | CRUD management, auto-resolve by platform | `core/repos.py` |
| **Tools** | fetch_jira, describe_image (Gemini Flash), run_grep | `tools/` |
| **Skills** | Jira Reader, GitLab Workflow, SDLC Handoff | `skills/` |
| **Agents** | 8 agent definitions with per-model config | `agents/` |
| **Pipelines** | jira-sdlc (6 steps), investigate (1 step) | `pipelines/` |

### UI Pages (7)

| Page | URL | Content |
|------|-----|---------|
| Kanban | `/kanban` | 4-column board, auto-refresh 5s, card detail panel |
| Dashboard | `/dashboard` | Stats (total, success rate, active, failed) |
| Agents | `/agents` | 8 agents with YAML config viewer |
| Runtime | `/runtime` | config.yaml + scheduler jobs |
| Pipelines | `/pipelines` | Pipeline YAML list with step count |
| History | `/history` | Runs table with status filter |
| Repos | `/repos` | CRUD for git repositories |

---

## Architecture Decisions

### Decision 1: Python over Go

**Why Python:**
- opencode subprocess management: `asyncio.create_subprocess_exec` — 5 lines. Go needs 25+.
- SQLite: `import sqlite3` in stdlib. Go needs CGO.
- CLI: Click decorators. Go needs Cobra boilerplate.
- Templates: Jinja2 for prompts. Go has `text/template` (less flexible).
- Pydantic: JSON schema validation. Go has manual validation.

**Trade-off:** Python is slower to execute but faster to write. Sisyphus is 90% coordination, 10% computation — Python is the right choice.

### Decision 2: FastAPI + HTMX over Next.js

**Why not Next.js:**
- Two processes (Python API + Node.js frontend) → complexity
- Webpack/build step → slower iteration
- The user doesn't need SPA features (auth, routing, state management)

**Why FastAPI + HTMX:**
- One Python process → simpler deployment
- HTMX attributes (`hx-get`, `hx-target`) replace JavaScript
- Zero build step — edit template → refresh → done
- Dark theme CSS inline → no CSS framework dependency
- Mermaid.js via CDN for pipeline visualization

**Trade-off:** Less "modern" look, but faster development. For a single-user system, this is ideal.

### Decision 3: Git Worktree over Docker-in-Docker

**Why not Docker-in-Docker:**
- Requires docker.sock mount → security risk
- Container-per-agent overhead → slow
- Multi-container orchestration → complex

**Why Git Worktree:**
- Bare clone once, worktree per step → fast (<1 second per worktree)
- Filesystem isolation → agents can't interfere with each other
- Works on any OS → no Docker required
- Cleanup is simple: `git worktree remove` or `rm -rf`

### Decision 4: SQLite over PostgreSQL

**Why SQLite:**
- Zero setup: `import sqlite3` → done
- Embedded in process → no separate server
- WAL mode → concurrent reads
- Single file → backup = `cp sisyphus.db backup.db`
- Sufficient for single-user system (<100K records)

**Why not PostgreSQL:**
- Requires separate process → more infrastructure
- Overkill for one user
- Multi-container setup needed

### Decision 5: File-based Resources over Pure DB

**Why filesystem for content:**
- Agent outputs are markdown (2-20KB) → filesystem is natural
- Agents READ files directly → no DB query overhead
- Git-friendly: `git log runs/SLN-12345/` → full history
- Debug-friendly: `cat resources/05_analysis.md` → instant

**Why SQLite for metadata:**
- Cards, runs, steps, resources_index → structured queries
- `SELECT * FROM cards WHERE status='running'` → instant
- Filters, sorting, counting → SQL is the right tool

**The hybrid model:** SQLite indexes point to filesystem content. Best of both worlds.

### Decision 6: Minimal JSON (not YAML) for Agent Output

**The problem:** LLMs are non-deterministic. Complex output formats (YAML, nested JSON) fail often.

**The solution:** 5-field flat JSON:
```json
{
    "status": "completed",     // completed | failed | timeout | cancelled
    "verdict": null,            // approved | changes_requested (Reviewer only)
    "summary": "one sentence",  // for card preview
    "risk": "low",             // low | medium | high
    "error": null               // if failed, why
}
```

**Why this works:**
- 5 fields, flat structure → LLM rarely breaks it
- `json.loads()` → one line
- Pydantic validation → schema enforcement
- If agent fails to produce JSON → auto-generate from exit code + stdout

### Decision 7: Append-only Resources with Snapshot Isolation

**The problem:** Parallel agents reading each other's output creates race conditions.

**The solution:**
- Each agent writes to `resources/{step_id}/` (never reads from same-order steps)
- Resources indexed in SQLite with `step_order` column
- `get_available_resources(run_id, step_order, exclude_step_id)` returns snapshot at step start
- Parallel siblings (same step_order) cannot read each other's output

**Result:** Deterministic, race-free agent execution.

### Decision 8: Platform-to-Repo Mapping

**The problem:** Pipeline needs a git repository URL, but the user shouldn't type it every time.

**The solution:**
- `config.yaml` → `repos:` with `platform: android` field
- `core/repos.py` → `get_repo_by_platform("android")` → returns repo config
- Pipeline: `platform=android` → resolves repo → creates worktree → agent runs
- User: just selects platform in the form, system does the rest

**This also means:** local repos work identically to remote repos. `source: /Users/lenyk/work/...` → `git clone --bare` → same as `git@gitlab.com:...`.

---

## How It Works

### Pipeline Execution (end-to-end)

```
1. CREATE CARD
   curl -X POST /api/cards -d "pipeline=investigate&platforms=android&description=Find colors"
   → card created in SQLite, status="created"
   → run created, status="running"
   → background task: asyncio.create_task(_run_bg(pipeline, card))

2. RESOLVE REPO
   platform="android" → get_repo_by_platform("android") → {name:"android-driver", source:"/Users/...", branch:"Staging"}
   → git clone --bare /Users/.../android-driver /workspace/repos/android-driver/

3. LOAD PIPELINE YAML
   pipelines/investigate.yaml → 1 step: {id: engineer, agent: android-engineer}
   → validate_pipeline() → 0 errors

4. TOPOLOGICAL SORT
   _topological_sort(steps) → [[{engineer}]] (1 wave)

5. CREATE WORKTREE
   git -C /workspace/repos/android-driver/ worktree add worktrees/run-xxx/ Staging
   → isolated copy of code for agent

6. RENDER PROMPT
   Jinja2 template with: agent role, access level, task description, resources, output format
   → "You are Android Engineer. Find hardcoded colors. Save result.json + analysis.md."

7. LAUNCH AGENT
   asyncio.create_subprocess_exec("opencode", "-p", prompt, "-c", worktree_path)
   → opencode starts, runs grep/read, analyzes code

8. CAPTURE OUTPUT
   opencode writes to stdout → launcher reads stdout, saves as analysis.md
   → auto-generates result.json from exit code + stdout

9. UPDATE STATE
   transition_step(step_id, "completed") → FSM validates transition
   → update_card_status("completed") → card visible on Kanban

10. CLEANUP
    git worktree remove worktrees/run-xxx/
    → disk freed, ready for next run
```

### Parallel Execution

```
Wave 0: [pm]
Wave 1: [assistant]        ← depends on pm
Wave 2: [tech-lead]        ← depends on assistant
Wave 3: [android-engineer, ios-engineer, kmp-engineer]  ← all parallel!
Wave 4: [reviewer]         ← waits for ALL engineers
Wave 5: [qa]               ← depends on reviewer
```

`asyncio.gather(*tasks, return_exceptions=True)` runs all Wave 3 agents simultaneously. Each gets its own worktree. No conflicts.

### Reviewer Loop

```
Engineer → Fix Plan → Reviewer
                         │
                    ┌────┴────┐
                    │         │
                APPROVED   CHANGES_REQUESTED
                    │         │
                    ▼         ▼
                   QA     Engineer re-runs (max 3 times)
                              │
                              ▼
                          Reviewer again
                              │
                    4th CHANGES_REQUESTED → pipeline FAILED
```

Loop count tracked in `loop_tracker` dict. Reviewer feedback saved as resource for engineer's re-run.

---

## Data Flow

### Card Lifecycle

```
created → running → completed
                 → failed
                 → cancelled
```

State transitions enforced by `core/fsm.py`:
```python
ALLOWED = {
    "pending": ["running"],
    "running": ["completed", "failed", "timeout", "cancelled", "error"],
}
```

### Resource Model

```
/workspace/runs/{run_id}/
├── resources/
│   ├── {step_id}/
│   │   ├── result.json     ← machine-readable (5 fields)
│   │   └── analysis.md     ← human-readable markdown
│   └── ...
└── logs/
    └── {step_id}.log        ← agent stdout/stderr
```

### DB Schema

```sql
cards (id, pipeline_name, status, platforms, description, jira_url, created_at, updated_at)
runs  (id, card_id, status, loop_count, started_at, finished_at, error)
steps (id, run_id, agent_name, step_order, status, loop_count, started_at, completed_at, result_json, error)
resources (id, run_id, step_id, filename, resource_type, step_order, created_at)
```

---

## Problems We Avoided

### 1. Multica's Distributed Complexity

**Multica's approach:** PostgreSQL → daemon (Go) → WebSocket hub → agent CLI (opencode). Three processes, HTTP/WS communication, polling, heartbeat, empty-claim cache, orphan recovery.

**Sisyphus's approach:** One Python process. Direct subprocess spawning. File-based state. No network coordination.

**Why:** Multica is a multi-tenant SaaS. Sisyphus is a single-user tool. Distributed architecture is unnecessary overhead for a single machine.

### 2. Docker-in-Docker Complexity

**What others do:** Each agent runs in its own Docker container. Orchestrator talks to docker.sock. Container-per-step overhead.

**What we do:** Git worktree isolation. No Docker required (though optional). Sub-second worktree creation vs seconds-per-container.

### 3. LLM Output Parsing Fragility

**The problem:** LLMs produce invalid JSON/YAML 5-15% of the time. Retry loops, repair libraries, complex fallback logic.

**Our solution:**
- 5-field flat JSON (LLM rarely breaks it)
- Auto-generation from stdout if JSON missing
- Pydantic validation with clear error messages
- Prompt explicitly tells agent: "If you cannot complete, set status='failed'"

### 4. Platform Configuration Hell

**The problem:** Each pipeline run needs a git repo URL. Users forget, typo, or use wrong branch.

**Our solution:** `config.yaml` → `repos:` → platform-to-repo mapping. User selects platform, system resolves repo. Done once, used forever.

### 5. HTMX targetError (Modal Not Opening)

**The problem:** `hx-target="#modal-container"` fails silently because the target div is outside the HTMX-loaded content.

**Our solution:** Changed to `hx-target="body" hx-swap="beforeend"` for all modal triggers. Added HX-Trigger events for list refresh. Toast notifications for user feedback.

### 6. Raw JSON in UI

**The problem:** API endpoints returning `{"status":"ok"}` appear as raw text on the page.

**Our solution:** All state-changing endpoints now return empty HTTP 200 with `HX-Trigger` header. Client-side listeners show toast notifications. Zero raw JSON visible.

---

## Technical Details

### FSM (Finite State Machine)

**Two-layer validation:**
- `core/state.py`: low-level validation. Rejects invalid transitions with ValueError.
- `core/fsm.py`: high-level wrapper. Returns bool instead of raising. Adds business logic (empty output → auto-fail).

**Crash recovery:**
- `core/recovery.py`: on startup, marks all "running" steps as "error" with reason "orchestrator_crash"
- User must manually resume (via CLI or UI)

### SQLite Connection Strategy

**Evolution:**
1. v1: `threading.local()` shared connection — broken under asyncio
2. v2: connection-per-function with explicit `.close()` — works but risky on exceptions
3. v3: connection-per-function with `try/finally` — safe, current implementation

**Key PRAGMAs:**
- `journal_mode=WAL` — concurrent reads
- `foreign_keys=ON` — referential integrity
- `busy_timeout=5000` — wait up to 5s for locks

### opencode Integration

**Environment variables passed to subprocess:**
- `SISYPHUS_OUTPUT_DIR` — where to write result.json + analysis.md
- `SISYPHUS_WORKTREE` — working directory path
- Agent-specific env from `opencode_config.env` in agent YAML

**Launcher flow:**
1. `asyncio.create_subprocess_exec("opencode", "-p", prompt, "-c", worktree_path)`
2. `asyncio.wait_for(proc.communicate(), timeout=timeout)` → stdout/stderr
3. Read `result.json` from output_dir (if agent wrote it)
4. If missing: auto-generate from stdout + exit code
5. Save analysis.md from stdout
6. Return `AgentResult` dataclass

**Timeout:**
- SIGTERM → 5s grace → SIGKILL
- AgentResult.status = "timeout"

**Cancel (via UI):**
- `transition_step(step_id, "cancelled", error="user_cancelled")`
- Client-side `pgrep -f opencode` + `kill -SIGTERM`

### HTMX Patterns

| Pattern | Usage |
|---------|-------|
| `hx-get` + `hx-trigger="every 5s"` | Board auto-refresh |
| `hx-get` + `hx-target="#detail-panel"` | Card detail on click |
| `hx-post` + `hx-swap="none"` | Cancel/Resume (silent) |
| `hx-target="body" hx-swap="beforeend"` | Modal forms |
| `HX-Trigger` response header | List refresh after CRUD |
| `hx-confirm` | Delete confirmation dialog |
| `hx-indicator` | Loading spinner |

### Mermaid Pipeline Graphs

Generated dynamically from pipeline YAML steps:
```
graph LR
    pm["PM"] --> assistant["PM Assistant"]
    assistant --> tech_lead["Tech Lead"]
    tech_lead --> engineer["Android Engineer"]
    engineer --> reviewer["Code Reviewer"]
    reviewer --> qa["QA Engineer"]
    
    style pm fill:#1b3a2d,stroke:#3fb950
    style engineer fill:#1f3a5f,stroke:#58a6ff
```

Node colors by status: completed=green, running=blue, pending=gray, failed=red.

---

## How to Run

### Local (no Docker)

```bash
cd sisyphus
WORKSPACE_ROOT=/tmp/sisyphus-workspace python3 -m web.app
# → http://localhost:PORT/kanban
```

### Docker (when opencode Linux binary available)

```bash
cd sisyphus
docker compose up -d
# → http://localhost:6199/kanban
```

### CLI (no UI)

```bash
cd sisyphus
WORKSPACE_ROOT=/tmp/sisyphus-workspace python3 sisyphus.py run investigate \
  --desc "Find hardcoded colors" --platforms android
```

### Tests

```bash
cd sisyphus
python3 -m pytest tests/ -v
# → 45 passed
```

---

## How to Extend

### Adding a new agent

1. Create `agents/my-agent.yaml`
2. Add to web UI: auto-discovered via `agents/__init__.py` → `list_agents()`

### Adding a new pipeline

1. Create `pipelines/my-pipeline.yaml` with steps
2. Add to web UI: auto-discovered via `pipelines/` glob
3. Run: `sisyphus run my-pipeline --desc "..."`

### Adding a new UI page

1. `web/templates/my-page.html` (extends base.html)
2. `web/routes.py` → `@router.get("/my-page")`
3. `web/templates/base.html` → add nav link

### Adding a new tool

1. `tools/my-tool.py` (standalone script, stdout output)
2. Reference in pipeline YAML `setup` block

### Adding execution mode (future)

1. Agent YAML: `access: edit`
2. worktree: `chmod 755`
3. agent commits: `git add && git commit -m "fix: ..."`
4. push: `glab mr create`

---

## Known Limitations

| Limitation | Reason | Fix |
|------------|--------|-----|
| No Docker (opencode) | macOS Mach-O binary, no Linux release | Wait for opencode Linux ARM64 build |
| No execution mode | Safety: plan mode only | Add in v3 |
| No WebSocket/SSE | HTMX polling is simpler | Add in v3 |
| No auth | Single-user tool | Not needed |
| No email/Slack notifications | Over-engineering for MVP | Add via webhooks in v3 |
| No session resumption | Agent dies after each step | Add in v3 for reviewer loop |
| No auto-pipeline classifier | Manual selection | Add AI-based classifier in v3 |
| Tables require thead/tbody | CSS specificity issue | All table templates now use thead/tbody |
| sdlc-handoff.md was in root | Subagent file placement error | Moved to skills/ |

---

## File Map

```
sisyphus/
│
├── core/                     ← Business logic (never imports web/)
│   ├── db.py                 SQLite connection + schema
│   ├── state.py              CRUD + FSM low-level
│   ├── fsm.py                FSM high-level + business rules
│   ├── recovery.py           Crash recovery
│   ├── schemas.py            Pydantic models
│   ├── resources.py          Resource model (append-only)
│   ├── templates.py          Jinja2 prompt rendering
│   ├── launcher.py           Agent subprocess + timeout
│   ├── worktree.py           Git worktree manager
│   ├── pipeline.py           DAG walker + parallel + loop
│   ├── pipeline_validator.py YAML validation
│   ├── config.py             config.yaml loader
│   ├── repos.py              Git repo management
│   └── scheduler.py          APScheduler integration
│
├── web/                      ← UI (imports core/)
│   ├── app.py                FastAPI entry
│   ├── routes.py             All endpoints
│   └── templates/            HTMX HTML templates
│       ├── base.html             Layout, CSS, CDNs
│       ├── kanban.html           Board page
│       ├── board_content.html    4-column grid
│       ├── card_detail.html      Slide-in detail panel
│       ├── new_card.html         Creation form
│       ├── dashboard.html        Stats
│       ├── agents.html           Agent list
│       ├── runtime.html          Config + scheduler
│       ├── pipelines.html        Pipeline list
│       ├── history.html          Runs table
│       ├── repos.html            Repos page
│       ├── repos_list.html       Repos list partial
│       ├── repos_new.html        Add repo form
│       └── repos_edit.html       Edit repo form
│
├── agents/                   ← 8 agent YAML definitions
├── pipelines/                ← jira-sdlc.yaml, investigate.yaml
├── tools/                    ← fetch_jira, describe_image, run_grep
├── skills/                   ← jira-reader, gitlab-workflow, sdlc-handoff
├── templates/                ← Jinja2 prompt templates (.j2)
├── tests/                    ← 45 tests + 7 mock agents
├── config.yaml               ← System configuration
├── sisyphus.py               ← CLI entry point
├── requirements.txt          ← Python dependencies
├── Dockerfile                ← Container build
└── docker-compose.yaml       ← Container orchestration
```
