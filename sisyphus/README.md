# Sisyphus — Personal AI Agent Orchestration

Sisyphus orchestrates AI agents to automate software development workflows, running agents as subprocesses with git worktree isolation. It runs directly on the host — no container — and is controlled either manually (CLI/web UI) or through its MCP server.

## Prerequisites

- **Python 3.12**
- **opencode** binary (or another configured runner) on `$PATH`

## Setup

```bash
git clone <repo-url> && cd sisyphus
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your API keys and endpoints
```

`WORKSPACE_ROOT` (git worktrees, run artifacts, `sisyphus.db`) defaults to `./workspace` next to the repo — no setup needed unless you want it elsewhere.

Register at least one repo and runner before running a pipeline — there's no CLI command for repos, use the web UI (`/repos` page) or an MCP tool (`sisyphus_add_repo`):

```bash
python3 sisyphus.py runners add --name opencode --binary opencode --default
```

## Quick Start

Three ways to run Sisyphus, all direct Python processes:

```bash
# CLI — create a card and run a pipeline
python3 sisyphus.py run --repo sisyphus --pipeline investigate --desc "Investigate X" --detach

# Check status / logs
python3 sisyphus.py status
python3 sisyphus.py status <card_id> --verbose
python3 sisyphus.py logs <card_id> --follow

# Cancel / resume / list / cleanup
python3 sisyphus.py cancel <card_id>
python3 sisyphus.py list --status running
python3 sisyphus.py cleanup --keep-days 7 --dry-run

# Runners and scheduler
python3 sisyphus.py runners list
python3 sisyphus.py scheduler status
```

```bash
# Web UI + API (manual dashboard) — kanban board over the same cards/runs
python3 -m uvicorn web.app:app --host 0.0.0.0 --port 7677
```

```bash
# MCP server (agent control) — stdio JSON-RPC, zero external deps
python3 mcp_server.py
```

Register `mcp_server.py` as an MCP stdio server in your MCP client config to give an agent the same card/repo/runner/pipeline control as the CLI and web UI. It can also start/stop/check the web server itself via the `sisyphus_server_start` / `sisyphus_server_stop` / `sisyphus_server_status` tools.

## Configuration

All configuration lives in `config.yaml`. Environment variable substitution is supported with `${VAR_NAME}` syntax. Secrets go in `.env` (never committed).

## Project Structure

```
sisyphus/
├── core/           # Core engine — pipeline runner, agent lifecycle
├── agents/         # Agent implementations (subprocess wrappers)
├── tools/          # Tool implementations (git, api clients)
├── web/            # FastAPI web UI + API
├── mcp_server.py   # MCP stdio server for agent control
├── sisyphus.py     # CLI entrypoint
├── workspace/      # Runtime data (git worktrees, logs, artifacts) — gitignored
├── config.yaml     # System configuration
└── requirements.txt
```
