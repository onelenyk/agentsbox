"""Tests for the /api/v1/agents endpoints."""
from fastapi.testclient import TestClient
from web.app import app

from core.agents import init_agents_table


def _client(db_path):
    init_agents_table()
    return TestClient(app)


def test_list_seeded_agents(db_path):
    r = _client(db_path).get("/api/v1/agents")
    assert r.status_code == 200
    names = {a["name"] for a in r.json()["agents"]}
    assert {"explorer", "engineer", "reviewer"} <= names


def test_tool_catalog(db_path):
    r = _client(db_path).get("/api/v1/agents/tool-catalog")
    tools = {t["name"] for t in r.json()["tools"]}
    assert {"get_context", "commit", "finish", "approve"} <= tools


def test_create_update_delete_agent(db_path):
    c = _client(db_path)
    created = c.post("/api/v1/agents", json={
        "name": "docs-writer", "persona": "You write docs.",
        "tools": ["get_context", "comment", "finish"],
    })
    assert created.status_code == 200
    assert created.json()["agent"]["tools"] == ["get_context", "comment", "finish"]

    patched = c.patch("/api/v1/agents/docs-writer", json={"model": "m2"})
    assert patched.json()["agent"]["model"] == "m2"

    assert c.request("DELETE", "/api/v1/agents/docs-writer").json()["success"] is True
    assert c.request("DELETE", "/api/v1/agents/docs-writer").status_code == 404


def test_duplicate_name_conflicts(db_path):
    c = _client(db_path)
    c.post("/api/v1/agents", json={"name": "dup", "tools": ["finish"]})
    assert c.post("/api/v1/agents", json={"name": "dup", "tools": ["finish"]}).status_code == 409


def test_cannot_delete_builtin(db_path):
    assert _client(db_path).request("DELETE", "/api/v1/agents/engineer").status_code == 422
