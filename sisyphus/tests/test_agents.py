"""Tests for core/agents.py — the configurable Agent entity."""
import pytest

from core.agents import (
    add_agent,
    agent_tools,
    delete_agent,
    get_agent,
    init_agents_table,
    list_agents,
    update_agent,
)


@pytest.fixture
def agents(db_path):
    init_agents_table()
    return db_path


class TestSeeding:
    def test_builtins_seeded_from_roles(self, agents):
        names = {a["name"] for a in list_agents()}
        assert {"explorer", "engineer", "reviewer"} <= names

    def test_builtin_matches_role_tools(self, agents):
        from core.roles import ROLES
        for name, spec in ROLES.items():
            assert agent_tools(name) == spec["tools"]
            assert get_agent(name)["is_builtin"] == 1

    def test_seed_is_idempotent(self, agents):
        before = len(list_agents())
        init_agents_table()   # second call must not duplicate
        assert len(list_agents()) == before


class TestCrud:
    def test_add_custom_agent(self, agents):
        a = add_agent("docs-writer", persona="You write docs.",
                      tools=["get_context", "comment", "finish"], model="x")
        assert a["name"] == "docs-writer"
        assert a["is_builtin"] == 0
        assert agent_tools("docs-writer") == ["get_context", "comment", "finish"]

    def test_update_agent(self, agents):
        add_agent("docs-writer", tools=["finish"])
        update_agent("docs-writer", tools=["get_context", "finish"], model="m2")
        a = get_agent("docs-writer")
        assert a["tools"] == ["get_context", "finish"]
        assert a["model"] == "m2"

    def test_delete_custom_agent(self, agents):
        add_agent("temp", tools=["finish"])
        assert delete_agent("temp") is True
        assert get_agent("temp") is None

    def test_cannot_delete_builtin(self, agents):
        with pytest.raises(ValueError):
            delete_agent("engineer")

    def test_invalid_kind_rejected(self, agents):
        with pytest.raises(ValueError):
            add_agent("weird", kind="wizard")

    def test_unknown_agent_is_none(self, agents):
        assert get_agent("nope") is None
        assert agent_tools("nope") == []
