# Sisyphus Architecture

## Process Design

Runs directly on the host as one or more plain Python processes (CLI, web/API, MCP server), each capable of running the scheduler and agent launcher. No microservices, no container — simplicity over scale.

## Agent Lifecycle

Agents run as **subprocesses** of the main Python process. Each agent is an isolated `opencode` invocation communicating via stdout JSON. The orchestrator spawns, monitors, and kills agents.

## Isolation Model

Each pipeline job gets a **git worktree** — an isolated working directory with its own branch. No shared mutable state between jobs. Workspaces are ephemeral and cleaned after completion.

## State Persistence

Two-tier storage:

- **SQLite** (`sisyphus.db`) — metadata, job queue, agent results, scheduler state
- **Filesystem** (`workspace/`) — git worktrees, agent logs, artifacts, pipeline outputs

## Resource Model

All pipeline inputs and outputs are **append-only resources**. A resource is a named file or directory produced by one step and consumed by another. Resources are tracked in SQLite and stored on disk under `workspace/resources/`.

## Pipeline Execution

Pipelines are defined as **YAML DAGs**. Each step declares inputs, an agent type, and outputs. The engine resolves dependencies, schedules steps topologically, passes resources between steps, and collects results.

## CLI Interface

CLI-first design using `click`. All operations available via `python -m sisyphus <command>`. Commands: `run` (pipeline/agent), `scheduler` (start/stop/status), `workspace` (list/clean).

## Key Principles

- No web framework — CLI + scheduler only until v2
- Agents don't share memory — only pass resources
- Every job is repeatable from its inputs
- Fail fast on agent errors, surface immediately to the caller
