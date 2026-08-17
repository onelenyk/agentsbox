"""Throwaway stdio MCP server for the runner smoke test.

Exposes exactly one tool, ``report``, and writes the arguments it was called
with to the file named by ``SMOKE_RESULT_PATH``. That file existing afterwards
is the proof the whole chain worked: the CLI spawned, loaded our generated
config, connected this server over stdio, showed the model the tool under the
runner's own naming convention, and the model actually called it.

Deliberately not core.tools.agent_mcp_stdio: a smoke test must not mint a real
session, post to a task thread, or reach the daemon at all. Nothing here has
side effects beyond the one result file.
"""
import json
import os
import sys

RESULT_PATH = os.environ.get("SMOKE_RESULT_PATH", "")

TOOLS = [{
    "name": "report",
    "description": (
        "Report that you can reach this tool. Call this immediately with a "
        "short status string. Do not do anything else."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"status": {"type": "string", "description": "Any short string."}},
        "required": ["status"],
    },
}]


def _send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:  # noqa: BLE001 — a malformed frame is not ours to fix
            continue
        method, rid = req.get("method"), req.get("id")

        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "sisyphus-smoke", "version": "1.0"},
            }})
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = req.get("params") or {}
            if RESULT_PATH:
                try:
                    with open(RESULT_PATH, "w", encoding="utf-8") as fh:
                        json.dump({"name": params.get("name"),
                                   "arguments": params.get("arguments")}, fh)
                except Exception:  # noqa: BLE001
                    pass
            _send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": "recorded"}]}})
        elif rid is not None:
            # Anything else gets an empty result rather than an error, so a CLI
            # probing optional capabilities does not treat us as broken.
            _send({"jsonrpc": "2.0", "id": rid, "result": {}})


if __name__ == "__main__":
    main()
