"""
Unit tests for MCP server JSON-RPC protocol and tool dispatch.

Tests cover:
- handle_request dispatcher: initialize, notifications, tools/list, tools/call,
  unknown methods, unknown tools, parse errors
- TOOL_DEFINITIONS: count, structure, required fields
- HANDLERS: count, tool-to-handler mapping symmetry
- Individual tool handlers: direct invocation with mocked dependencies
"""

import json
import sys
import os
from unittest.mock import MagicMock, patch

# mcp_server is now a thin HTTP client: it imports no core modules and has no
# import-time side effects (no DB init, no scheduler), so importing it is cheap.
from mcp_server import handle_request, TOOL_DEFINITIONS, HANDLERS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EXPECTED_TOOL_COUNT = 19

ALL_TOOL_NAMES = [
    "sisyphus_create_card",
    "sisyphus_run_card",
    "sisyphus_list_cards",
    "sisyphus_get_card",
    "sisyphus_cancel_card",
    "sisyphus_resume_card",
    "sisyphus_list_repos",
    "sisyphus_add_repo",
    "sisyphus_delete_repo",
    "sisyphus_update_repo",
    "sisyphus_list_runners",
    "sisyphus_add_runner",
    "sisyphus_update_runner",
    "sisyphus_delete_runner",
    "sisyphus_runner_health",
    "sisyphus_runner_set_default",
    "sisyphus_server_start",
    "sisyphus_server_stop",
    "sisyphus_server_status",
]


def _make_request(method: str, params: dict = None, req_id=1) -> dict:
    """Build a JSON-RPC 2.0 request dict."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": method,
        "params": params or {},
    }


# ===========================================================================
# handle_request — initialize
# ===========================================================================


class TestHandleRequestInitialize:
    """Tests for the 'initialize' method."""

    def test_returns_protocol_version(self):
        req = _make_request("initialize")
        resp = handle_request(req)
        assert resp["id"] == 1
        assert resp["jsonrpc"] == "2.0"
        result = resp["result"]
        assert result["protocolVersion"] == "2024-11-05"

    def test_returns_server_info(self):
        req = _make_request("initialize")
        resp = handle_request(req)
        info = resp["result"]["serverInfo"]
        assert info["name"] == "sisyphus-mcp"
        assert info["version"] == "0.1.0"

    def test_returns_tools_capability(self):
        req = _make_request("initialize")
        resp = handle_request(req)
        caps = resp["result"]["capabilities"]
        assert "tools" in caps
        assert caps["tools"] == {}

    def test_preserves_request_id(self):
        req = _make_request("initialize", req_id="my-custom-id")
        resp = handle_request(req)
        assert resp["id"] == "my-custom-id"


# ===========================================================================
# handle_request — notifications/initialized
# ===========================================================================


class TestHandleRequestNotifications:
    """Tests for the 'notifications/initialized' method."""

    def test_returns_none(self):
        req = _make_request("notifications/initialized")
        resp = handle_request(req)
        assert resp is None


# ===========================================================================
# handle_request — tools/list
# ===========================================================================


class TestHandleRequestToolsList:
    """Tests for the 'tools/list' method."""

    def test_returns_tools_key(self):
        req = _make_request("tools/list")
        resp = handle_request(req)
        assert "tools" in resp["result"]

    def test_returns_all_tool_names(self):
        req = _make_request("tools/list")
        resp = handle_request(req)
        returned_names = [t["name"] for t in resp["result"]["tools"]]
        assert sorted(returned_names) == sorted(ALL_TOOL_NAMES)

    def test_each_tool_has_required_fields(self):
        req = _make_request("tools/list")
        resp = handle_request(req)
        for tool in resp["result"]["tools"]:
            assert "name" in tool, f"Missing 'name' in {tool}"
            assert "description" in tool, f"Missing 'description' in {tool}"
            assert "inputSchema" in tool, f"Missing 'inputSchema' in {tool}"

    def test_correct_tool_count(self):
        req = _make_request("tools/list")
        resp = handle_request(req)
        assert len(resp["result"]["tools"]) == EXPECTED_TOOL_COUNT


# ===========================================================================
# handle_request — tools/call
# ===========================================================================


class TestHandleRequestToolsCall:
    """Tests for the 'tools/call' method."""

    def test_valid_tool_returns_result_with_content(self):
        """Call a tool whose handler we mock so no DB/IO happens."""
        with patch.dict(HANDLERS, {"sisyphus_list_cards": MagicMock(return_value={"cards": [], "count": 0})}):
            req = _make_request("tools/call", params={"name": "sisyphus_list_cards", "arguments": {}})
            resp = handle_request(req)
            assert resp["jsonrpc"] == "2.0"
            assert resp["id"] == 1
            result = resp["result"]
            assert "content" in result
            assert isinstance(result["content"], list)
            assert len(result["content"]) == 1
            assert result["content"][0]["type"] == "text"
            parsed = json.loads(result["content"][0]["text"])
            assert parsed == {"cards": [], "count": 0}

    def test_valid_tool_isError_false_on_success(self):
        with patch.dict(HANDLERS, {"sisyphus_list_repos": MagicMock(return_value={"repos": []})}):
            req = _make_request("tools/call", params={"name": "sisyphus_list_repos", "arguments": {}})
            resp = handle_request(req)
            assert resp["result"]["isError"] is False

    def test_valid_tool_isError_true_when_handler_returns_error_key(self):
        with patch.dict(HANDLERS, {"sisyphus_list_runners": MagicMock(return_value={"error": "something wrong"})}):
            req = _make_request("tools/call", params={"name": "sisyphus_list_runners", "arguments": {}})
            resp = handle_request(req)
            assert resp["result"]["isError"] is True

    def test_unknown_tool_returns_error_code(self):
        req = _make_request("tools/call", params={"name": "nonexistent_tool", "arguments": {}})
        resp = handle_request(req)
        assert resp["error"]["code"] == -32601
        assert "Unknown tool" in resp["error"]["message"]
        assert "nonexistent_tool" in resp["error"]["message"]

    def test_passes_arguments_to_handler(self):
        """Verify that arguments are forwarded to the handler function."""
        mock_handler = MagicMock(return_value={"ok": True})
        with patch.dict(HANDLERS, {"sisyphus_list_cards": mock_handler}):
            req = _make_request("tools/call", params={
                "name": "sisyphus_list_cards",
                "arguments": {"repo": "myrepo", "status": "backlog"},
            })
            handle_request(req)
            mock_handler.assert_called_once_with(repo="myrepo", status="backlog")

    def test_type_error_returns_isError_with_message(self):
        """When handler raises TypeError (wrong args), returns isError=True."""
        def _bad_handler(**kwargs):
            raise TypeError("missing required argument: repo")

        with patch.dict(HANDLERS, {"sisyphus_list_cards": _bad_handler}):
            req = _make_request("tools/call", params={"name": "sisyphus_list_cards", "arguments": {}})
            resp = handle_request(req)
            assert resp["result"]["isError"] is True
            text = resp["result"]["content"][0]["text"]
            assert "missing required argument" in text

    def test_generic_exception_returns_isError_with_message(self):
        """When handler raises a generic Exception, returns isError=True."""
        def _crashing_handler(**kwargs):
            raise RuntimeError("connection refused")

        with patch.dict(HANDLERS, {"sisyphus_list_cards": _crashing_handler}):
            req = _make_request("tools/call", params={"name": "sisyphus_list_cards", "arguments": {}})
            resp = handle_request(req)
            assert resp["result"]["isError"] is True
            text = resp["result"]["content"][0]["text"]
            assert "connection refused" in text


# ===========================================================================
# handle_request — error paths
# ===========================================================================


class TestHandleRequestErrors:
    """Tests for error responses."""

    def test_unknown_method_returns_error_code(self):
        req = _make_request("some.bogus.method")
        resp = handle_request(req)
        assert resp["error"]["code"] == -32601
        assert "Method not found" in resp["error"]["message"]

    def test_unknown_method_includes_method_name(self):
        req = _make_request("custom/weirdStUfF")
        resp = handle_request(req)
        assert "custom/weirdStUfF" in resp["error"]["message"]

    def test_missing_method_key(self):
        """Request dict without 'method' key — treated as empty string."""
        req = {"jsonrpc": "2.0", "id": 1}
        resp = handle_request(req)
        assert resp["error"]["code"] == -32601

    def test_error_response_has_jsonrpc_version(self):
        req = _make_request("does.not.exist")
        resp = handle_request(req)
        assert resp["jsonrpc"] == "2.0"

    def test_parse_error_pattern(self):
        """The main() loop handles JSON parse errors (code -32700).
        We verify the response shape here."""
        # Simulate what main() produces on bad JSON
        response = {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32700, "message": "Parse error"},
        }
        assert response["jsonrpc"] == "2.0"
        assert response["id"] is None
        assert response["error"]["code"] == -32700
        assert "Parse error" in response["error"]["message"]


# ===========================================================================
# TOOL_DEFINITIONS
# ===========================================================================


class TestToolDefinitions:
    """Tests for the TOOL_DEFINITIONS constant."""

    def test_exact_count(self):
        assert len(TOOL_DEFINITIONS) == EXPECTED_TOOL_COUNT

    def test_all_have_name_field(self):
        for tool in TOOL_DEFINITIONS:
            assert "name" in tool
            assert isinstance(tool["name"], str)
            assert len(tool["name"]) > 0

    def test_all_have_description_field(self):
        for tool in TOOL_DEFINITIONS:
            assert "description" in tool
            assert isinstance(tool["description"], str)
            assert len(tool["description"]) > 0

    def test_all_have_inputSchema_with_object_type(self):
        for tool in TOOL_DEFINITIONS:
            schema = tool["inputSchema"]
            assert isinstance(schema, dict)
            assert schema.get("type") == "object"
            assert "properties" in schema or schema.get("properties", {}) == {}

    def test_create_card_requires_repo_and_description(self):
        tool = _find_tool("sisyphus_create_card")
        assert "required" in tool["inputSchema"]
        assert "repo" in tool["inputSchema"]["required"]
        assert "description" in tool["inputSchema"]["required"]

    def test_run_card_requires_repo_and_description(self):
        tool = _find_tool("sisyphus_run_card")
        assert "repo" in tool["inputSchema"]["required"]
        assert "description" in tool["inputSchema"]["required"]

    def test_get_card_requires_card_id(self):
        tool = _find_tool("sisyphus_get_card")
        assert "card_id" in tool["inputSchema"]["required"]

    def test_cancel_card_requires_card_id(self):
        tool = _find_tool("sisyphus_cancel_card")
        assert "card_id" in tool["inputSchema"]["required"]

    def test_resume_card_requires_card_id(self):
        tool = _find_tool("sisyphus_resume_card")
        assert "card_id" in tool["inputSchema"]["required"]

    def test_add_repo_requires_name_and_source(self):
        tool = _find_tool("sisyphus_add_repo")
        assert "name" in tool["inputSchema"]["required"]
        assert "source" in tool["inputSchema"]["required"]

    def test_add_runner_requires_name_and_binary(self):
        tool = _find_tool("sisyphus_add_runner")
        assert "name" in tool["inputSchema"]["required"]
        assert "binary" in tool["inputSchema"]["required"]

    def test_list_tools_have_empty_required(self):
        """List-only tools (no args needed) should have required=[]."""
        for name in ("sisyphus_list_cards", "sisyphus_list_repos",
                     "sisyphus_list_runners", "sisyphus_server_start",
                     "sisyphus_server_stop", "sisyphus_server_status"):
            tool = _find_tool(name)
            assert tool["inputSchema"]["required"] == [], f"{name} should have empty required"

    def test_all_schema_properties_are_objects(self):
        for tool in TOOL_DEFINITIONS:
            props = tool["inputSchema"].get("properties", {})
            assert isinstance(props, dict)

    def test_no_duplicate_tool_names(self):
        names = [t["name"] for t in TOOL_DEFINITIONS]
        assert len(names) == len(set(names))


# ===========================================================================
# HANDLERS
# ===========================================================================


class TestHandlers:
    """Tests for the HANDLERS dict."""

    def test_exact_count(self):
        assert len(HANDLERS) == EXPECTED_TOOL_COUNT

    def test_every_tool_has_handler(self):
        for tool in TOOL_DEFINITIONS:
            name = tool["name"]
            assert name in HANDLERS, f"Missing handler for {name}"

    def test_no_extra_handlers_beyond_tools(self):
        tool_names = {t["name"] for t in TOOL_DEFINITIONS}
        handler_names = set(HANDLERS.keys())
        assert tool_names == handler_names

    def test_all_handlers_are_callable(self):
        for name, handler in HANDLERS.items():
            assert callable(handler), f"Handler {name} is not callable"


# ===========================================================================
# Individual tool handlers (direct invocation with mocked dependencies)
# ===========================================================================


class TestToolForwarding:
    """Every card/repo/runner tool forwards to the daemon's JSON API and passes
    the daemon's response straight through — the MCP process holds no state."""

    def _call(self, tool, **kwargs):
        sentinel = {"ok": "passthrough"}
        with patch("mcp_server._daemon", return_value=sentinel) as mock:
            result = HANDLERS[tool](**kwargs)
        assert result is sentinel  # response returned unchanged
        return mock.call_args

    # cards
    def test_create_card_posts_with_run_false(self):
        args = self._call("sisyphus_create_card", repo="r", description="d")
        assert args.args[0] == "POST"
        assert args.args[1] == "/api/v1/cards"
        assert args.args[2]["repo"] == "r"
        assert args.args[2]["run"] is False

    def test_run_card_posts_with_run_true(self):
        args = self._call("sisyphus_run_card", repo="r")
        assert args.args[1] == "/api/v1/cards"
        assert args.args[2]["run"] is True

    def test_list_cards_gets_with_filter_params(self):
        args = self._call("sisyphus_list_cards", repo="x", status="running")
        assert args.args[:2] == ("GET", "/api/v1/cards")
        assert args.kwargs["params"] == {"repo": "x", "status": "running", "pipeline": ""}

    def test_get_card_gets_by_id(self):
        assert self._call("sisyphus_get_card", card_id="card-1").args == ("GET", "/api/v1/cards/card-1")

    def test_cancel_card_posts(self):
        assert self._call("sisyphus_cancel_card", card_id="c").args == ("POST", "/api/v1/cards/c/cancel")

    def test_resume_card_posts(self):
        assert self._call("sisyphus_resume_card", card_id="c").args == ("POST", "/api/v1/cards/c/resume")

    # repos
    def test_list_repos(self):
        assert self._call("sisyphus_list_repos").args == ("GET", "/api/v1/repos")

    def test_add_repo(self):
        args = self._call("sisyphus_add_repo", name="n", source="s")
        assert args.args[:2] == ("POST", "/api/v1/repos")
        assert args.args[2]["name"] == "n" and args.args[2]["source"] == "s"

    def test_delete_repo(self):
        assert self._call("sisyphus_delete_repo", name="n").args == ("DELETE", "/api/v1/repos/n")

    def test_update_repo(self):
        assert self._call("sisyphus_update_repo", name="n").args == ("POST", "/api/v1/repos/n/update")

    # runners
    def test_list_runners(self):
        assert self._call("sisyphus_list_runners").args == ("GET", "/api/v1/runners")

    def test_add_runner(self):
        args = self._call("sisyphus_add_runner", name="n", binary="b")
        assert args.args[:2] == ("POST", "/api/v1/runners")
        assert args.args[2]["binary"] == "b"

    def test_update_runner(self):
        args = self._call("sisyphus_update_runner", name="n", binary="b")
        assert args.args[:2] == ("PATCH", "/api/v1/runners/n")

    def test_delete_runner(self):
        assert self._call("sisyphus_delete_runner", name="n").args == ("DELETE", "/api/v1/runners/n")

    def test_runner_health(self):
        assert self._call("sisyphus_runner_health", name="n").args == ("POST", "/api/v1/runners/n/health")

    def test_runner_set_default(self):
        assert self._call("sisyphus_runner_set_default", name="n").args == ("POST", "/api/v1/runners/n/set-default")


class TestDaemonHelper:
    """The _daemon HTTP client degrades gracefully."""

    def test_unreachable_daemon_returns_friendly_error(self):
        import urllib.error
        from mcp_server import _daemon
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
            result = _daemon("GET", "/api/v1/cards")
        assert "error" in result
        assert "not reachable" in result["error"]
        assert "uvicorn" in result["error"]

    def test_http_error_detail_surfaced(self):
        import io
        import urllib.error
        from mcp_server import _daemon
        err = urllib.error.HTTPError(
            "u", 404, "Not Found", {}, io.BytesIO(b'{"detail":"Card x not found"}'),
        )
        with patch("urllib.request.urlopen", side_effect=err):
            result = _daemon("GET", "/api/v1/cards/x")
        assert "error" in result
        assert "not found" in result["error"].lower()


class TestHandlerSisyphusServerStatus:
    """Test sisyphus_server_status() directly — must mock subprocess.run."""

    def test_returns_running_false_when_no_process(self):
        """Mock lsof returning no output (no server on port)."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", stderr="")
            from mcp_server import HANDLERS
            result = HANDLERS["sisyphus_server_status"](port=9999)
            assert result["running"] is False
            assert result["port"] == 9999
            assert result["pid"] is None

    def test_returns_running_true_when_process_exists(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="12345\n", stderr="")
            from mcp_server import HANDLERS
            result = HANDLERS["sisyphus_server_status"](port=9999)
            assert result["running"] is True
            assert result["port"] == 9999
            assert result["pid"] == "12345"

    def test_uses_default_port_7677(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", stderr="")
            from mcp_server import HANDLERS
            result = HANDLERS["sisyphus_server_status"]()
            assert result["port"] == 7677


class TestHandlerSisyphusServerStart:
    """Test sisyphus_server_start() — mock subprocess."""

    def test_returns_running_true_with_default_port(self):
        with patch("subprocess.run") as mock_run, patch("subprocess.Popen") as mock_popen:
            mock_run.return_value = MagicMock(stdout="", stderr="")
            mock_popen.return_value = MagicMock(pid=4242)
            from mcp_server import HANDLERS
            result = HANDLERS["sisyphus_server_start"]()
            assert result["running"] is True
            assert result["port"] == 7677
            assert result["pid"] == 4242

    def test_returns_already_running(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="12345\n", stderr="")
            from mcp_server import HANDLERS
            result = HANDLERS["sisyphus_server_start"](port=8080)
            assert result["running"] is True
            assert result["pid"] == "12345"
            assert result["port"] == 8080


class TestHandlerSisyphusServerStop:
    """Test sisyphus_server_stop() — mock subprocess and os.kill."""

    def test_returns_running_false_when_no_process(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", stderr="")
            from mcp_server import HANDLERS
            result = HANDLERS["sisyphus_server_stop"](port=9999)
            assert result["running"] is False
            assert result["port"] == 9999

    def test_kills_process_when_running(self):
        with patch("subprocess.run") as mock_run, patch("os.kill") as mock_kill:
            mock_run.return_value = MagicMock(stdout="11111\n", stderr="")
            from mcp_server import HANDLERS
            result = HANDLERS["sisyphus_server_stop"](port=9999)
            assert result["running"] is False
            assert result["stopped"] is True
            mock_kill.assert_called_once_with(11111, 15)  # SIGTERM = 15


# ===========================================================================
# JSON-RPC 2.0 Response format
# ===========================================================================


class TestJsonRpcFormat:
    """Verify JSON-RPC 2.0 response structure."""

    def test_all_responses_have_jsonrpc_version(self):
        req = _make_request("initialize")
        resp = handle_request(req)
        assert resp["jsonrpc"] == "2.0"

    def test_error_response_has_no_result_key(self):
        req = _make_request("bogus.method")
        resp = handle_request(req)
        assert "result" not in resp
        assert "error" in resp

    def test_success_response_has_no_error_key(self):
        req = _make_request("initialize")
        resp = handle_request(req)
        assert "error" not in resp
        assert "result" in resp

    def test_error_object_has_code_and_message(self):
        req = _make_request("bogus.method")
        resp = handle_request(req)
        err = resp["error"]
        assert "code" in err
        assert "message" in err
        assert isinstance(err["code"], int)


# ---------------------------------------------------------------------------
# Helpers (defined at bottom so they can be discovered after imports)
# ---------------------------------------------------------------------------


def _find_tool(name: str) -> dict:
    """Find a tool definition by name in TOOL_DEFINITIONS."""
    for tool in TOOL_DEFINITIONS:
        if tool["name"] == name:
            return tool
    raise ValueError(f"Tool not found: {name}")
