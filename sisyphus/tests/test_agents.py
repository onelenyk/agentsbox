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
from core.roles import ROLES


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


class TestOrchestratorAgent:
    def test_seeded_as_orchestrator_kind(self, agents):
        o = get_agent("orchestrator")
        assert o is not None
        assert o["kind"] == "orchestrator"
        assert "decide_spawn_worker" in o["tools"]
        assert "decide_complete" in o["tools"]

    def test_listed_by_kind(self, agents):
        names = {a["name"] for a in list_agents(kind="orchestrator")}
        assert names == {"orchestrator"}


class TestUpgradeSeeding:
    def test_orchestrator_added_to_a_worker_only_db(self, db_path):
        """A DB seeded before the orchestrator agent existed (Phase 1) gains it
        on the next init — otherwise _decide would have no agent to run."""
        from core.agents import get_agent, init_agents_table
        from core.db import get_conn
        # simulate the old (worker-only) state
        conn = get_conn()
        conn.execute("DELETE FROM agents WHERE kind = 'orchestrator'")
        conn.commit()
        conn.close()
        assert get_agent("orchestrator") is None

        init_agents_table()   # upgrade re-adds it, workers untouched
        assert get_agent("orchestrator")["kind"] == "orchestrator"
        assert len(list_agents(kind="worker")) == len(ROLES)

    def test_an_agent_the_code_pins_by_name_is_created_if_absent(self, db_path):
        """core.reviews pins every review-of-MR task to 'mr-reviewer'. It lived
        only as a row someone had made by hand, so on a database where nobody
        had, every opened MR spawned a task pinned to an agent that does not
        exist and the review never happened."""
        from core.agents import get_agent, init_agents_table
        from core.db import get_conn
        conn = get_conn()
        conn.execute("DELETE FROM agents WHERE name = 'mr-reviewer'")
        conn.commit()
        conn.close()
        assert get_agent("mr-reviewer") is None

        init_agents_table()
        agent = get_agent("mr-reviewer")
        assert agent and "get_mr_diff" in agent["tools"]

    def test_a_tuned_persona_is_never_overwritten(self, db_path):
        """Create-if-absent, not upsert: what someone edited is theirs."""
        from core.agents import get_agent, init_agents_table, update_agent
        update_agent("mr-reviewer", persona="my own words")
        init_agents_table()
        assert get_agent("mr-reviewer")["persona"] == "my own words"
