"""
API endpoint tests for FastAPI web routes using TestClient.

All tests use the ``db_path`` fixture from conftest for DB setup.
Pipeline execution is mocked to avoid real subprocesses.
"""

import os
import sys
from unittest.mock import AsyncMock, patch, MagicMock

# Ensure sisyphus is importable (same as conftest)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Prevent start_scheduler from launching at import time.
_saved_scheduler = sys.modules.get('core.scheduler')
sys.modules['core.scheduler'] = MagicMock()
sys.modules['core.scheduler'].start_scheduler = MagicMock(return_value=None)

from fastapi.testclient import TestClient  # noqa: E402
from web.app import app  # noqa: E402

# Restore original module so other tests aren't affected
if _saved_scheduler is not None:
    sys.modules['core.scheduler'] = _saved_scheduler
else:
    del sys.modules['core.scheduler']

from core.operations import create_card_only  # noqa: E402
from core.runners import init_runners_table  # noqa: E402


# ── Fixture: TestClient ──────────────────────────────────────────────────


def _make_client():
    """Create a fresh TestClient — called inside a test so DB_PATH is set."""
    return TestClient(app)


# ── Tests: GET /api/cards (list) ─────────────────────────────────────────


def test_list_cards_empty(db_path):
    """Empty database returns empty list."""
    client = _make_client()
    response = client.get("/api/cards")
    assert response.status_code == 200
    assert response.json() == []


def test_list_cards_with_card(db_path):
    """After creating a card, the list contains it."""
    create_card_only("investigate", "test-repo", "test description")
    client = _make_client()
    response = client.get("/api/cards")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["description"] == "test description"


# ── Tests: POST /api/cards (create) ──────────────────────────────────────


def test_create_card(db_path):
    """Creating a card without run_now returns 200 and HX-Trigger cardCreated."""
    client = _make_client()
    response = client.post(
        "/api/cards",
        data={
            "pipeline": "investigate",
            "repo": "test-repo",
            "description": "test card",
        },
    )
    assert response.status_code == 200
    assert "cardCreated" in response.headers.get("HX-Trigger", "")


def test_create_card_run_now(db_path):
    """Creating a card with run_now=1 returns 200 and card is created."""
    client = _make_client()
    with patch(
        "core.pipeline.run_pipeline",
        new_callable=AsyncMock,
        return_value={"status": "completed"},
    ):
        response = client.post(
            "/api/cards",
            data={
                "pipeline": "investigate",
                "repo": "test-repo",
                "description": "run now card",
                "run_now": "1",
            },
        )
    assert response.status_code == 200
    assert "cardCreated" in response.headers.get("HX-Trigger", "")

    # Verify card was created
    r = client.get("/api/cards")
    cards = r.json()
    assert any(c["description"] == "run now card" for c in cards)


# ── Tests: GET /api/cards/{card_id}/detail ───────────────────────────────


def test_card_detail_existing(db_path):
    """Existing card returns 200 with card data in HTML."""
    card = create_card_only("investigate", "test-repo", "detail card")
    client = _make_client()
    response = client.get(f"/api/cards/{card['id']}/detail")
    assert response.status_code == 200
    assert "detail card" in response.text
    assert "investigate" in response.text


def test_card_detail_nonexistent(db_path):
    """Nonexistent card returns 200 with 'Not found' in HTML."""
    client = _make_client()
    response = client.get("/api/cards/nonexistent-card-id/detail")
    assert response.status_code == 200
    assert "Not found" in response.text


# ── Tests: POST /api/cards/{card_id}/run ─────────────────────────────────


def test_run_card(db_path, card):
    """Running an existing card returns 200 and HX-Trigger cardUpdated."""
    client = _make_client()
    with patch(
        "core.pipeline.run_pipeline",
        new_callable=AsyncMock,
        return_value={"status": "completed"},
    ):
        response = client.post(f"/api/cards/{card['id']}/run")
    assert response.status_code == 200
    assert "cardUpdated" in response.headers.get("HX-Trigger", "")


# ── Tests: POST /api/cards/{card_id}/cancel ──────────────────────────────


def test_cancel_card(db_path, card):
    """Cancelling an existing card returns 200 and HX-Trigger cardUpdated."""
    client = _make_client()
    with patch(
        "core.operations._kill_opencode_for_card",
        return_value=None,
    ):
        response = client.post(f"/api/cards/{card['id']}/cancel")
    assert response.status_code == 200
    assert "cardUpdated" in response.headers.get("HX-Trigger", "")


# ── Tests: POST /api/cards/{card_id}/resume ──────────────────────────────


def test_resume_card(db_path, run):
    """Resuming a card with a run returns 200 and HX-Trigger cardUpdated."""
    card_id = run["card_id"]
    client = _make_client()
    with patch(
        "core.pipeline.run_pipeline",
        new_callable=AsyncMock,
        return_value={"status": "completed"},
    ):
        response = client.post(f"/api/cards/{card_id}/resume")
    assert response.status_code == 200
    assert "cardUpdated" in response.headers.get("HX-Trigger", "")


# ── Tests: GET /api/cards/board (kanban board) ───────────────────────────


def test_board(db_path):
    """Board endpoint returns 200 with board HTML content."""
    client = _make_client()
    response = client.get("/api/cards/board")
    assert response.status_code == 200
    assert "backlog" in response.text.lower()


# ── Tests: GET /api/repos/list ───────────────────────────────────────────


def test_repos_list(db_path):
    """Repos list returns 200 with HTML content."""
    client = _make_client()
    mock_repos = [
        {"name": "test-repo", "source": "https://example.com/repo.git", "branch": "main"},
    ]
    with patch("core.repos.list_repos", return_value=mock_repos):
        response = client.get("/api/repos/list")
    assert response.status_code == 200
    # HTML template rendered successfully
    assert response.text


# ── Tests: GET /api/repos/health ─────────────────────────────────────────


def test_repos_health(db_path):
    """Repos health returns 200 with JSON containing total/cloned/pending."""
    client = _make_client()
    mock_repos = [
        {"name": "r1", "cloned": True},
        {"name": "r2", "cloned": False},
        {"name": "r3", "cloned": False},
    ]
    with patch("core.repos.list_repos", return_value=mock_repos):
        response = client.get("/api/repos/health")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 3
    assert data["cloned"] == 1
    assert data["pending"] == 2


# ── Tests: GET /api/runners/list ─────────────────────────────────────────


def test_runners_list(db_path):
    """Runners list returns 200 with HTML content."""
    init_runners_table()
    client = _make_client()
    response = client.get("/api/runners/list")
    assert response.status_code == 200
    assert response.text


# ── Tests: GET / (root redirect) ─────────────────────────────────────────


def test_root(db_path):
    """Root URL returns 200 with redirect HTML."""
    client = _make_client()
    response = client.get("/")
    assert response.status_code == 200
    assert "refresh" in response.text.lower()


# ── Tests: GET /dashboard ────────────────────────────────────────────────


def test_dashboard(db_path):
    """Dashboard returns 200 with HTML containing stats."""
    client = _make_client()
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "Total Cards" in response.text
