# Sisyphus — The Idea

> *"Humans roll their boulder every day. So do AI agents. We're not so different."*

---

## What Is Sisyphus?

Sisyphus is a **personal AI agent orchestration system**. It replaces the manual "me + AI agent" workflow with an automated pipeline where multiple specialized AI agents collaborate — each with their own role, perspective, and responsibility — to produce higher-quality results than any single agent could alone.

### The Core Insight

**One agent is blind.** A single AI agent, no matter how capable, has cognitive gaps. It skips edge cases. It misses files. It hallucinates patterns that don't exist. The only way to compensate is to have multiple agents check each other's work from different angles — the same way a human team (PM, Engineer, Reviewer, QA) catches what individuals miss.

Sisyphus makes this automatic.

---

## Why We Built It

### The Problem: "Working with an agent feels faster, but is actually slower"

When you work directly with an AI agent (openCode, Claude Code, Codex):

1. You write a prompt describing the task
2. The agent works for 5-20 minutes
3. You review the output, find 3-5 issues
4. You explain what's wrong, the agent tries again
5. You repeat steps 3-4 a few more times
6. Total time: 1-2 hours. Result: often still imperfect.

The bottleneck is **you**. Every iteration requires your attention. Every mistake requires your explanation. The agent never learns from its own errors because there's no second pair of eyes checking its work.

### The Solution: Automated Multi-Agent Collaboration

Sisyphus runs the same workflow you do manually, but **without you in the loop**:

```
Your manual process:                    Sisyphus:
                                        
You: "Fix DAMU brand colors"           User: creates card with Jira link
Agent: writes plan (5min)              PM: reads Jira, understands context
You: finds 3 missed files              Assistant: analyzes screenshots
Agent: fixes (5min)                    Tech Lead: determines affected platforms
You: finds 2 more issues               Engineer: writes Fix Plan (5min)
Agent: fixes (5min)                    Reviewer: finds missed files, edge cases
You: "good enough" (30min total)       Engineer: revises plan (3min)
                                       QA: validates completeness
                                       → Done in 4 minutes, with zero human review
```

**The difference:** In the manual process, the human is the bottleneck (30 min). In Sisyphus, the Reviewer and QA agents catch what the Engineer missed — and the loop resolves automatically.

---

## The Philosophy

### Agent Roles Mirror Human Teams

| Human Role | Sisyphus Agent | Responsibility |
|---|---|---|
| Product Manager | PM | Understands the task, gathers context |
| Tech Lead | Tech Lead | Performs technical analysis, assigns platforms |
| Developer | Engineer | Writes Fix Plan, analyzes code |
| Code Reviewer | Reviewer | Finds missed problems, requests changes |
| QA Engineer | QA | Validates completeness |

**The key:** Each role checks the work of the previous one. No single agent carries the full burden. The chain of verification is the product.

*The PM Assistant (not shown above) is a specialized context-gathering agent that fetches Jira data and analyzes screenshots before the Tech Lead begins.*

### Deterministic Over AI Where Possible

Sisyphus uses AI **only where intelligence is needed** (analyzing code, writing plans). Everything else is deterministic Python:

- Pipeline orchestration → DAG walker (deterministic)
- State transitions → FSM (deterministic)
- Git management → subprocess (deterministic)
- Tool execution → Python scripts (deterministic)
- UI rendering → HTMX (deterministic)
- Card/run tracking → SQLite (deterministic)

This means: the system **never hallucinates** what to do next. The pipeline YAML defines the sequence. The FSM enforces valid transitions. The tools produce predictable output.

### File-Based Over Cloud-Based

Everything lives on your machine. No cloud dependency, no vendor lock-in, no API quotas:

- State: SQLite file (`sisyphus.db`)
- Config: YAML file (`config.yaml`)
- Agent outputs: markdown files (`analysis.md`)
- Run history: directory tree (`runs/{run_id}/`)

Backup → `cp -r sisyphus/ backup/`. Migration → `git push`. Debug → `cat runs/SLN-12345/03_tech_lead/analysis.md`.

---

## The Architecture

### Layered Design

```
┌────────────────────────────────────────────┐
│              Web UI (HTMX + Mermaid)        │  ← User interface
├────────────────────────────────────────────┤
│              REST API (FastAPI)             │  ← HTTP endpoints
├────────────────────────────────────────────┤
│              CLI (Click)                    │  ← Terminal interface
├────────────────────────────────────────────┤
│         Core Engine (pipeline, fsm)         │  ← Orchestration logic
├────────────────────────────────────────────┤
│    State (SQLite)  │  Resources (files)     │  ← Data layer
└────────────────────────────────────────────┘
```

The core engine (`core/`) is UI-agnostic. CLI and Web both call the same functions. You can switch between them or use both.

### Agent Isolation

Each agent runs in an isolated git worktree — a complete copy of the codebase that cannot interfere with other agents. This means:

- **Multiple engineers can run in parallel** on the same repo (across different cards/pipelines)
- **No git conflicts** between agents
- **Clean teardown** after each step (worktree removed)

### Resource Model

Agents communicate through a shared, append-only resource directory:

```
runs/SLN-12345/resources/
├── 01_pm/
│   └── analysis.md          ← PM's analysis
├── 02_assistant/
│   ├── screenshot_1.md       ← Gemini description of screenshot
│   └── jira_data.json        ← Raw Jira API response
├── 03_tech_lead/
│   └── analysis.md           ← Tech Lead's findings
├── 04_engineer/
│   ├── result.json           ← Machine-readable output
│   ├── analysis.md           ← Detailed Fix Plan
│   └── grep_results.txt      ← Raw grep output
├── 05_reviewer/
│   ├── result.json           ← verdict: "changes_requested"
│   └── analysis.md           ← What specifically to fix
└── 06_qa/
    └── result.json           ← verdict: "pass"
```

**Each agent sees everything produced by all previous agents.** Resources are **append-only** — agents can add new files but never modify or delete existing ones. This preserves: **audit integrity** (you can trace every decision to its source), **replayability** (re-run any step with the same context), and **parallel safety** (agents running simultaneously cannot interfere with each other's data). No hidden state. No back-channels.

### Pipeline as Code

Pipelines are YAML files, not hardcoded logic:

```yaml
name: jira-sdlc
steps:
  - id: pm
    agent: pm
    setup:
      - tool: fetch_jira      # runs BEFORE agent
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

Adding a new step, changing the order, or inserting a new agent requires editing ONE YAML file. No code changes.

> **Note:** The `setup` field runs deterministic tools (like `fetch_jira`) *before* the agent starts. These tools produce resources that the agent reads — no agent ever calls APIs directly.

---

## What Sisyphus Replaces

### Before (Multica Cloud)

- **Kanban board**: Multica Cloud (paid, no export)
- **Agent lifecycle**: Multica daemon (Go process, needs WebSocket)
- **@mention flow**: Manual triggers, agents sometimes don't wake up
- **State**: PostgreSQL (Multica Cloud)
- **Config**: Multica Cloud UI (no file-based backup)
- **Debug**: Logs scattered in `~/.multica/`

### After (Sisyphus)

- **Kanban board**: Built-in web UI (free, self-hosted)
- **Agent lifecycle**: Python subprocess (deterministic, no daemon)
- **Pipeline flow**: DAG-based (no @mention, no waiting)
- **State**: SQLite file (copy-paste backup)
- **Config**: YAML file (git-tracked)
- **Debug**: File tree (`runs/{run_id}/`) with full history

---

## The Trade-offs

### What We Gave Up

| What | Why |
|------|-----|
| Multi-user support | Single-user tool — not needed |
| Real-time WebSocket | HTMX polling is simpler, sufficient |
| Cloud-hosted | Local is faster, private, zero cost |
| OAuth/SSO | No users to authenticate |
| Mobile UI | Desktop-only (browser) |
| Notification system | Toast notifications in the browser only |
| Code execution | Plan mode only — agents analyze but never modify code |

### What We Gained

| What | Why |
|------|-----|
| Full control | Every file, every line — yours |
| Zero cost | No subscription, no API fees beyond opencode |
| Instant backup | `cp` or `git push` |
| Deterministic pipelines | YAML-defined, FSM-enforced |
| Full audit trail | Every agent output preserved |
| Git isolation | Parallel agents, no conflicts |
| Easy debug | `cat runs/{run_id}/03_tech_lead/analysis.md` |
| Platform-to-repo mapping | Select "android" → system finds the right repo |

---

## The Numbers

| Metric | Value |
|--------|-------|
| Lines of Python | ~2,640 |
| Lines of HTML/CSS | ~920 |
| Lines of YAML config | ~200 |
| Database tables | 4 (cards, runs, steps, resources) |
| Agent definitions | 8 |
| Pipeline definitions | 2 |
| UI pages | 7 |
| Unit tests | 45 |
| Tools | 3 |
| Skills | 3 |
| Mock agents for testing | 7 |
| Build time (from scratch) | ~32 hours |
| Tech stack | Python 3.9+, FastAPI, HTMX, SQLite, Mermaid.js |
| Deployment | Single Python process (or Docker) |

---

## Future Vision

### v3: Execution Mode
Agents can commit code, create MRs via GitLab API, push to branches.

**Why deferred:** Plan mode is safe by design — agents read and analyze but never modify. Before enabling execution, we need: (a) staged rollout (test on non-production repos first), (b) mandatory human review of MRs before merge, (c) rollback safety via separate branches. Safety before speed.

### v4: Self-Improving *(exploratory)*
*No scaffolding exists yet.* Would require: pipeline analytics, prompt versioning, A/B testing of agent configurations. A promising direction but not actively planned.

### v5: Multi-Project
One Sisyphus instance managing multiple independent projects with shared agent pool.

### The Ultimate Goal

A system where you describe a task in natural language, and Sisyphus:
1. Classifies it (bug, feature, investigation)
2. Selects the right pipeline
3. Runs the full agent chain
4. Produces a reviewed, validated Fix Plan (or executed change)
5. Reports back

**Without you in the loop.**
