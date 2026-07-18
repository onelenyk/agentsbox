"""Unit tests for Jinja2 prompt template rendering via render_prompt."""

import os
import pytest
from core.templates import render_prompt

WORKSPACE = "/tmp/test-workspace"


@pytest.fixture(autouse=True)
def _mock_workspace_root(monkeypatch):
    """Mock WORKSPACE_ROOT environment variable for all tests."""
    monkeypatch.setitem(os.environ, "WORKSPACE_ROOT", WORKSPACE)


class TestRenderPrompt:
    """Tests for render_prompt covering base/reviewer templates, resources, and edge cases."""

    # ------------------------------------------------------------------
    # 1. Base template (non-reviewer agent)
    # ------------------------------------------------------------------
    def test_base_template_renders_agent_and_task_info(self):
        agent_def = {
            "name": "Android Engineer",
            "role": "Android Developer",
            "access": "write",
        }
        card = {"description": "Fix crash on login screen"}
        step = {"id": "step-1", "run_id": "run-42"}

        result = render_prompt(agent_def, card, step)

        assert "Android Developer" in result
        assert "Fix crash on login screen" in result
        assert "Access Level: WRITE" in result
        expected_output_dir = f"{WORKSPACE}/runs/run-42/resources/step-1"
        assert expected_output_dir in result

    # ------------------------------------------------------------------
    # 2. Reviewer template
    # ------------------------------------------------------------------
    def test_reviewer_template_uses_reviewer_j2(self):
        agent_def = {
            "name": "Code Reviewer",
            "role": "Code Reviewer",
            "access": "analyze",
            "output_fields": {"verdict": "required"},
        }
        card = {"description": "Review the fix plan"}
        step = {"id": "review-step", "run_id": "run-99"}

        result = render_prompt(agent_def, card, step)

        assert "Review the Fix Plan" in result
        assert "Are all findings accurate?" in result
        assert "approved" in result
        assert "changes_requested" in result

    # ------------------------------------------------------------------
    # 3. With resources
    # ------------------------------------------------------------------
    def test_with_resources_renders_resource_filenames(self):
        agent_def = {
            "name": "Explorer",
            "role": "Code Explorer",
            "access": "read",
        }
        card = {"description": "Analyze the codebase"}
        step = {"id": "step-2", "run_id": "run-1"}
        resources = [
            {"step_id": "step-1", "filename": "analysis.md", "resource_type": "analysis"},
            {"step_id": "step-1", "filename": "result.json", "resource_type": "result"},
        ]

        result = render_prompt(agent_def, card, step, resources)

        assert "resources/step-1/analysis.md" in result
        assert "resources/step-1/result.json" in result
        assert "from analysis" in result
        assert "from result" in result

    # ------------------------------------------------------------------
    # 4. With jira_url in card
    # ------------------------------------------------------------------
    def test_with_jira_url_appends_jira_url_to_output(self):
        agent_def = {
            "name": "Bug Fixer",
            "role": "Bug Fix Engineer",
            "access": "write",
        }
        card = {
            "description": "Fix null pointer exception",
            "jira_url": "https://jira.example.com/PROJ-1234",
        }
        step = {"id": "step-3", "run_id": "run-7"}

        result = render_prompt(agent_def, card, step)

        assert "Jira Issue: https://jira.example.com/PROJ-1234" in result
        assert "Fix null pointer exception" in result

    # ------------------------------------------------------------------
    # 5. Empty description fallback
    # ------------------------------------------------------------------
    def test_empty_description_does_not_crash(self):
        agent_def = {
            "name": "Tester",
            "role": "Test Runner",
            "access": "read",
        }
        card = {}
        step = {"id": "step-4", "run_id": "run-10"}

        result = render_prompt(agent_def, card, step)

        assert "Analyze the task and produce your output" in result
        assert "Access Level: READ" in result

    # ------------------------------------------------------------------
    # 6. Missing step fields (no run_id)
    # ------------------------------------------------------------------
    def test_missing_step_fields_uses_unknown_fallback(self):
        agent_def = {
            "name": "Scout",
            "role": "Repository Scout",
            "access": "read",
        }
        card = {"description": "Find all TODOs"}
        step = {}

        result = render_prompt(agent_def, card, step)

        assert f"{WORKSPACE}/runs/unknown/resources/unknown" in result
        assert "Find all TODOs" in result
