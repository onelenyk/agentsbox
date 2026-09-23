#!/usr/bin/env python3
"""sisyphus-mcp: MCP server exposing Sisyphus agent orchestration as tools.

Zero external dependencies — pure stdlib.

Implements the Model Context Protocol (MCP) over stdio JSON-RPC 2.0.
Supports tools/list and tools/call only (no resources or prompts).

This is a THIN CLIENT: it holds no state and touches no database. Every tool
forwards to the Sisyphus daemon (the web/API server) over HTTP, so runs
execute — and survive — in the daemon regardless of this process's lifetime.
Point it at the daemon with SISYPHUS_DAEMON_URL (default http://127.0.0.1:7677).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DAEMON_URL = os.environ.get("SISYPHUS_DAEMON_URL", "http://127.0.0.1:7677").rstrip("/")
# Shared API token — only needed when the daemon has auth enabled AND is reached
# non-locally (a loopback daemon exempts localhost). Empty = send no header.
API_TOKEN = os.environ.get("SISYPHUS_API_TOKEN", "").strip()


def _daemon(method: str, path: str, payload: dict | None = None, params: dict | None = None) -> dict:
    """Call the daemon's JSON API. Returns parsed JSON, or {"error": ...}."""
    url = DAEMON_URL + path
    if params:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        if query:
            url += "?" + query
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if API_TOKEN:
        headers["Authorization"] = f"Bearer {API_TOKEN}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        try:
            detail = json.loads(body).get("detail", body)
        except Exception:
            detail = body
        return {"error": f"{exc.code}: {detail}"}
    except urllib.error.URLError as exc:
        return {
            "error": (
                f"Sisyphus daemon not reachable at {DAEMON_URL} ({exc.reason}). "
                f"Start it with: python3 -m uvicorn web.app:app --port 7677"
            )
        }


# ── Tool Functions (thin forwards to the daemon) ─────────────────────────────

# ── Repos (4) ────────────────────────────────────────────────────────────────

def sisyphus_list_repos() -> dict[str, Any]:
    """List all configured git repos."""
    return _daemon("GET", "/api/v1/repos")


def sisyphus_add_repo(
    name: str,
    source: str,
    branch: str = "main",
    platform: str = "",
) -> dict[str, Any]:
    """Add a git repo (bare clone + config entry)."""
    return _daemon("POST", "/api/v1/repos", {
        "name": name, "source": source, "branch": branch, "platform": platform,
    })


def sisyphus_delete_repo(name: str) -> dict[str, Any]:
    """Delete a repo (removes bare clone + config entry)."""
    return _daemon("DELETE", f"/api/v1/repos/{name}")


def sisyphus_update_repo(name: str) -> dict[str, Any]:
    """Fetch latest from origin for a bare repo."""
    return _daemon("POST", f"/api/v1/repos/{name}/update")


# ── Runners (6) ─────────────────────────────────────────────────────────────

def sisyphus_list_runners() -> dict[str, Any]:
    """List all configured AI runners."""
    return _daemon("GET", "/api/v1/runners")


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
    return _daemon("POST", "/api/v1/runners", {
        "name": name, "binary": binary, "type": type, "model": model,
        "api_key_env": api_key_env, "base_url": base_url, "is_default": is_default,
    })


def sisyphus_update_runner(
    name: str,
    binary: str = "",
    model: str = "",
    api_key_env: str = "",
    base_url: str = "",
) -> dict[str, Any]:
    """Update an existing runner's configuration."""
    return _daemon("PATCH", f"/api/v1/runners/{name}", {
        "binary": binary, "model": model, "api_key_env": api_key_env, "base_url": base_url,
    })


def sisyphus_delete_runner(name: str) -> dict[str, Any]:
    """Delete a runner by name."""
    return _daemon("DELETE", f"/api/v1/runners/{name}")


def sisyphus_runner_health(name: str) -> dict[str, Any]:
    """Check runner health (binary availability, API key, base URL reachability)."""
    return _daemon("POST", f"/api/v1/runners/{name}/health")


def sisyphus_runner_set_default(name: str) -> dict[str, Any]:
    """Set a runner as the default for all agents."""
    return _daemon("POST", f"/api/v1/runners/{name}/set-default")


# ── Tasks (4) ────────────────────────────────────────────────────────────────

def sisyphus_create_task(goal: str, repo: str) -> dict[str, Any]:
    """Create a task (goal + repo) and start it processing immediately. repo is
    required — a worker with no repo never gets its tool connection wired up
    and can never succeed."""
    return _daemon("POST", "/api/v1/tasks", {"goal": goal, "repo": repo})


def sisyphus_list_tasks(status: str = "") -> dict[str, Any]:
    """List tasks, optionally filtered by status (active, waiting, paused, done, failed)."""
    return _daemon("GET", "/api/v1/tasks", params={"status": status})


def sisyphus_get_task(task_id: str) -> dict[str, Any]:
    """Get a task's full detail: the task itself, its thread, and a rendered transcript."""
    return _daemon("GET", f"/api/v1/tasks/{task_id}")


def sisyphus_reply_task(task_id: str, text: str) -> dict[str, Any]:
    """Post a human reply to a task's thread (e.g. answering a question) and
    wake the orchestrator to continue."""
    return _daemon("POST", f"/api/v1/tasks/{task_id}/messages", {"text": text})


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
        ["python3", "-m", "uvicorn", "web.app:app", "--host", "127.0.0.1", "--port", str(port)],
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
        "name": "sisyphus_create_task",
        "description": "Create a task (goal + repo) and start it processing immediately",
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "What should get done",
                },
                "repo": {
                    "type": "string",
                    "description": "Repo name to work in — required, a repo-less worker can never succeed",
                },
            },
            "required": ["goal", "repo"],
        },
    },
    {
        "name": "sisyphus_list_tasks",
        "description": "List tasks, optionally filtered by status (active, waiting, paused, done, failed)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Filter by status: active, waiting, paused, done, or failed",
                    "default": "",
                },
            },
            "required": [],
        },
    },
    {
        "name": "sisyphus_get_task",
        "description": "Get a task's full detail: the task itself, its thread, and a rendered transcript",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task ID (e.g. task-a1b2c3d4)",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "sisyphus_reply_task",
        "description": "Post a human reply to a task's thread and wake the orchestrator to continue",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task ID to reply to",
                },
                "text": {
                    "type": "string",
                    "description": "Reply text (e.g. answering a pending question)",
                },
            },
            "required": ["task_id", "text"],
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
    "sisyphus_create_task": sisyphus_create_task,
    "sisyphus_list_tasks": sisyphus_list_tasks,
    "sisyphus_get_task": sisyphus_get_task,
    "sisyphus_reply_task": sisyphus_reply_task,
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
