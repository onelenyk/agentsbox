"""The worker's MCP bridge, spawned for real (tools/agent_mcp_stdio.py).

Every tool a worker has reaches it through this process. It had no test that
executed it — the suite asserted only that core/worker.py wires the right path
into the generated opencode.json, which is a claim about a string. So these
spawn the real script, speak JSON-RPC to its stdin, and read its stdout, with
the daemon's real /mcp/agent router answering on a real socket.

The bridge is transport and nothing else, so what is worth pinning is what
transport can get wrong: a roster that arrives empty, a reply that never comes,
a malformed line that takes the process down with it, and a daemon that is not
there. The most expensive incident this project has documented (docs/known-gaps,
closed 2026-08-23) was an engineer retrying open_mr 160 times against an empty
tool roster — so "the roster arrives, and a failure never LOOKS like an empty
roster" is the guarantee these are here for.
"""
import json
import os
import pathlib
import select
import subprocess
import sys
import threading
import time

import pytest

from core import agents, sessions, store

BRIDGE = pathlib.Path(__file__).resolve().parents[1] / "tools" / "agent_mcp_stdio.py"


class _Bridge:
    """A spawned bridge process, one JSON-RPC line at a time."""

    def __init__(self, proc):
        self.proc = proc

    def send(self, request: dict) -> None:
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()

    def readline(self, timeout=15.0):
        """The next line of stdout, or None if none arrives in *timeout*."""
        ready, _, _ = select.select([self.proc.stdout], [], [], timeout)
        if not ready:
            return None
        line = self.proc.stdout.readline()
        return json.loads(line) if line.strip() else None

    def call(self, request: dict, timeout=15.0):
        self.send(request)
        return self.readline(timeout)


def _rpc(method, params=None, req_id=1):
    return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}


@pytest.fixture
def daemon(db_path):
    """The real /mcp/agent router on a real port.

    Only that router — the full app's lifespan starts the orchestrator and the
    autopilot loop, and this test is about a socket, not a daemon."""
    import uvicorn
    from fastapi import FastAPI
    from web.agent_mcp import agent_mcp

    app = FastAPI()
    app.include_router(agent_mcp)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 15
    while time.time() < deadline and not (server.started and server.servers):
        time.sleep(0.02)
    assert server.started and server.servers, "the test daemon never bound a port"
    port = server.servers[0].sockets[0].getsockname()[1]

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=10)
    sessions.clear()


@pytest.fixture
def spawn(daemon):
    """Spawn the bridge against a URL and token; kill it when the test ends."""
    procs = []

    def _spawn(token="", url=None):
        env = {**os.environ,
               "SISYPHUS_DAEMON_URL": url or daemon,
               "SISYPHUS_SESSION_TOKEN": token}
        proc = subprocess.Popen(
            [sys.executable, str(BRIDGE)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env)
        procs.append(proc)
        return _Bridge(proc)

    yield _spawn

    for p in procs:
        try:
            p.kill()
            p.wait(timeout=5)
        except Exception:
            pass


def _worker_session(role="engineer"):
    task = store.create_task("do the thing", agent=role)
    return sessions.mint(task["id"], role, worktree="/tmp/wt"), task


# ── the roster ──────────────────────────────────────────────────────────

class TestTheRosterActuallyArrivesThroughTheBridge:

    def test_an_engineer_gets_its_tools(self, spawn):
        token, _ = _worker_session("engineer")
        resp = spawn(token).call(_rpc("tools/list"))

        assert resp and "result" in resp, f"no usable reply: {resp}"
        names = [t["name"] for t in resp["result"]["tools"]]
        assert names, "the bridge delivered an EMPTY roster — the 2026-08-23 shape"
        assert set(names) == set(agents.agent_tools("engineer")) & set(names)
        for expected in ("open_mr", "commit", "finish"):
            assert expected in names, f"an engineer cannot {expected} through the bridge"

    def test_every_tool_arrives_whole(self, spawn):
        """A tool the runner cannot parse is a tool the agent does not have."""
        token, _ = _worker_session("engineer")
        tools = spawn(token).call(_rpc("tools/list"))["result"]["tools"]
        for t in tools:
            assert t["name"] and t["description"]
            assert t["inputSchema"]["type"] == "object"

    def test_a_reviewer_gets_a_different_roster_than_an_engineer(self, spawn):
        """Role scoping is decided on the daemon; the bridge must not flatten
        it by sending everyone's token to the same place."""
        eng_token, _ = _worker_session("engineer")
        rev_token, _ = _worker_session("reviewer")
        eng = [t["name"] for t in spawn(eng_token).call(_rpc("tools/list"))["result"]["tools"]]
        rev = [t["name"] for t in spawn(rev_token).call(_rpc("tools/list"))["result"]["tools"]]
        assert eng and rev and set(eng) != set(rev)

    def test_a_bad_token_is_an_error_not_an_empty_roster(self, spawn):
        """The distinction the incident turned on. An agent told "you have no
        tools" retries against nothing; an agent told "your session is invalid"
        has something to report."""
        resp = spawn("not-a-real-token").call(_rpc("tools/list"))
        assert "error" in resp and "result" not in resp
        assert "session" in resp["error"]["message"]

    def test_no_token_at_all_is_an_error_too(self, spawn):
        resp = spawn("").call(_rpc("tools/list"))
        assert "error" in resp and resp["error"]["code"] == -32001

    def test_an_agent_that_no_longer_exists_is_an_error_not_an_empty_roster(self, spawn):
        """Deleting a custom agent does not revoke the sessions running as it,
        so a live worker can outlive its own definition. agent_tools() answers
        [] for an agent that is not in the table, and an empty roster served as
        a success is precisely the 2026-08-23 incident: nothing to call, no
        reason given, and a run that can only end at its timeout."""
        agents.add_agent("temp-engineer", persona="p", tools=["get_context", "finish"])
        token, _ = _worker_session("temp-engineer")
        assert spawn(token).call(_rpc("tools/list"))["result"]["tools"], "fixture is wrong"

        agents.delete_agent("temp-engineer")
        resp = spawn(token).call(_rpc("tools/list"))

        assert "result" not in resp
        assert resp["error"]["code"] == -32002
        assert "temp-engineer" in resp["error"]["message"]
        assert "cannot proceed" in resp["error"]["message"]

    def test_an_agent_saved_with_no_tools_is_an_error_too(self, spawn):
        agents.add_agent("toothless", persona="p", tools=[])
        token, _ = _worker_session("toothless")
        resp = spawn(token).call(_rpc("tools/list"))
        assert resp["error"]["code"] == -32002


class TestCallingAToolThroughTheBridge:

    def test_arguments_reach_the_daemon_and_the_result_comes_back(self, spawn):
        token, task = _worker_session("engineer")
        resp = spawn(token).call(_rpc("tools/call", {
            "name": "comment", "arguments": {"text": "halfway there"}}))

        assert resp["result"]["isError"] is False
        thread = store.get_thread(task["id"])
        assert thread[-1]["text"] == "halfway there"
        assert thread[-1]["author"] == "engineer"

    def test_a_tool_the_role_may_not_use_comes_back_as_an_error(self, spawn):
        token, _ = _worker_session("reviewer")
        resp = spawn(token).call(_rpc("tools/call", {"name": "commit",
                                                     "arguments": {"message": "x"}}))
        assert resp["result"]["isError"] is True
        assert "not permitted" in resp["result"]["content"][0]["text"]


# ── the stdio contract ──────────────────────────────────────────────────

class TestTheStdioContract:

    def test_initialize_round_trips(self, spawn):
        resp = spawn("").call(_rpc("initialize"))
        assert resp["result"]["protocolVersion"] == "2024-11-05"
        assert resp["result"]["serverInfo"]["name"] == "sisyphus-agent"
        assert resp["id"] == 1

    def test_a_notification_gets_no_reply_and_does_not_desync_the_stream(self, spawn):
        """The daemon answers a notification with 202 and no body. If the
        bridge wrote a line anyway, every later reply would be read against the
        wrong request."""
        token, _ = _worker_session()
        bridge = spawn(token)
        bridge.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert bridge.readline(timeout=2) is None, "answered a notification"

        resp = bridge.call(_rpc("initialize", req_id=7))
        assert resp["id"] == 7

    def test_a_malformed_line_is_a_parse_error_and_the_bridge_survives_it(self, spawn):
        """One bad line must not end the worker's access to every tool."""
        token, _ = _worker_session()
        bridge = spawn(token)
        bridge.proc.stdin.write("{not json\n")
        bridge.proc.stdin.flush()

        err = bridge.readline()
        assert err["error"]["code"] == -32700
        assert err["id"] is None

        names = [t["name"] for t in bridge.call(_rpc("tools/list", req_id=2))["result"]["tools"]]
        assert names, "a malformed line cost the worker its roster"

    def test_blank_lines_are_skipped(self, spawn):
        token, _ = _worker_session()
        bridge = spawn(token)
        bridge.proc.stdin.write("\n   \n")
        bridge.proc.stdin.flush()
        assert bridge.readline(timeout=2) is None
        assert bridge.call(_rpc("initialize", req_id=3))["id"] == 3

    def test_ids_are_preserved_across_a_conversation(self, spawn):
        token, _ = _worker_session()
        bridge = spawn(token)
        for i in (1, 2, 99, 100):
            assert bridge.call(_rpc("tools/list", req_id=i))["id"] == i

    def test_an_unknown_method_is_reported_by_name(self, spawn):
        token, _ = _worker_session()
        resp = spawn(token).call(_rpc("tools/teleport"))
        assert resp["error"]["code"] == -32601 and "tools/teleport" in resp["error"]["message"]


# ── when the other end is not there ─────────────────────────────────────

class TestWhenTheDaemonIsNotReachable:

    def test_the_reply_names_the_url_it_could_not_reach(self, spawn):
        """Silence here reads to the runner as a hung server, which is what it
        drops the MCP connection over. An error it can print is the difference
        between a diagnosis and 160 retries."""
        bridge = spawn("tok", url="http://127.0.0.1:1")
        resp = bridge.call(_rpc("tools/list"))

        assert "error" in resp and resp["error"]["code"] == -32000
        assert "not reachable" in resp["error"]["message"]
        assert "127.0.0.1:1" in resp["error"]["message"]
        assert "result" not in resp, "an unreachable daemon must not look like a roster"

    def test_the_bridge_stays_up_and_recovers(self, spawn, daemon):
        """A daemon restarting mid-run must not require the runner to respawn
        the MCP server it has already given up on once."""
        bridge = spawn("tok", url="http://127.0.0.1:1")
        assert "error" in bridge.call(_rpc("tools/list", req_id=1))
        assert "error" in bridge.call(_rpc("tools/list", req_id=2))
        assert bridge.proc.poll() is None, "the bridge exited on an unreachable daemon"

    def test_an_http_error_is_reported_with_its_status(self, spawn, db_path):
        import uvicorn
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse

        app = FastAPI()

        @app.post("/mcp/agent")
        async def boom():
            return JSONResponse({"detail": "nope"}, status_code=500)

        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0,
                                               log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = time.time() + 15
            while time.time() < deadline and not (server.started and server.servers):
                time.sleep(0.02)
            port = server.servers[0].sockets[0].getsockname()[1]

            resp = spawn("tok", url=f"http://127.0.0.1:{port}").call(_rpc("tools/list"))
            assert resp["error"]["code"] == -32000
            assert "HTTP 500" in resp["error"]["message"]
        finally:
            server.should_exit = True
            thread.join(timeout=10)
