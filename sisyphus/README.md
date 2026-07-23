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

## Architecture: one daemon, thin clients

The **web/API server is the daemon** — the single always-on process. It owns the
only scheduler and executes all pipeline runs in its own event loop, so a run
survives its trigger: start one from the AI chat and it keeps going in the
daemon even if the chat closes. It serves both the browser (kanban UI) and a
JSON API at `/api/v1`.

```bash
# Start the daemon (Web UI + JSON API + scheduler + executor)
python3 -m uvicorn web.app:app --host 0.0.0.0 --port 7677
```

The **MCP server is a thin client** of the daemon — it holds no state and forwards
every tool call over HTTP. Register it in your MCP client (e.g. an AI chat) to
drive the same system the Web UI drives:

```bash
# points at the daemon via SISYPHUS_DAEMON_URL (default http://127.0.0.1:7677)
python3 mcp_server.py
```

If the daemon isn't running, MCP tools return a friendly "daemon not reachable"
message; the `sisyphus_server_start` tool can bring it up.

The **CLI** is for local one-shots (it runs a pipeline in-process and exits — no
background scheduler):

```bash
python3 sisyphus.py run --repo sisyphus --pipeline investigate --desc "Investigate X"
python3 sisyphus.py status [<card_id> --verbose]
python3 sisyphus.py logs <card_id> --follow
python3 sisyphus.py list --status running
python3 sisyphus.py cancel <card_id>
python3 sisyphus.py runners list
```

All three surfaces share the same `core/` logic — the daemon just exposes it
over HTTP for the clients.

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
