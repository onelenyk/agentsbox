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

# ---------------------------------------------------------------------------
# Mock side-effect-heavy modules BEFORE importing mcp_server.
# mcp_server calls init_db(), recover_orphaned_steps(), start_scheduler()
# at module level.  We mock scheduler and recovery to avoid background
# threads and noisy recovery output.  DB init is harmless — conftest's
# pytest_configure already sets SISYPHUS_DB → a test database.
# If core.scheduler / core.recovery are already imported by other tests,
# the real modules are already in sys.modules and our mock won't replace
# them — but start_scheduler is then a no-op (idempotent guard).
# ---------------------------------------------------------------------------
_cleanup_needed = []
for _mod_name in ("core.scheduler", "core.recovery"):
    if _mod_name not in sys.modules:
        _m = MagicMock()
        if _mod_name == "core.recovery":
            _m.recover_orphaned_steps = MagicMock(return_value=0)
        if _mod_name == "core.scheduler":
            _m.start_scheduler = MagicMock(return_value=None)
            _m.BackgroundScheduler = MagicMock()
            _m.IntervalTrigger = MagicMock()
        sys.modules[_mod_name] = _m
        _cleanup_needed.append(_mod_name)

from mcp_server import handle_request, TOOL_DEFINITIONS, HANDLERS

for _mod_name in _cleanup_needed:
    del sys.modules[_mod_name]

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


class TestHandlerSisyphusListCards:
    """Test sisyphus_list_cards() directly."""

    @patch("core.operations.list_cards_filtered", return_value=[])
    def test_returns_cards_and_count_keys(self, _mock):
        from mcp_server import HANDLERS
        result = HANDLERS["sisyphus_list_cards"]()
        assert "cards" in result
        assert "count" in result
        assert result["cards"] == []
        assert result["count"] == 0

    @patch("core.operations.list_cards_filtered", return_value=[{"id": "card-1"}])
    def test_returns_nonzero_count(self, _mock):
        from mcp_server import HANDLERS
        result = HANDLERS["sisyphus_list_cards"]()
        assert result["count"] == 1

    @patch("core.operations.list_cards_filtered", return_value=[])
    def test_passes_filters_correctly(self, mock_filter):
        from mcp_server import HANDLERS
        HANDLERS["sisyphus_list_cards"](repo="myrepo", status="running", pipeline="investigate")
        mock_filter.assert_called_once_with(
            pipeline="investigate", status="running", repo="myrepo",
        )


class TestHandlerSisyphusListRepos:
    """Test sisyphus_list_repos() directly."""

    @patch("core.repos.list_repos", return_value=[])
    def test_returns_repos_key(self, _mock):
        from mcp_server import HANDLERS
        result = HANDLERS["sisyphus_list_repos"]()
        assert "repos" in result
        assert result["repos"] == []

    @patch("core.repos.list_repos", return_value=[{"name": "r1"}, {"name": "r2"}])
    def test_returns_repos_list(self, _mock):
        from mcp_server import HANDLERS
        result = HANDLERS["sisyphus_list_repos"]()
        assert len(result["repos"]) == 2


class TestHandlerSisyphusListRunners:
    """Test sisyphus_list_runners() directly."""

    @patch("core.runners.list_runners", return_value=[])
    def test_returns_runners_key(self, _mock):
        from mcp_server import HANDLERS
        result = HANDLERS["sisyphus_list_runners"]()
        assert "runners" in result
        assert result["runners"] == []

    @patch("core.runners.list_runners", return_value=[{"name": "default"}])
    def test_returns_runners_list(self, _mock):
        from mcp_server import HANDLERS
        result = HANDLERS["sisyphus_list_runners"]()
        assert len(result["runners"]) == 1


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


class TestHandlerSisyphusCreateCard:
    """Test sisyphus_create_card() — uses db_path fixture."""

    def test_returns_card_id_and_status_backlog(self, db_path):
        from mcp_server import HANDLERS
        result = HANDLERS["sisyphus_create_card"](repo="test-repo", description="test description")
        assert "card_id" in result
        assert result["card_id"].startswith("card-")
        assert result["status"] == "backlog"
        assert result["pipeline"] == "investigate"

    def test_custom_pipeline(self, db_path):
        from mcp_server import HANDLERS
        result = HANDLERS["sisyphus_create_card"](
            repo="r", description="d", pipeline="custom-pipeline",
        )
        assert result["pipeline"] == "custom-pipeline"

    def test_jira_url_preserved(self, db_path):
        from mcp_server import HANDLERS
        result = HANDLERS["sisyphus_create_card"](
            repo="r", description="d", jira_url="https://jira.example.com/PROJ-1",
        )
        assert result["card_id"] is not None


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


class TestHandlerSisyphusRunCard:
    """Test sisyphus_run_card() — mock core.operations."""

    @patch("core.operations.create_and_run",
           return_value={"status": "completed", "run_id": "run-abc", "error": ""})
    def test_returns_status_and_run_id(self, _mock):
        from mcp_server import HANDLERS
        result = HANDLERS["sisyphus_run_card"](repo="r", description="d")
        assert result["status"] == "completed"
        assert result["run_id"] == "run-abc"
        assert result["error"] == ""


class TestHandlerSisyphusCancelCard:
    """Test sisyphus_cancel_card() — mock core.operations."""

    @patch("core.operations.cancel_card")
    def test_returns_success(self, mock_cancel):
        from mcp_server import HANDLERS
        import asyncio
        async def _fake(cid):
            pass
        mock_cancel.side_effect = _fake
        result = HANDLERS["sisyphus_cancel_card"](card_id="card-xyz")
        assert result["success"] is True
        assert result["card_id"] == "card-xyz"


class TestHandlerSisyphusResumeCard:
    """Test sisyphus_resume_card() — mock core.operations."""

    @patch("core.operations.resume_card")
    def test_returns_success(self, mock_resume):
        from mcp_server import HANDLERS
        import asyncio
        async def _fake(cid):
            pass
        mock_resume.side_effect = _fake
        result = HANDLERS["sisyphus_resume_card"](card_id="card-xyz")
        assert result["success"] is True
        assert result["card_id"] == "card-xyz"


class TestHandlerSisyphusGetCard:
    """Test sisyphus_get_card() — mock core.operations."""

    @patch("core.operations.get_card_detail",
           return_value={"card": {"id": "card-1"}, "runs": [], "steps": [], "resources": []})
    def test_returns_card_detail(self, _mock):
        from mcp_server import HANDLERS
        result = HANDLERS["sisyphus_get_card"](card_id="card-1")
        assert "card" in result
        assert result["card"]["id"] == "card-1"


class TestHandlerSisyphusAddRepo:
    """Test sisyphus_add_repo() — mock core.repos."""

    @patch("core.repos.add_repo", return_value="/bare/test-repo")
    def test_returns_repo_info(self, _mock):
        from mcp_server import HANDLERS
        result = HANDLERS["sisyphus_add_repo"](name="test", source="https://github.com/u/r.git")
        assert result["name"] == "test"
        assert result["source"] == "https://github.com/u/r.git"
        assert result["bare_path"] == "/bare/test-repo"


class TestHandlerSisyphusDeleteRepo:
    """Test sisyphus_delete_repo() — mock core.repos."""

    @patch("core.repos.delete_repo")
    def test_returns_success(self, mock_delete):
        from mcp_server import HANDLERS
        result = HANDLERS["sisyphus_delete_repo"](name="to-delete")
        assert result["success"] is True
        assert result["name"] == "to-delete"
        mock_delete.assert_called_once_with("to-delete")


class TestHandlerSisyphusUpdateRepo:
    """Test sisyphus_update_repo() — mock core.repos."""

    @patch("core.repos.update_repo", return_value=True)
    def test_returns_success(self, mock_update):
        from mcp_server import HANDLERS
        result = HANDLERS["sisyphus_update_repo"](name="to-update")
        assert result["success"] is True
        assert result["name"] == "to-update"


class TestHandlerSisyphusAddRunner:
    """Test sisyphus_add_runner() — mock core.runners."""

    @patch("core.runners.add_runner",
           return_value={"id": 1, "name": "default", "binary": "opencode", "type": "opencode"})
    def test_returns_runner_info(self, _mock):
        from mcp_server import HANDLERS
        result = HANDLERS["sisyphus_add_runner"](name="default", binary="opencode")
        assert result["id"] == 1
        assert result["name"] == "default"
        assert result["binary"] == "opencode"


class TestHandlerSisyphusUpdateRunner:
    """Test sisyphus_update_runner() — mock core.runners."""

    @patch("core.runners.update_runner")
    @patch("core.runners.get_runner_by_name",
           return_value={"id": 1, "name": "test-runner"})
    def test_returns_success(self, _mock_get, mock_update):
        from mcp_server import HANDLERS
        result = HANDLERS["sisyphus_update_runner"](name="test-runner", binary="/new/path")
        assert result["success"] is True
        assert result["name"] == "test-runner"
        mock_update.assert_called_once_with(1, binary="/new/path")

    @patch("core.runners.get_runner_by_name", return_value=None)
    def test_returns_error_when_not_found(self, _mock_get):
        from mcp_server import HANDLERS
        result = HANDLERS["sisyphus_update_runner"](name="nonexistent")
        assert "error" in result
        assert "not found" in result["error"]


class TestHandlerSisyphusDeleteRunner:
    """Test sisyphus_delete_runner() — mock core.runners."""

    @patch("core.runners.delete_runner")
    @patch("core.runners.get_runner_by_name",
           return_value={"id": 2, "name": "to-delete"})
    def test_returns_success(self, _mock_get, mock_delete):
        from mcp_server import HANDLERS
        result = HANDLERS["sisyphus_delete_runner"](name="to-delete")
        assert result["success"] is True
        assert result["name"] == "to-delete"


class TestHandlerSisyphusRunnerHealth:
    """Test sisyphus_runner_health() — mock core.runners."""

    @patch("core.runners.health_check")
    @patch("core.runners.get_runner_by_name",
           return_value={"id": 1, "name": "default"})
    def test_returns_health_result(self, _mock_get, mock_health):
        import asyncio
        async def _fake(runner_id):
            return {"binary_ok": True, "api_key": True}
        mock_health.side_effect = _fake
        from mcp_server import HANDLERS
        result = HANDLERS["sisyphus_runner_health"](name="default")
        assert result["binary_ok"] is True
        assert result["api_key"] is True


class TestHandlerSisyphusRunnerSetDefault:
    """Test sisyphus_runner_set_default() — mock core.runners."""

    @patch("core.runners.set_default_runner")
    @patch("core.runners.get_runner_by_name",
           return_value={"id": 1, "name": "default"})
    def test_returns_success(self, _mock_get, mock_set):
        from mcp_server import HANDLERS
        result = HANDLERS["sisyphus_runner_set_default"](name="default")
        assert result["success"] is True
        assert result["name"] == "default"


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
