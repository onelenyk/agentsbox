"""Tests for Jira issue intake — creating tasks from Jira issues."""
import pytest
from datetime import datetime, timezone
from core import jira_intake, store, integrations
from core.jira import JiraError


def _issue(key, summary, description="test desc", issuetype="Bug", priority="High", status="To Do", updated="2024-01-15T15:30:00.000Z"):
    """Helper to build a canned Jira issue."""
    return {
        "key": key,
        "id": f"id-{key}",
        "url": f"https://jira.example.com/browse/{key}",
        "fields": {
            "summary": summary,
            "description": description,
            "issuetype": issuetype,
            "priority": priority,
            "status": status,
            "labels": ["bug", "urgent"],
            "components": ["backend"],
            "created": "2024-01-01T10:00:00.000Z",
            "updated": updated,
            "reporter": "alice@example.com",
            "assignee": "bob@example.com",
        }
    }


class TestGoalFor:
    """Test goal_for shape for different repo modes."""

    def test_goal_for_write_repo(self, db_path):
        """goal_for includes fix instructions for write repos."""
        repo = {"name": "myrepo", "mode": None}
        issue = _issue("TSK-1", "Fix the login bug", "User cannot log in")
        goal = jira_intake.goal_for(issue, repo)

        assert "TSK-1 · Fix the login bug" in goal
        assert "Jira: https://jira.example.com/browse/TSK-1" in goal
        assert "Bug" in goal
        assert "Priority: High" in goal
        assert "Status: To Do" in goal
        assert "Labels: bug, urgent" in goal
        assert "Components: backend" in goal
        assert "Reporter: alice@example.com" in goal
        assert "Description (from Jira):" in goal
        assert "User cannot log in" in goal
        assert "fix the cause, add a test, commit and open a merge request" in goal

    def test_goal_for_read_only_repo(self, db_path, monkeypatch):
        """goal_for includes investigation instructions for read-only repos."""
        # Mock is_read_only to return True
        monkeypatch.setattr(
            "core.repos.is_read_only",
            lambda name: name == "readonly-repo"
        )
        repo = {"name": "readonly-repo", "mode": "read-only"}
        issue = _issue("TSK-2", "Investigate performance", "High memory usage")
        goal = jira_intake.goal_for(issue, repo)

        assert "Repo is read-only" in goal
        assert "classify it (module, platform, severity, effort)" in goal
        assert "Do not change files" in goal

    def test_goal_for_empty_fields(self, db_path):
        """goal_for handles empty fields gracefully."""
        repo = {"name": "myrepo"}
        issue = {
            "key": "TSK-3",
            "url": "",
            "fields": {
                "summary": "Something",
                "description": "",
                "issuetype": "",
                "priority": "",
                "status": "",
                "labels": [],
                "components": [],
                "reporter": "",
                "assignee": "",
                "created": "",
                "updated": "",
            }
        }
        goal = jira_intake.goal_for(issue, repo)

        assert "TSK-3 · Something" in goal
        assert "Description (from Jira):" in goal
        assert "(none)" in goal


class TestImportIssues:
    """Test importing issues into tasks."""

    def test_import_creates_tasks(self, db_path, monkeypatch):
        """import_issues creates tasks with the right origin and title."""
        # Mock jira.search to return canned issues
        issues = [
            _issue("ABC-1", "First bug"),
            _issue("ABC-2", "Second bug"),
        ]
        monkeypatch.setattr("core.jira_intake.jira.search", lambda *a, **k: issues)

        # Mock emit to avoid orchestrator
        monkeypatch.setattr("core.orchestrator.emit", lambda *a: None)

        result = jira_intake.import_issues("project=ABC", "test-repo", "human:ui", max_results=50)

        assert len(result["created"]) == 2
        assert "ABC-1" in result["created"]
        assert "ABC-2" in result["created"]
        assert len(result["skipped"]) == 0
        assert len(result["tasks"]) == 2

        # Check that tasks were created with the right origin
        task1 = jira_intake.find_by_key("ABC-1")
        task2 = jira_intake.find_by_key("ABC-2")
        assert task1 is not None
        assert task2 is not None
        assert task1["origin"] == "jira:ABC-1"
        assert task2["origin"] == "jira:ABC-2"
        assert "ABC-1 · First bug" in task1["title"]
        assert "ABC-2 · Second bug" in task2["title"]

    def test_import_skips_existing(self, db_path, monkeypatch):
        """import_issues skips issues that were already imported."""
        # Create an existing task with origin
        existing_task = store.create_task("old goal", "test-repo", origin="jira:ABC-1")

        # Mock jira.search
        issues = [_issue("ABC-1", "First bug"), _issue("ABC-2", "New bug")]
        monkeypatch.setattr("core.jira_intake.jira.search", lambda *a, **k: issues)
        monkeypatch.setattr("core.orchestrator.emit", lambda *a: None)

        result = jira_intake.import_issues("project=ABC", "test-repo", "human:ui")

        assert len(result["created"]) == 1
        assert "ABC-2" in result["created"]
        assert len(result["skipped"]) == 1
        assert "ABC-1" in result["skipped"]
        assert len(result["tasks"]) == 1

    def test_import_journals_one_row(self, db_path, monkeypatch):
        """import_issues journals one row per import (not per issue)."""
        issues = [_issue("ABC-1", "Bug"), _issue("ABC-2", "Bug")]
        monkeypatch.setattr("core.jira_intake.jira.search", lambda *a, **k: issues)
        monkeypatch.setattr("core.orchestrator.emit", lambda *a: None)

        jira_intake.import_issues("project=ABC", "test-repo", "human:ui")

        # Check journal entries
        events = integrations.list_events(provider="jira", limit=10)
        create_tasks_events = [e for e in events if e["action"] == "create_tasks"]
        assert len(create_tasks_events) >= 1

        # Get the most recent one
        ev = create_tasks_events[0]
        assert ev["count"] == 2
        assert ev["detail"] == "created 2, skipped 0"
        assert ev["ok"] == 1
        meta = ev.get("meta", {})
        assert len(meta.get("created", [])) == 2
        assert len(meta.get("skipped", [])) == 0

    def test_import_handles_jira_error(self, db_path, monkeypatch):
        """import_issues journals and re-raises JiraError."""
        error = JiraError("Connection timeout")
        monkeypatch.setattr("core.jira_intake.jira.search", lambda *a, **k: (_ for _ in ()).throw(error))

        with pytest.raises(JiraError):
            jira_intake.import_issues("bad jql", "test-repo", "human:ui")

        # Check error was journaled
        events = integrations.list_events(provider="jira", limit=10)
        assert any(e["ok"] == 0 for e in events)


class TestPollOnce:
    """Test single polling pass."""

    def test_poll_once_disabled(self, db_path, monkeypatch):
        """poll_once returns empty when jira.intake.enabled is false."""
        monkeypatch.setenv("SISYPHUS_CONFIG", '{"jira": {"intake": {"enabled": false}}}')
        from core.config import reload_config
        reload_config()

        result = jira_intake.poll_once()
        assert result == {"created": [], "skipped": [], "tasks": []}

    def test_poll_once_not_configured(self, db_path, monkeypatch):
        """poll_once returns empty when jql or repo is not configured."""
        from core.config import reload_config
        monkeypatch.setenv("SISYPHUS_CONFIG", '{"jira": {"intake": {"enabled": true, "jql": ""}}}')
        reload_config()

        result = jira_intake.poll_once()
        assert result == {"created": [], "skipped": [], "tasks": []}



class TestPollForever:
    """Test the polling loop (basic structure)."""

    @pytest.mark.asyncio
    async def test_poll_forever_registers_heartbeat(self, db_path, monkeypatch):
        """poll_forever registers a heartbeat."""
        monkeypatch.setenv(
            "SISYPHUS_CONFIG",
            '{"jira": {"intake": {"enabled": false}}}'
        )
        from core.config import reload_config
        reload_config()
        from core import heartbeat
        heartbeat.clear()

        # Run poll_forever just to register (then it will return early)
        import asyncio
        task = asyncio.create_task(jira_intake.poll_forever())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Check heartbeat was registered
        watchers = {w["name"] for w in heartbeat.list_all()}
        assert "jira poll" in watchers


class TestRoute:
    """Test the HTTP route for importing issues on demand."""

    def test_import_route_success(self, db_path, monkeypatch):
        """POST /api/integrations/jira/import returns 200 with result."""
        from fastapi.testclient import TestClient
        from web.app import app

        # Mock import_issues
        def mock_import(jql, repo, actor, max_results=200):
            return {"created": ["TSK-1"], "skipped": [], "tasks": ["task-abc123"]}

        monkeypatch.setattr("core.jira_intake.import_issues", mock_import)

        client = TestClient(app)
        resp = client.post(
            "/api/integrations/jira/import",
            data={"jql": "project=TEST", "repo": "test-repo", "max_results": "50"}
        )

        assert resp.status_code == 200
        assert "Created 1 task" in resp.text
        assert "flash" in resp.text

    def test_import_route_jira_error(self, db_path, monkeypatch):
        """POST /api/integrations/jira/import renders JiraError as text."""
        from fastapi.testclient import TestClient
        from web.app import app
        from core.jira import JiraError

        error = JiraError("Jira is not configured")
        monkeypatch.setattr(
            "core.jira_intake.import_issues",
            lambda *a, **k: (_ for _ in ()).throw(error)
        )

        client = TestClient(app)
        resp = client.post(
            "/api/integrations/jira/import",
            data={"jql": "project=TEST", "repo": "test-repo"}
        )

        assert resp.status_code == 200  # Not a 500
        assert "error" in resp.text.lower()
        assert "Jira is not configured" in resp.text


class TestFindByKey:
    """Test task lookup by Jira key."""

    def test_find_by_key_exists(self, db_path, monkeypatch):
        """find_by_key returns the task when origin exists."""
        monkeypatch.setattr("core.orchestrator.emit", lambda *a: None)

        # Create a task with Jira origin
        task = store.create_task(
            "test goal",
            "test-repo",
            title="TSK-1 · Some issue",
            origin="jira:TSK-1"
        )

        found = jira_intake.find_by_key("TSK-1")
        assert found is not None
        assert found["id"] == task["id"]
        assert found["origin"] == "jira:TSK-1"

    def test_find_by_key_not_found(self, db_path):
        """find_by_key returns None when no task has that origin."""
        found = jira_intake.find_by_key("NONEXISTENT-123")
        assert found is None
