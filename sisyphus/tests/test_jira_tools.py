"""Tests for Jira tools in web/agent_mcp.py.

Verifies agent access to Jira operations: reading issues, transitioning status,
commenting. Every tool call is journaled under the agent's name + task.
"""
import json
from unittest.mock import patch

from fastapi.testclient import TestClient
from web.app import app

from core import agents, integrations, sessions, store


def _client():
    return TestClient(app)


def _rpc(client, token, method, params=None, req_id=1):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    body = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp/agent", json=body, headers=headers)


def _call(client, token, name, args=None):
    return _rpc(client, token, "tools/call", {"name": name, "arguments": args or {}}).json()


def _session(db_path, role="engineer", task_origin=None):
    sessions.clear()
    task = store.create_task("do the thing", repo="sisyphus", origin=task_origin)
    token = sessions.mint(task["id"], role)
    return task["id"], token, task


def _grant(role, *tools):
    """Grant additional tools to a role for testing."""
    current = agents.agent_tools(role)
    new = list(set(current) | set(tools))
    agents.update_agent(role, tools=new)


# ── jira_issue ──────────────────────────────────────────────────────────

def test_jira_issue_reads_issue(db_path):
    """jira_issue returns the shaped issue."""
    task_id, token, _ = _session(db_path)
    _grant("engineer", "jira_issue")

    mock_issue = {
        "key": "ABC-123",
        "url": "https://jira.example.com/browse/ABC-123",
        "fields": {
            "summary": "Fix bug",
            "description": "A description",
            "issuetype": "Bug",
            "priority": "High",
            "status": "Open",
            "labels": ["urgent"],
            "components": ["backend"],
            "created": "2026-01-01T00:00:00Z",
            "updated": "2026-01-02T00:00:00Z",
            "reporter": "alice",
            "assignee": "bob",
        },
    }

    with patch("core.jira.configured", return_value=True):
        with patch("core.jira.get_issue", return_value=mock_issue) as mock_get:
            out = _call(_client(), token, "jira_issue", {"key": "ABC-123"})

    assert out["result"]["isError"] is False
    result = json.loads(out["result"]["content"][0]["text"])
    assert result["key"] == "ABC-123"
    assert result["url"] == "https://jira.example.com/browse/ABC-123"
    assert result["fields"]["summary"] == "Fix bug"
    # Verify the actor and task_id were passed
    mock_get.assert_called_once()
    call_args = mock_get.call_args
    assert call_args[0][1] == "agent:engineer@" + task_id  # actor
    assert call_args[1]["task_id"] == task_id


def test_jira_issue_from_origin(db_path):
    """jira_issue without key extracts the key from task origin."""
    task_id, token, _ = _session(db_path, task_origin="jira:VAN-42")
    _grant("engineer", "jira_issue")

    mock_issue = {"key": "VAN-42", "url": "", "fields": {}}

    with patch("core.jira.configured", return_value=True):
        with patch("core.jira.get_issue", return_value=mock_issue) as mock_get:
            out = _call(_client(), token, "jira_issue", {})

    assert out["result"]["isError"] is False
    # Verify get_issue was called with the key from origin
    call_args = mock_get.call_args
    assert call_args[0][0] == "VAN-42"


def test_jira_issue_no_key_no_origin(db_path):
    """jira_issue without key and no origin returns error."""
    task_id, token, _ = _session(db_path)
    _grant("engineer", "jira_issue")

    with patch("core.jira.configured", return_value=True):
        out = _call(_client(), token, "jira_issue", {})

    assert out["result"]["isError"] is True
    result = json.loads(out["result"]["content"][0]["text"])
    assert "error" in result
    assert "no key" in result["error"].lower()


def test_jira_issue_unconfigured(db_path):
    """jira_issue when Jira is not configured returns the error."""
    task_id, token, _ = _session(db_path)
    _grant("engineer", "jira_issue")

    with patch("core.jira.configured", return_value=False):
        out = _call(_client(), token, "jira_issue", {"key": "ABC-123"})

    assert out["result"]["isError"] is True
    result = json.loads(out["result"]["content"][0]["text"])
    assert "not configured" in result["error"].lower()


# ── jira_transition ─────────────────────────────────────────────────────

def test_jira_transition_moves_issue_and_posts_note(db_path):
    """jira_transition moves the issue and posts a thread note."""
    task_id, token, task = _session(db_path, task_origin="jira:ABC-123")
    _grant("engineer", "jira_transition")

    mock_result = {"key": "ABC-123", "status": "In Progress"}

    with patch("core.jira.configured", return_value=True):
        with patch("core.jira.transition", return_value=mock_result) as mock_trans:
            out = _call(_client(), token, "jira_transition", {"to_status": "In Progress"})

    assert out["result"]["isError"] is False
    result = json.loads(out["result"]["content"][0]["text"])
    assert result["key"] == "ABC-123"
    assert result["status"] == "In Progress"

    # Verify the actor and task_id were passed
    call_args = mock_trans.call_args
    assert call_args[0][2] == f"agent:engineer@{task_id}"  # actor
    assert call_args[1]["task_id"] == task_id

    # Verify thread note was posted
    thread = store.get_thread(task_id)
    notes = [m for m in thread if m["kind"] == "note"]
    assert any("moved ABC-123 to In Progress" in m["text"] for m in notes)


def test_jira_transition_from_origin(db_path):
    """jira_transition without key extracts the key from task origin."""
    task_id, token, _ = _session(db_path, task_origin="jira:VAN-42")
    _grant("engineer", "jira_transition")

    mock_result = {"key": "VAN-42", "status": "Done"}

    with patch("core.jira.configured", return_value=True):
        with patch("core.jira.transition", return_value=mock_result):
            out = _call(_client(), token, "jira_transition", {"to_status": "Done"})

    assert out["result"]["isError"] is False


def test_jira_transition_no_key_no_origin_error(db_path):
    """jira_transition without key and no origin returns error, nothing journaled."""
    task_id, token, _ = _session(db_path)
    _grant("engineer", "jira_transition")

    with patch("core.jira.configured", return_value=True):
        out = _call(_client(), token, "jira_transition", {"to_status": "In Progress"})

    assert out["result"]["isError"] is True
    result = json.loads(out["result"]["content"][0]["text"])
    assert "error" in result
    assert "no key" in result["error"].lower()

    # Verify nothing was journaled
    events = integrations.list_events("jira", limit=100)
    assert len(events) == 0


def test_jira_transition_requires_to_status(db_path):
    """jira_transition requires to_status."""
    task_id, token, _ = _session(db_path, task_origin="jira:ABC-123")
    _grant("engineer", "jira_transition")

    with patch("core.jira.configured", return_value=True):
        out = _call(_client(), token, "jira_transition", {})

    assert out["result"]["isError"] is True


def test_jira_transition_unconfigured(db_path):
    """jira_transition when Jira is not configured returns the error."""
    task_id, token, _ = _session(db_path, task_origin="jira:ABC-123")
    _grant("engineer", "jira_transition")

    with patch("core.jira.configured", return_value=False):
        out = _call(_client(), token, "jira_transition", {"to_status": "Done"})

    assert out["result"]["isError"] is True
    result = json.loads(out["result"]["content"][0]["text"])
    assert "not configured" in result["error"].lower()


def test_jira_transition_with_explicit_key(db_path):
    """jira_transition can override the task's origin with an explicit key."""
    task_id, token, _ = _session(db_path, task_origin="jira:ABC-123")
    _grant("engineer", "jira_transition")

    mock_result = {"key": "XYZ-999", "status": "Done"}

    with patch("core.jira.configured", return_value=True):
        with patch("core.jira.transition", return_value=mock_result) as mock_trans:
            out = _call(_client(), token, "jira_transition", {"to_status": "Done", "key": "XYZ-999"})

    assert out["result"]["isError"] is False
    # Verify the explicit key was used
    call_args = mock_trans.call_args
    assert call_args[0][0] == "XYZ-999"


# ── jira_comment ────────────────────────────────────────────────────────

def test_jira_comment_posts_comment_and_note(db_path):
    """jira_comment comments on the issue and posts a thread note."""
    task_id, token, _ = _session(db_path, task_origin="jira:ABC-123")
    _grant("engineer", "jira_comment")

    mock_result = {"key": "ABC-123", "comment_id": "c123"}

    with patch("core.jira.configured", return_value=True):
        with patch("core.jira.comment", return_value=mock_result) as mock_comment:
            out = _call(_client(), token, "jira_comment", {"text": "This is fixed now"})

    assert out["result"]["isError"] is False
    result = json.loads(out["result"]["content"][0]["text"])
    assert result["key"] == "ABC-123"
    assert result["comment_id"] == "c123"

    # Verify the actor and task_id were passed
    call_args = mock_comment.call_args
    assert call_args[0][2] == f"agent:engineer@{task_id}"  # actor
    assert call_args[1]["task_id"] == task_id

    # Verify thread note was posted
    thread = store.get_thread(task_id)
    notes = [m for m in thread if m["kind"] == "note"]
    assert any("commented on ABC-123" in m["text"] for m in notes)


def test_jira_comment_from_origin(db_path):
    """jira_comment without key extracts the key from task origin."""
    task_id, token, _ = _session(db_path, task_origin="jira:VAN-42")
    _grant("engineer", "jira_comment")

    mock_result = {"key": "VAN-42", "comment_id": "c456"}

    with patch("core.jira.configured", return_value=True):
        with patch("core.jira.comment", return_value=mock_result) as mock_comment:
            out = _call(_client(), token, "jira_comment", {"text": "Done!"})

    assert out["result"]["isError"] is False
    call_args = mock_comment.call_args
    assert call_args[0][0] == "VAN-42"


def test_jira_comment_requires_text(db_path):
    """jira_comment requires text."""
    task_id, token, _ = _session(db_path, task_origin="jira:ABC-123")
    _grant("engineer", "jira_comment")

    with patch("core.jira.configured", return_value=True):
        out = _call(_client(), token, "jira_comment", {})

    assert out["result"]["isError"] is True


def test_jira_comment_no_key_no_origin_error(db_path):
    """jira_comment without key and no origin returns error."""
    task_id, token, _ = _session(db_path)
    _grant("engineer", "jira_comment")

    with patch("core.jira.configured", return_value=True):
        out = _call(_client(), token, "jira_comment", {"text": "This is great!"})

    assert out["result"]["isError"] is True
    result = json.loads(out["result"]["content"][0]["text"])
    assert "error" in result


def test_jira_comment_unconfigured(db_path):
    """jira_comment when Jira is not configured returns the error."""
    task_id, token, _ = _session(db_path, task_origin="jira:ABC-123")
    _grant("engineer", "jira_comment")

    with patch("core.jira.configured", return_value=False):
        out = _call(_client(), token, "jira_comment", {"text": "Nice work!"})

    assert out["result"]["isError"] is True
    result = json.loads(out["result"]["content"][0]["text"])
    assert "not configured" in result["error"].lower()


# ── Journaling ──────────────────────────────────────────────────────────

def test_jira_tools_journal_with_agent_actor(db_path, monkeypatch):
    """Every Jira tool call journals with actor agent:<role>@<task_id>."""
    import json

    # Set up Jira config
    jira_values = {"jira.base_url": "https://x.atlassian.net", "jira.email": "me@x.test",
                   "jira.token": "SECRET-TOKEN"}
    monkeypatch.setattr("core.config.get_config",
                        lambda key=None, default=None: jira_values.get(key, default))

    task_id, token, _ = _session(db_path, task_origin="jira:ABC-123")
    _grant("engineer", "jira_issue", "jira_transition", "jira_comment")

    # Response helper that implements context manager
    class _Resp:
        def __init__(self, obj):
            self.raw = json.dumps(obj).encode()
        def read(self):
            return self.raw
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        if "/transitions" in req.full_url and req.get_method() == "GET":
            return _Resp({"transitions": [{"id": "1", "name": "Start", "to": {"name": "Done"}}]})
        elif "/comment" in req.full_url and req.get_method() == "POST":
            return _Resp({"id": "c1"})
        elif "/issue/" in req.full_url and req.get_method() == "GET":
            return _Resp({"key": "ABC-123", "id": "1", "fields": {
                "summary": "Test", "status": {"name": "Open"},
                "description": {}, "issuetype": {"name": "Task"}, "priority": {"name": "Normal"},
                "labels": [], "components": [], "created": "2026-01-01", "updated": "2026-01-01",
                "reporter": {"displayName": "alice"}, "assignee": {"displayName": "bob"}
            }})
        elif req.get_method() == "POST" and "/transitions" in req.full_url:
            return _Resp({})
        return _Resp({})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    _call(_client(), token, "jira_issue", {})
    _call(_client(), token, "jira_transition", {"to_status": "Done"})
    _call(_client(), token, "jira_comment", {"text": "Done"})

    expected_actor = f"agent:engineer@{task_id}"
    events = integrations.list_events("jira", limit=100)

    assert len(events) >= 3
    for event in events[-3:]:
        assert event["actor"] == expected_actor
        assert event["task_id"] == task_id


# ── Migration ───────────────────────────────────────────────────────────

def test_migration_appends_tools_to_builtin_agent(db_path):
    """The migration appends new tools to built-in agents."""
    from core.agents import init_agents_table
    from core.db import get_conn

    # Simulate an old database with engineer without the new tools
    conn = get_conn()
    conn.execute("DELETE FROM agents WHERE name = 'engineer'")

    # Insert old engineer without the Jira tools
    old_tools = ["get_context", "comment", "attach", "commit", "open_mr", "finish"]
    import json
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO agents (id, name, persona, tools, runner_id, model,
           kind, enabled, is_builtin, created_at, updated_at)
           VALUES (?, ?, ?, ?, NULL, '', 'worker', 1, 1, ?, ?)""",
        ("agent-oldeng", "engineer", "old persona", json.dumps(old_tools), now, now)
    )
    conn.commit()
    conn.close()

    # Run the migration
    init_agents_table()

    # Verify the tools were appended
    engineer = agents.get_agent("engineer")
    assert engineer is not None
    assert "jira_issue" in engineer["tools"]
    assert "jira_transition" in engineer["tools"]
    assert "jira_comment" in engineer["tools"]
    # Verify old tools are still there
    assert "commit" in engineer["tools"]
    assert "finish" in engineer["tools"]


def test_migration_leaves_custom_agents_alone(db_path):
    """The migration does not touch non-builtin agents."""
    from core.agents import init_agents_table, add_agent

    # Add a custom (non-builtin) agent with specific tools
    custom = add_agent("my-agent", persona="custom", tools=["get_context", "finish"])
    custom_tools = custom["tools"].copy()

    # Run the migration
    init_agents_table()

    # Verify the custom agent was not modified
    custom_after = agents.get_agent("my-agent")
    assert custom_after["tools"] == custom_tools
    assert "jira_issue" not in custom_after["tools"]
