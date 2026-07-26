#!/usr/bin/env python3
"""agent-mcp stdio bridge — the worker's local MCP server.

opencode spawns this as a local (stdio) MCP server (see core/worker.py's
generated opencode.json), exactly like every other MCP server already
configured on this machine (engram, playwright, etc.) — a plain subprocess
speaking JSON-RPC over stdin/stdout, not a remote HTTP connection (opencode's
remote-MCP transport proved unreliable in practice; see docs/adr/).

It does nothing on its own: it forwards each request byte-for-byte to the
daemon's agent-facing MCP endpoint (web/agent_mcp.py at /mcp/agent) over HTTP,
authenticated with the session token minted for this worker, and writes the
response back to stdout. All real logic (role scoping, tool preconditions,
writing to the task thread) lives in web/agent_mcp.py — this is transport only.

Mirrors mcp_server.py's stdio loop and daemon-forwarding pattern exactly.
Stdlib only.
"""
import json
import os
import sys
import urllib.error
import urllib.request

DAEMON_URL = os.environ.get("SISYPHUS_DAEMON_URL", "http://127.0.0.1:7677").rstrip("/")
SESSION_TOKEN = os.environ.get("SISYPHUS_SESSION_TOKEN", "")
TARGET = DAEMON_URL + "/mcp/agent"


def _forward(request: dict):
    """POST *request* to the daemon's agent-MCP endpoint. Returns the parsed
    JSON response, or None for a notification (no body / 202)."""
    data = json.dumps(request).encode()
    req = urllib.request.Request(
        TARGET, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {SESSION_TOKEN}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        return {
            "jsonrpc": "2.0", "id": request.get("id"),
            "error": {"code": -32000, "message": f"daemon returned HTTP {exc.code}"},
        }
    except urllib.error.URLError as exc:
        return {
            "jsonrpc": "2.0", "id": request.get("id"),
            "error": {
                "code": -32000,
                "message": f"Sisyphus daemon not reachable at {DAEMON_URL} ({exc.reason})",
            },
        }


def main() -> None:
    """Read JSON-RPC 2.0 requests from stdin, write responses to stdout.
    One request per line, no batching — same contract as mcp_server.py."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }) + "\n")
            sys.stdout.flush()
            continue

        response = _forward(request)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
