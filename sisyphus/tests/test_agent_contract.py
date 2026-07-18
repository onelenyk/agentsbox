"""Agent communication contract tests.

Validates that:
1. All agent YAML output_fields reference valid BaseResult fields (no drift)
2. Rendered prompts contain all declared conditional output_fields
3. Fields NOT declared in output_fields do NOT appear in rendered prompts
"""

import os
import pytest
from agents import _validate_output_fields, load_agent, list_agents
from core.templates import render_prompt

WORKSPACE = "/tmp/test-workspace"
AGENT_NAMES = list_agents()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_workspace_root(monkeypatch):
    monkeypatch.setitem(os.environ, "WORKSPACE_ROOT", WORKSPACE)


@pytest.fixture
def card():
    return {"description": "Contract test task"}


@pytest.fixture
def step():
    return {"id": "contract-step", "run_id": "run-contract"}


# ---------------------------------------------------------------------------
# 1. Output fields validation (load_agent / _validate_output_fields)
# ---------------------------------------------------------------------------


class TestOutputFieldsValidation:
    """Unit tests for output_fields validation against BaseResult.model_fields."""

    def test_all_agents_pass_validation(self):
        """Every real agent YAML must load without validation errors."""
        for name in AGENT_NAMES:
            agent = load_agent(name, validate=True)
            assert "output_fields" in agent, f"{name} is missing output_fields"

    def test_unknown_field_raises_valueerror(self):
        """An output_field not in BaseResult.model_fields raises ValueError."""
        agent_def = {
            "name": "Test Agent",
            "output_fields": {
                "status": "required",
                "nonexistent_field": "required",
            },
        }
        with pytest.raises(ValueError, match="nonexistent_field"):
            _validate_output_fields(agent_def)

    def test_multiple_unknown_fields_reports_first(self):
        """When multiple unknown fields exist, the first one is reported."""
        agent_def = {
            "name": "Bad Agent",
            "output_fields": {
                "bad1": "required",
                "bad2": "optional",
            },
        }
        with pytest.raises(ValueError, match="bad1"):
            _validate_output_fields(agent_def)

    def test_empty_output_fields_is_valid(self):
        """Agents with no output_fields are valid (e.g. agents that only read)."""
        _validate_output_fields({"name": "Reader", "output_fields": {}})

    def test_missing_output_fields_is_valid(self):
        """Agent definition without output_fields key at all is valid."""
        _validate_output_fields({"name": "Minimal Agent"})

    def test_load_agent_without_validation_skips_check(self):
        """validate=False bypasses the check (useful for loading raw YAML)."""
        for name in AGENT_NAMES:
            agent = load_agent(name, validate=False)
            assert isinstance(agent, dict)


# ---------------------------------------------------------------------------
# 2. Per-agent prompt contract tests
# ---------------------------------------------------------------------------


class TestAgentPromptContracts:
    """Every agent's rendered prompt must faithfully reflect its output_fields."""

    # ---- Conditional fields -------------------------------------------------

    @pytest.mark.parametrize("agent_name", AGENT_NAMES)
    def test_verdict_appears_only_when_required(self, agent_name, card, step):
        """'verdict' field AND 'VALID VERDICT VALUES' appear ONLY for agents
        that declare verdict: required in output_fields."""
        agent = load_agent(agent_name)
        output = render_prompt(agent, card, step)
        declared = agent.get("output_fields", {})

        if declared.get("verdict") == "required":
            assert '"verdict": "approved"' in output, (
                f"{agent_name}: verdict is required but missing from prompt"
            )
            assert "VALID VERDICT VALUES" in output, (
                f"{agent_name}: verdict is required but VALID VERDICT VALUES missing"
            )
        else:
            assert '"verdict"' not in output, (
                f"{agent_name}: verdict should NOT appear but was found in prompt"
            )
            assert "VALID VERDICT VALUES" not in output, (
                f"{agent_name}: VALID VERDICT VALUES should NOT appear"
            )

    @pytest.mark.parametrize("agent_name", AGENT_NAMES)
    def test_risk_appears_only_when_declared(self, agent_name, card, step):
        """'risk' field appears in JSON example ONLY when agent declares it
        (any truthy value — required or optional)."""
        agent = load_agent(agent_name)
        output = render_prompt(agent, card, step)
        declared = agent.get("output_fields", {})

        if declared.get("risk"):
            assert '"risk": "low"' in output, (
                f"{agent_name}: risk is declared ({declared['risk']}) "
                f"but missing from prompt JSON example"
            )
        else:
            assert '"risk"' not in output, (
                f"{agent_name}: risk is NOT declared but appears in prompt"
            )

    # ---- Hardcoded fields (always present) ----------------------------------

    @pytest.mark.parametrize("agent_name", AGENT_NAMES)
    def test_hardcoded_fields_always_present(self, agent_name, card, step):
        """status, summary, and error are hardcoded in every agent prompt."""
        agent = load_agent(agent_name)
        output = render_prompt(agent, card, step)

        assert '"status": "completed"' in output
        assert '"summary":' in output
        assert '"error": null' in output

    # ---- Cross-agent consistency --------------------------------------------

    def test_all_engineer_agents_declare_risk(self):
        """All engineer agents (non-reviewer, non-PM) should declare risk."""
        engineer_agents = [
            n
            for n in AGENT_NAMES
            if n
            not in (
                "code-reviewer",
                "pm",
                "pm-assistant",
                "tech-lead",
                "qa",
            )
        ]
        for name in engineer_agents:
            agent = load_agent(name)
            assert agent["output_fields"].get("risk"), (
                f"{name}: engineer agent should declare risk in output_fields"
            )

    def test_all_agents_have_output_fields(self):
        """Every agent must declare output_fields."""
        for name in AGENT_NAMES:
            agent = load_agent(name)
            assert "output_fields" in agent, f"{name} is missing output_fields"
            assert isinstance(agent["output_fields"], dict)
