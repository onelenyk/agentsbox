#!/usr/bin/env python3
"""sisyphus-mcp: MCP server exposing Sisyphus agent orchestration as tools.

Zero external dependencies — pure stdlib.

Implements the Model Context Protocol (MCP) over stdio JSON-RPC 2.0.
Supports tools/list and tools/call only (no resources or prompts).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

# Set WORKSPACE_ROOT before importing core modules
os.environ.setdefault("WORKSPACE_ROOT", os.path.join(os.path.dirname(__file__), "workspace"))

from core.db import init_db
from core.recovery import recover_orphaned_steps

# Initialize on startup
init_db()
recovered = recover_orphaned_steps()
if recovered:
    print(f"[sisyphus-mcp] Recovered {recovered} orphaned steps", file=sys.stderr)


# ── Tool Functions ──────────────────────────────────────────────────────────

# ── Cards (6) ───────────────────────────────────────────────────────────────

def sisyphus_create_card(
    repo: str,
    pipeline: str = "investigate",
    description: str = "",
    jira_url: str = "",
) -> dict[str, Any]:
    """Create a card in backlog (no pipeline execution)."""
    from core.operations import create_card_only

    card = create_card_only(
        pipeline, repo, description or "No description", jira_url or None,
    )
    return {
        "card_id": card["id"],
        "repo_name": card.get("repo_name", ""),
        "status": card["status"],
        "pipeline": card["pipeline_name"],
    }


def sisyphus_run_card(
    repo: str,
    pipeline: str = "investigate",
    description: str = "",
    jira_url: str = "",
) -> dict[str, Any]:
    """Create card and run pipeline synchronously (blocks until done)."""
    import asyncio
    from core.operations import create_and_run

    result = create_and_run(
        pipeline, repo, description or "No description", jira_url or None,
    )
    return {
        "status": result["status"],
        "run_id": result.get("run_id", ""),
        "error": result.get("error", ""),
    }


def sisyphus_list_cards(
    repo: str = "",
    status: str = "",
    pipeline: str = "",
) -> dict[str, Any]:
    """List cards with optional filters."""
    from core.operations import list_cards_filtered

    cards = list_cards_filtered(
        pipeline=pipeline or None,
        status=status or None,
        repo=repo or None,
    )
    return {"cards": cards, "count": len(cards)}


def sisyphus_get_card(card_id: str) -> dict[str, Any]:
    """Get full detail for a card (card, run, steps, resources)."""
    from core.operations import get_card_detail

    return get_card_detail(card_id)


def sisyphus_cancel_card(card_id: str) -> dict[str, Any]:
    """Cancel a running card (marks steps cancelled, kills subprocesses)."""
    import asyncio
    from core.operations import cancel_card

    asyncio.run(cancel_card(card_id))
    return {"success": True, "card_id": card_id}


def sisyphus_resume_card(card_id: str) -> dict[str, Any]:
    """Resume a failed card (cancels old steps, fires fresh pipeline)."""
    import asyncio
    from core.operations import resume_card

    asyncio.run(resume_card(card_id))
    return {"success": True, "card_id": card_id}


# ── Repos (4) ────────────────────────────────────────────────────────────────

def sisyphus_list_repos() -> dict[str, Any]:
    """List all configured git repos."""
    from core.repos import list_repos

    return {"repos": list_repos()}


def sisyphus_add_repo(
    name: str,
    source: str,
    branch: str = "main",
    platform: str = "",
) -> dict[str, Any]:
    """Add a git repo (bare clone + config entry)."""
    from core.repos import add_repo

    path = add_repo(name, source, platform, branch)
    return {"name": name, "source": source, "branch": branch, "bare_path": path}


def sisyphus_delete_repo(name: str) -> dict[str, Any]:
    """Delete a repo (removes bare clone + config entry)."""
    from core.repos import delete_repo

    delete_repo(name)
    return {"success": True, "name": name}


def sisyphus_update_repo(name: str) -> dict[str, Any]:
    """Fetch latest from origin for a bare repo."""
    from core.repos import update_repo

    ok = update_repo(name)
    return {"success": ok, "name": name}


# ── Runners (6) ─────────────────────────────────────────────────────────────

def sisyphus_list_runners() -> dict[str, Any]:
    """List all configured AI runners."""
    from core.runners import list_runners

    return {"runners": list_runners()}


def sisyphus_add_runner(
    name: str,
    binary: str,
    type: str = "opencode",
    model: str = "",
    api_key_env: str = "",
    base_url: str = "",
    is_default: bool = False,
) -> dict[str, Any]:
    """Add a new AI runner configuration."""
    from core.runners import add_runner

    r = add_runner(name, binary, type, model, api_key_env, base_url, [], is_default)
    return {
        "id": r["id"],
        "name": r["name"],
        "binary": r["binary"],
        "type": r["type"],
    }


def sisyphus_update_runner(
    name: str,
    binary: str = "",
    model: str = "",
    api_key_env: str = "",
    base_url: str = "",
) -> dict[str, Any]:
    """Update an existing runner's configuration."""
    from core.runners import get_runner_by_name, update_runner

    r = get_runner_by_name(name)
    if not r:
        return {"error": f"Runner not found: {name}"}

    fields: dict[str, Any] = {}
    if binary:
        fields["binary"] = binary
    if model:
        fields["model"] = model
    if api_key_env:
        fields["api_key_env"] = api_key_env
    if base_url:
        fields["base_url"] = base_url

    update_runner(r["id"], **fields)
    return {"success": True, "name": name}


def sisyphus_delete_runner(name: str) -> dict[str, Any]:
    """Delete a runner by name."""
    from core.runners import get_runner_by_name, delete_runner

    r = get_runner_by_name(name)
    if not r:
        return {"error": f"Runner not found: {name}"}
    delete_runner(r["id"])
    return {"success": True, "name": name}


def sisyphus_runner_health(name: str) -> dict[str, Any]:
    """Check runner health (binary availability, API key, base URL reachability)."""
    import asyncio
    from core.runners import get_runner_by_name, health_check

    r = get_runner_by_name(name)
    if not r:
        return {"error": f"Runner not found: {name}"}
    return asyncio.run(health_check(r["id"]))


def sisyphus_runner_set_default(name: str) -> dict[str, Any]:
    """Set a runner as the default for all agents."""
    from core.runners import get_runner_by_name, set_default_runner

    r = get_runner_by_name(name)
    if not r:
        return {"error": f"Runner not found: {name}"}
    set_default_runner(r["id"])
    return {"success": True, "name": name}


# ── Server (3) ───────────────────────────────────────────────────────────────

def sisyphus_server_start(port: int = 7677) -> dict[str, Any]:
    """Start the Sisyphus web server (uvicorn) on specified port."""
    import os
    import subprocess

    # Check if already running
    result = subprocess.run(["lsof", "-ti", f":{port}"], capture_output=True, text=True)
    if result.stdout.strip():
        return {"running": True, "pid": result.stdout.strip(), "port": port, "url": f"http://localhost:{port}"}

    ws_root = os.environ.get("WORKSPACE_ROOT", os.path.join(os.path.dirname(__file__), "workspace"))
    proc = subprocess.Popen(
        ["python3", "-m", "uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", str(port)],
        env={**os.environ, "WORKSPACE_ROOT": ws_root},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
    return {"running": True, "pid": proc.pid, "port": port, "url": f"http://localhost:{port}"}


def sisyphus_server_stop(port: int = 7677) -> dict[str, Any]:
    """Stop the Sisyphus web server running on specified port."""
    import os
    import signal
    import subprocess

    result = subprocess.run(["lsof", "-ti", f":{port}"], capture_output=True, text=True)
    if not result.stdout.strip():
        return {"running": False, "port": port}
    for pid_str in result.stdout.strip().split("\n"):
        try:
            os.kill(int(pid_str), signal.SIGTERM)
        except ProcessLookupError:
            pass
    return {"running": False, "port": port, "stopped": True}


def sisyphus_server_status(port: int = 7677) -> dict[str, Any]:
    """Check if the Sisyphus web server is running on specified port."""
    import subprocess

    result = subprocess.run(["lsof", "-ti", f":{port}"], capture_output=True, text=True)
    running = bool(result.stdout.strip())
    return {
        "running": running,
        "port": port,
        "pid": result.stdout.strip() if running else None,
    }


# ── Tool Registry ────────────────────────────────────────────────────────────

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "sisyphus_create_card",
        "description": "Create a card in backlog (no pipeline execution)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "Repository name",
                },
                "pipeline": {
                    "type": "string",
                    "description": "Pipeline name",
                    "default": "investigate",
                },
                "description": {
                    "type": "string",
                    "description": "Task description",
                },
                "jira_url": {
                    "type": "string",
                    "description": "Optional Jira URL",
                },
            },
            "required": ["repo", "description"],
        },
    },
    {
        "name": "sisyphus_run_card",
        "description": "Create card and run pipeline synchronously (blocks until done)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "Repository name",
                },
                "pipeline": {
                    "type": "string",
                    "description": "Pipeline name",
                    "default": "investigate",
                },
                "description": {
                    "type": "string",
                    "description": "Task description",
                },
                "jira_url": {
                    "type": "string",
                    "description": "Optional Jira URL",
                },
            },
            "required": ["repo", "description"],
        },
    },
    {
        "name": "sisyphus_list_cards",
        "description": "List cards with optional filters by repo, status, or pipeline",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "Filter by repository name",
                    "default": "",
                },
                "status": {
                    "type": "string",
                    "description": "Filter by status (backlog, running, completed, failed)",
                    "default": "",
                },
                "pipeline": {
                    "type": "string",
                    "description": "Filter by pipeline name",
                    "default": "",
                },
            },
            "required": [],
        },
    },
    {
        "name": "sisyphus_get_card",
        "description": "Get full detail for a card (card, run, steps, resources)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "card_id": {
                    "type": "string",
                    "description": "Card ID (e.g. card-a1b2c3d4)",
                },
            },
            "required": ["card_id"],
        },
    },
    {
        "name": "sisyphus_cancel_card",
        "description": "Cancel a running card (marks steps cancelled, kills subprocesses)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "card_id": {
                    "type": "string",
                    "description": "Card ID to cancel",
                },
            },
            "required": ["card_id"],
        },
    },
    {
        "name": "sisyphus_resume_card",
        "description": "Resume a failed card (cancels old steps, fires fresh pipeline execution)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "card_id": {
                    "type": "string",
                    "description": "Card ID to resume",
                },
            },
            "required": ["card_id"],
        },
    },
    {
        "name": "sisyphus_list_repos",
        "description": "List all configured git repositories",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "sisyphus_add_repo",
        "description": "Add a git repository (bare clone + config entry)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Repository name (unique identifier)",
                },
                "source": {
                    "type": "string",
                    "description": "Git clone URL (e.g. https://github.com/user/repo.git)",
                },
                "branch": {
                    "type": "string",
                    "description": "Default branch",
                    "default": "main",
                },
                "platform": {
                    "type": "string",
                    "description": "Platform identifier (optional)",
                    "default": "",
                },
            },
            "required": ["name", "source"],
        },
    },
    {
        "name": "sisyphus_delete_repo",
        "description": "Delete a repo (removes bare clone from disk and config entry)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Repository name to delete",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "sisyphus_update_repo",
        "description": "Fetch latest changes from origin for a bare repo",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Repository name to update",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "sisyphus_list_runners",
        "description": "List all configured AI runners",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "sisyphus_add_runner",
        "description": "Add a new AI runner configuration",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Runner name (unique identifier)",
                },
                "binary": {
                    "type": "string",
                    "description": "Binary path or command name (e.g. opencode, claude)",
                },
                "type": {
                    "type": "string",
                    "description": "Runner type: opencode, claude-cli, ollama, openai-api, custom",
                    "default": "opencode",
                },
                "model": {
                    "type": "string",
                    "description": "Model name (e.g. deepseek-v3, gpt-4o)",
                    "default": "",
                },
                "api_key_env": {
                    "type": "string",
                    "description": "Environment variable name for the API key",
                    "default": "",
                },
                "base_url": {
                    "type": "string",
                    "description": "Base URL for the API endpoint",
                    "default": "",
                },
                "is_default": {
                    "type": "boolean",
                    "description": "Set as the default runner for all agents",
                    "default": False,
                },
            },
            "required": ["name", "binary"],
        },
    },
    {
        "name": "sisyphus_update_runner",
        "description": "Update an existing runner's configuration",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Runner name to update",
                },
                "binary": {
                    "type": "string",
                    "description": "New binary path",
                    "default": "",
                },
                "model": {
                    "type": "string",
                    "description": "New model name",
                    "default": "",
                },
                "api_key_env": {
                    "type": "string",
                    "description": "New API key env var name",
                    "default": "",
                },
                "base_url": {
                    "type": "string",
                    "description": "New base URL",
                    "default": "",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "sisyphus_delete_runner",
        "description": "Delete a runner by name",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Runner name to delete",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "sisyphus_runner_health",
        "description": "Check runner health (binary availability, API key, base URL reachability)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Runner name to check",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "sisyphus_runner_set_default",
        "description": "Set a runner as the default for all agents",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Runner name to set as default",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "sisyphus_server_start",
        "description": "Start the Sisyphus web server (uvicorn) on specified port",
        "inputSchema": {
            "type": "object",
            "properties": {
                "port": {
                    "type": "integer",
                    "description": "Port to listen on (default: 7677)",
                    "default": 7677,
                },
            },
            "required": [],
        },
    },
    {
        "name": "sisyphus_server_stop",
        "description": "Stop the Sisyphus web server running on specified port",
        "inputSchema": {
            "type": "object",
            "properties": {
                "port": {
                    "type": "integer",
                    "description": "Port to kill (default: 7677)",
                    "default": 7677,
                },
            },
            "required": [],
        },
    },
    {
        "name": "sisyphus_server_status",
        "description": "Check if the Sisyphus web server is running on specified port",
        "inputSchema": {
            "type": "object",
            "properties": {
                "port": {
                    "type": "integer",
                    "description": "Port to check (default: 7677)",
                    "default": 7677,
                },
            },
            "required": [],
        },
    },
]


HANDLERS: dict[str, Any] = {
    "sisyphus_create_card": sisyphus_create_card,
    "sisyphus_run_card": sisyphus_run_card,
    "sisyphus_list_cards": sisyphus_list_cards,
    "sisyphus_get_card": sisyphus_get_card,
    "sisyphus_cancel_card": sisyphus_cancel_card,
    "sisyphus_resume_card": sisyphus_resume_card,
    "sisyphus_list_repos": sisyphus_list_repos,
    "sisyphus_add_repo": sisyphus_add_repo,
    "sisyphus_delete_repo": sisyphus_delete_repo,
    "sisyphus_update_repo": sisyphus_update_repo,
    "sisyphus_list_runners": sisyphus_list_runners,
    "sisyphus_add_runner": sisyphus_add_runner,
    "sisyphus_update_runner": sisyphus_update_runner,
    "sisyphus_delete_runner": sisyphus_delete_runner,
    "sisyphus_runner_health": sisyphus_runner_health,
    "sisyphus_runner_set_default": sisyphus_runner_set_default,
    "sisyphus_server_start": sisyphus_server_start,
    "sisyphus_server_stop": sisyphus_server_stop,
    "sisyphus_server_status": sisyphus_server_status,
}


# ── MCP Protocol ─────────────────────────────────────────────────────────────


def handle_request(request: dict[str, Any]) -> dict[str, Any] | None:
    """Dispatch a single JSON-RPC 2.0 request and return a response dict."""
    req_id = request.get("id")
    method = request.get("method", "")
    params = request.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "sisyphus-mcp",
                    "version": "0.1.0",
                },
            },
        }

    if method == "notifications/initialized":
        return None  # type: ignore[return-value]

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOL_DEFINITIONS},
        }

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if tool_name not in HANDLERS:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32601,
                    "message": f"Unknown tool: {tool_name}",
                },
            }

        try:
            raw = HANDLERS[tool_name](**arguments)
        except TypeError as exc:
            raw = {"error": f"invalid arguments: {exc}"}
        except Exception as exc:
            raw = {"error": str(exc)}

        is_error = "error" in raw
        content = [{"type": "text", "text": json.dumps(raw)}]

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": content,
                "isError": is_error,
            },
        }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {
            "code": -32601,
            "message": f"Method not found: {method}",
        },
    }


def main() -> None:
    """Read JSON-RPC 2.0 requests from stdin, write responses to stdout.

    Each line of stdin is a complete JSON-RPC request.  Each line of stdout
    is a complete JSON-RPC response.  No batching — one request per line.
    """
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
            continue

        response = handle_request(request)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
