# Sisyphus v2 — Web UI (FastAPI + HTMX + Mermaid.js)

## TL;DR

> **Quick Summary**: Add a full web UI to Sisyphus — Kanban board, agent management, runtime settings, pipeline viewer, and dashboard. Single Python process, zero JS build step.
>
> **Deliverables**:
> - FastAPI server on port 6199
> - Kanban board (4 columns) + card detail panel
> - Agents page (CRUD — view/edit agent YAML configs)
> - Runtime settings page (opencode config, API keys, scheduler)
> - Pipeline viewer (list + detail with Mermaid graph)
> - Run history page (past runs with filters)
> - Dashboard (quick stats)
> - New card creation form
> - HTMX auto-refresh (5s polling)
>
> **Estimated Effort**: ~18 hours (10 tasks)
> **Parallel Execution**: YES — 2 waves, max 4 concurrent
> **Critical Path**: Server → Board → Detail → Agents → Runtime

---

## Context

### What Exists
- Full CLI system with 8 commands
- SQLite database with cards, runs, steps, resources
- Pipeline engine (DAG walker, parallel execution, reviewer loop)
- 8 agents (YAML), 3 tools, 3 skills, 2 pipelines
- Core modules in `sisyphus/core/`

### What's New
- `sisyphus/web/` — FastAPI + HTML templates
- `requirements.txt` — add `fastapi`, `uvicorn` (Jinja2 already exists)

---

## Work Objectives

### Core Objective
Add a web UI making Sisyphus usable without CLI. Manage cards, agents, runtime settings, and view pipeline progress — all from a browser.

### Navigation Structure
```
┌──────────────────────────────────────────────┐
│  🏠 Dashboard  📋 Kanban  🤖 Agents  ⚙️ Runtime  🔄 Pipelines  📜 History │
└──────────────────────────────────────────────┘
```

### Must Have
- Kanban board (Created / In Progress / Review / Done)
- Card detail panel (steps, resources, logs, Mermaid graph)
- New card creation form (pipeline + platforms + description)
- Agents page (list + view YAML config)
- Runtime settings page (view/edit config.yaml)
- Cancel / Resume buttons
- Auto-refresh (5s polling)

### Nice to Have
- Dashboard with stats (cards today, success rate, active agents)
- Run history page (filterable list)
- Pipeline viewer (list + Mermaid preview)
- Tool/Skill list page (read-only)

### Must NOT Have
- NO authentication
- NO WebSocket (HTMX polling)
- NO user preferences
- NO execution mode
- NO notifications
- NO mobile-optimized layout

---

## Verification Strategy

### QA Policy
Browser-based manual testing at `http://localhost:6199`.

---

## Execution Strategy

```
Wave 1 (Server + Core UI):
├── Task 1: FastAPI server + navigation layout [quick]
├── Task 2: Kanban board + card detail panel [visual-engineering]
├── Task 3: New card form + Cancel/Resume [visual-engineering]
└── Task 4: Mermaid pipeline graph in card detail [quick]

Wave 2 (Management pages + polish):
├── Task 5: Agents page (list + view config) [visual-engineering]
├── Task 6: Runtime settings page [quick]
├── Task 7: Dashboard (stats) [visual-engineering]
├── Task 8: Pipeline viewer page [quick]
├── Task 9: Run history page [visual-engineering]
└── Task 10: Auto-refresh + polish + error states [quick]
```

**Critical Path**: Task 1 → Task 2 → Task 3 → Task 5 → Task 6
**Max Concurrent**: 4 (Wave 2)

---

## TODOs

### Wave 1 — Server + Core UI (Tasks 1-4)

- [ ] 1. FastAPI server + navigation layout

  **What to do**:
  - Add `fastapi`, `uvicorn` to `requirements.txt`
  - Create `web/__init__.py`, `web/app.py`, `web/routes.py`
  - FastAPI app on port 6199 with static files
  - Navigation bar: Dashboard, Kanban, Agents, Runtime, Pipelines, History
  - Base template with HTMX + Mermaid CDN
  - All API endpoints for CRUD operations
  - Startup: init_db, recover_orphaned_steps, start scheduler

  **APIs needed**:
  - Cards: GET list, GET detail, POST create, POST cancel, POST resume
  - Agents: GET list, GET detail
  - Config: GET config.yaml, POST update
  - Pipelines: GET list, GET detail (YAML content + Mermaid)
  - Runs: GET history (filterable)
  - Dashboard: GET stats (counts by status, today's cards)
  - Resources: GET list for card, GET content
  - Logs: GET for card/step

  **Acceptance**:
  - [ ] `uvicorn web.app:app --port 6199` starts
  - [ ] `curl localhost:6199/api/cards` returns `[]`
  - [ ] Navigation bar visible on all pages

  **QA**: Open browser → see Dashboard with 0 cards

- [ ] 2. Kanban board + card detail panel

  **What to do**:
  - 4 columns: Created, In Progress, Review, Done
  - Cards show: id, pipeline, current agent, status color
  - Click card → slide-in detail panel (right side)
  - Detail panel: steps list, resources, logs tab, cancel/resume
  - HTMX auto-refresh: `hx-get="/api/cards" hx-trigger="every 5s"`

  **Acceptance**:
  - [ ] Board loads with 4 empty columns
  - [ ] Card created → appears on board within 5s
  - [ ] Click card → detail panel slides in
  - [ ] Steps, resources, logs visible

  **QA**: Create card via CLI, verify on board

- [ ] 3. New card form + Cancel/Resume

  **What to do**:
  - "+ New Card" button → modal form
  - Fields: pipeline dropdown, platforms checkboxes, description textarea, jira_url
  - Submit → POST /api/cards → board refreshes
  - Pipeline auto-starts on creation
  - Cancel: confirm dialog → POST /api/cards/{id}/cancel
  - Resume: POST /api/cards/{id}/resume → re-run failed steps

  **Acceptance**:
  - [ ] Form opens, all fields present
  - [ ] Submit creates card → appears on board
  - [ ] Cancel marks step cancelled
  - [ ] Resume restarts failed pipeline

- [ ] 4. Mermaid pipeline graph

  **What to do**:
  - In card detail panel, below steps
  - Generate Mermaid flowchart from pipeline YAML
  - Nodes colored by step status: completed=#3fb950, running=#58a6ff, pending=#8b949e, failed=#f85149
  - Animated pulse on current running node
  - Re-renders when detail panel refreshes

  **Acceptance**:
  - [ ] Graph renders correctly for jira-sdlc
  - [ ] Completed steps green, running step blue, pending gray
  - [ ] Failed steps red

### Wave 2 — Management Pages (Tasks 5-10)

- [ ] 5. Agents page (list + view config)

  **What to do**:
  - List all 8 agents with: name, model, access level, skills count
  - Click agent → expand to show full YAML config (syntax-highlighted)
  - **Read-only for now** (edit comes in v2.1 — YAML editing in browser is tricky)
  - Show agent status: last active time, total runs
  - Route: `/agents`

  **Acceptance**:
  - [ ] All 8 agents listed
  - [ ] Click agent → YAML config visible
  - [ ] Model, access level, skills shown

- [ ] 6. Runtime settings page

  **What to do**:
  - Read `config.yaml` and display all settings
  - Sections: workspace, defaults (model, timeout, loops), Jira, GitLab, OpenRouter, scheduler
  - API keys masked (`${JIRA_API_TOKEN}` not actual value)
  - Scheduler jobs list with status (next run time)
  - **Read-only for now** (edit comes in v2.1)
  - Route: `/runtime`

  **Acceptance**:
  - [ ] Config displayed with all sections
  - [ ] Scheduler jobs listed
  - [ ] Secrets masked

- [ ] 7. Dashboard (stats)

  **What to do**:
  - Quick stats cards at top:
    - Total cards today
    - Success rate (completed / total)
    - Active cards (running)
    - Failed cards
  - Recent activity list (last 10 cards)
  - Route: `/` (homepage)

  **Acceptance**:
  - [ ] Stats cards show correct numbers
  - [ ] Recent activity list updates

- [ ] 8. Pipeline viewer page

  **What to do**:
  - List available pipelines (jira-sdlc, investigate)
  - Click pipeline → show:
    - YAML content (syntax-highlighted)
    - Mermaid preview graph
    - Step count, agent count
  - Route: `/pipelines`

  **Acceptance**:
  - [ ] Both pipelines listed
  - [ ] Click → YAML + Mermaid shown

- [ ] 9. Run history page

  **What to do**:
  - List all past runs with: card_id, pipeline, status, started_at, duration
  - Filters: by pipeline, by status, by date range
  - Click run → expand to show steps
  - Route: `/history`

  **Acceptance**:
  - [ ] Runs listed with filters
  - [ ] Click → steps visible

- [ ] 10. Auto-refresh + polish

  **What to do**:
  - All dynamic content: HTMX auto-refresh (board 5s, detail 3s, dashboard 10s)
  - Loading indicators (HTMX `hx-indicator` — spinner)
  - Empty states: friendly messages for each page
  - Error states: friendly messages on API failures
  - Color coding consistency across all pages

  **Acceptance**:
  - [ ] Board updates without page reload
  - [ ] Loading spinner during requests
  - [ ] Empty states shown when no data
  - [ ] Error states shown on API failures

---

## Final Checklist
- [ ] `uvicorn web.app:app --port 6199` starts
- [ ] All 6 nav pages load
- [ ] Kanban board: create card → appears → click detail
- [ ] Agents: list all 8 → view config
- [ ] Runtime: config displayed
- [ ] Pipelines: both listed → Mermaid shown
- [ ] History: runs list with filters
- [ ] Dashboard: stats visible
- [ ] Cancel / Resume work
- [ ] Auto-refresh works on all pages
