"""Tests for web/api.py — the daemon's JSON API (/api/v1).

This is the surface the MCP thin client (and any HTTP client) forwards to.
Pipeline execution is mocked; these tests exercise the JSON contract and that
each endpoint mutates state via core/.
"""
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

# Prevent start_scheduler from launching at import time (mirror test_web_routes).
_saved_scheduler = sys.modules.get("core.scheduler")
sys.modules["core.scheduler"] = MagicMock()
sys.modules["core.scheduler"].start_scheduler = MagicMock(return_value=None)

from fastapi.testclient import TestClient  # noqa: E402
from web.app import app  # noqa: E402

if _saved_scheduler is not None:
    sys.modules["core.scheduler"] = _saved_scheduler
else:
    del sys.modules["core.scheduler"]

from core.runners import init_runners_table  # noqa: E402


def _client():
    return TestClient(app)


# ── Health ──────────────────────────────────────────────────────────────


def test_health(db_path):
    r = _client().get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


# ── Cards ───────────────────────────────────────────────────────────────


def test_create_card_backlog(db_path):
    c = _client()
    r = c.post("/api/v1/cards", json={"repo": "sisyphus", "description": "d", "name": "N"})
    assert r.status_code == 200
    body = r.json()
    assert body["card_id"].startswith("card-")
    assert body["status"] == "backlog"
    assert body["name"] == "N"


def test_create_and_run_dispatches(db_path):
    """run:true fires create_and_run_async (background task in the daemon)."""
    c = _client()
    with patch("core.operations.create_and_run_async",
               new=AsyncMock(return_value="card-xyz")) as mock:
        r = c.post("/api/v1/cards", json={"repo": "sisyphus", "run": True})
    assert r.status_code == 200
    assert r.json() == {"card_id": "card-xyz", "status": "running"}
    mock.assert_awaited_once()


def test_create_list_get_roundtrip(db_path):
    c = _client()
    cid = c.post("/api/v1/cards", json={"repo": "sisyphus", "description": "roundtrip"}).json()["card_id"]
    listed = c.get("/api/v1/cards").json()
    assert listed["count"] >= 1
    assert any(card["id"] == cid for card in listed["cards"])
    detail = c.get(f"/api/v1/cards/{cid}").json()
    assert detail["card"]["id"] == cid
    assert "activities" in detail


def test_get_unknown_card_404(db_path):
    assert _client().get("/api/v1/cards/nope").status_code == 404


def test_run_card_non_blocking(db_path):
    c = _client()
    cid = c.post("/api/v1/cards", json={"repo": "sisyphus"}).json()["card_id"]
    with patch("core.operations.run_card", new=AsyncMock()) as mock:
        r = c.post(f"/api/v1/cards/{cid}/run")
    assert r.status_code == 200
    assert r.json() == {"card_id": cid, "status": "running"}


def test_run_unknown_card_404(db_path):
    with patch("core.operations.run_card", new=AsyncMock()):
        assert _client().post("/api/v1/cards/nope/run").status_code == 404


def test_comment_creates_human_activity(db_path):
    from core.state import get_activities_for_card

    c = _client()
    cid = c.post("/api/v1/cards", json={"repo": "sisyphus"}).json()["card_id"]
    r = c.post(f"/api/v1/cards/{cid}/comments", json={"text": "looks good"})
    assert r.status_code == 200
    acts = get_activities_for_card(cid)
    assert len(acts) == 1
    assert acts[0]["text"] == "looks good"
    assert acts[0]["author_type"] == "human"


def test_empty_comment_rejected(db_path):
    c = _client()
    cid = c.post("/api/v1/cards", json={"repo": "sisyphus"}).json()["card_id"]
    assert c.post(f"/api/v1/cards/{cid}/comments", json={"text": "   "}).status_code == 422


# ── Repos ───────────────────────────────────────────────────────────────


def test_list_repos(db_path):
    r = _client().get("/api/v1/repos")
    assert r.status_code == 200
    assert "repos" in r.json()


# ── Runners ─────────────────────────────────────────────────────────────


def test_runner_crud(db_path):
    init_runners_table()
    c = _client()
    assert c.get("/api/v1/runners").status_code == 200

    added = c.post("/api/v1/runners", json={"name": "t", "binary": "echo"}).json()
    assert added["name"] == "t"

    assert c.post("/api/v1/runners/t/set-default").json()["success"] is True
    assert c.patch("/api/v1/runners/t", json={"binary": "cat"}).json()["success"] is True
    assert c.request("DELETE", "/api/v1/runners/t").json()["success"] is True


def test_runner_not_found_404(db_path):
    init_runners_table()
    assert _client().post("/api/v1/runners/nope/set-default").status_code == 404
