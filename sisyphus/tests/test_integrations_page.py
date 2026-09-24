"""Tests for the Integrations page (GET /integrations, GET /api/integrations/view, POST /api/integrations/jira/check)."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="function")
def client(db_path):
    """FastAPI test client with fresh DB."""
    from web.app import app
    return TestClient(app)


def test_integrations_page_renders(client):
    """The /integrations page should render successfully."""
    resp = client.get("/integrations")
    assert resp.status_code == 200
    assert "Integrations" in resp.text
    assert "integrations-view" in resp.text


def test_integrations_view_shows_recorded_event(client, monkeypatch):
    """The view fragment should show a recorded integration event with task link and subject link."""
    from core import integrations, jira

    # Mock jira.configured to return True
    monkeypatch.setattr(jira, "configured", lambda: True)

    # Record a test event
    task_id = "task-abc123"
    integrations.record(
        "jira",
        "search",
        f"agent:engineer@{task_id}",
        subject="PROJ-123",
        detail="fetched 5 issues",
        count=5,
        ok=True,
        task_id=task_id
    )

    # Get the view
    resp = client.get("/api/integrations/view?provider=jira&errors=0&limit=100")
    assert resp.status_code == 200

    # Check the event is shown
    assert "PROJ-123" in resp.text
    assert "fetched 5 issues" in resp.text
    assert task_id[:8] in resp.text or task_id in resp.text


def test_integrations_view_filters_by_provider(client, monkeypatch):
    """The provider filter should work."""
    from core import integrations, jira

    monkeypatch.setattr(jira, "configured", lambda: True)

    # Record Jira events
    integrations.record("jira", "search", "human:ui", subject="PROJ-1", detail="test 1", count=1)
    integrations.record("jira", "transition", "human:ui", subject="PROJ-2", detail="test 2", count=1)

    # Filter by Jira
    resp = client.get("/api/integrations/view?provider=jira&errors=0&limit=100")
    assert resp.status_code == 200
    assert "test 1" in resp.text
    assert "test 2" in resp.text


def test_integrations_view_filters_errors_only(client, monkeypatch):
    """The errors-only filter should hide ok rows."""
    from core import integrations, jira

    monkeypatch.setattr(jira, "configured", lambda: True)

    # Record one ok event and one error event
    integrations.record("jira", "search", "human:ui", subject="OK-1", detail="ok event", count=1, ok=True)
    integrations.record("jira", "transition", "human:ui", subject="ERR-1", detail="error event", count=0, ok=False, error="timeout")

    # Get all events
    resp = client.get("/api/integrations/view?provider=&errors=0&limit=100")
    assert resp.status_code == 200
    assert "ok event" in resp.text
    assert "error event" in resp.text

    # Get only errors
    resp = client.get("/api/integrations/view?provider=&errors=1&limit=100")
    assert resp.status_code == 200
    assert "ok event" not in resp.text
    assert "error event" in resp.text


def test_integrations_check_button_connected(client, monkeypatch):
    """The check button should show connected name when successful."""
    from core import jira

    # Mock myself to return a valid connection
    def mock_myself(actor):
        return {"displayName": "Test User", "emailAddress": "test@example.com"}

    monkeypatch.setattr(jira, "myself", mock_myself)

    resp = client.post("/api/integrations/jira/check")
    assert resp.status_code == 200
    assert "Test User" in resp.text or "Connected" in resp.text.lower()


def test_integrations_check_button_error(client, monkeypatch):
    """The check button should show error when Jira raises."""
    from core import jira

    # Mock myself to raise an error
    def mock_myself_error(actor):
        raise jira.JiraError("Connection refused")

    monkeypatch.setattr(jira, "myself", mock_myself_error)

    resp = client.post("/api/integrations/jira/check")
    assert resp.status_code == 200
    assert "Connection refused" in resp.text


def test_integrations_actor_rendering(client, monkeypatch):
    """Actor tags should be rendered correctly."""
    from core import integrations, jira

    monkeypatch.setattr(jira, "configured", lambda: True)

    # Record events with different actors
    integrations.record("jira", "search", "agent:engineer@task-1a2b3c4d", subject="PROJ-1", detail="agent action", count=1, task_id="task-1a2b3c4d")
    integrations.record("jira", "transition", "daemon:jira-poll", subject="PROJ-2", detail="daemon action", count=1)
    integrations.record("jira", "comment", "human:ui", subject="PROJ-3", detail="human action", count=1)

    resp = client.get("/api/integrations/view?provider=jira&errors=0&limit=100")
    assert resp.status_code == 200

    # Check agent actor is shown with link
    assert "engineer" in resp.text
    assert "task-1a2b3c4d" in resp.text or "task-1a" in resp.text

    # Check daemon actor is shown
    assert "jira-poll" in resp.text

    # Check human actor is shown
    assert "you" in resp.text.lower() or "UI" in resp.text
