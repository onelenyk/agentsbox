"""Integration tests for investigate pipeline."""
import pytest

from core.state import create_card, create_run, create_step


class TestInvestigate:
    def test_single_step_pipeline(self, db_path):
        card = create_card("investigate", ["android"], "find hardcoded colors")
        run = create_run(card["id"])
        step = create_step(run["id"], "Android Engineer", 1)
        assert step["status"] == "pending"
        assert step["step_order"] == 1
        assert card["pipeline_name"] == "investigate"

    def test_invalid_pipeline_reference(self, db_path):
        card = create_card("nonexistent-pipeline", ["android"], "test")
        assert card["pipeline_name"] == "nonexistent-pipeline"
        assert card["status"] == "backlog"

    def test_multiple_steps(self, db_path, card):
        run = create_run(card["id"])
        s1 = create_step(run["id"], "PM", 1)
        s2 = create_step(run["id"], "Engineer", 2)
        s3 = create_step(run["id"], "Reviewer", 3)

        assert s1["step_order"] == 1
        assert s2["step_order"] == 2
        assert s3["step_order"] == 3
        assert s1["run_id"] == s2["run_id"] == s3["run_id"]


class TestPipelineValidator:
    def test_valid_investigate_pipeline(self, db_path):
        from core.pipeline_validator import validate_pipeline_string

        yaml = """
name: investigate
description: Investigate a codebase issue
steps:
  - id: engineer
    agent: android-engineer
    depends_on: []
"""
        errors = validate_pipeline_string(yaml)
        assert len(errors) == 0

    def test_invalid_circular_dep(self, db_path):
        from core.pipeline_validator import validate_pipeline_string

        yaml = """
name: test
steps:
  - id: s1
    agent: android-engineer
    depends_on: s2
  - id: s2
    agent: code-reviewer
    depends_on: s1
"""
        errors = validate_pipeline_string(yaml)
        assert len(errors) > 0
        assert any("circular" in e.lower() for e in errors)

    def test_missing_name(self, db_path):
        from core.pipeline_validator import validate_pipeline_string

        yaml = """
steps:
  - id: s1
    agent: android-engineer
"""
        errors = validate_pipeline_string(yaml)
        assert len(errors) > 0
        assert any("name" in e.lower() for e in errors)

    def test_missing_steps(self, db_path):
        from core.pipeline_validator import validate_pipeline_string

        yaml = "name: test\n"
        errors = validate_pipeline_string(yaml)
        assert len(errors) > 0
        assert any("steps" in e.lower() for e in errors)

    def test_empty_steps(self, db_path):
        from core.pipeline_validator import validate_pipeline_string

        yaml = """
name: test
steps: []
"""
        errors = validate_pipeline_string(yaml)
        assert len(errors) > 0
        assert any("empty" in e.lower() for e in errors)

    def test_missing_agent(self, db_path):
        from core.pipeline_validator import validate_pipeline_string

        yaml = """
name: test
steps:
  - id: s1
"""
        errors = validate_pipeline_string(yaml)
        assert len(errors) > 0
        assert any("agent" in e.lower() for e in errors)

    def test_unknown_agent(self, db_path):
        from core.pipeline_validator import validate_pipeline_string

        yaml = """
name: test
steps:
  - id: s1
    agent: nonexistent-agent-xyz
"""
        errors = validate_pipeline_string(yaml)
        assert len(errors) > 0
        assert any("unknown" in e.lower() for e in errors)

    def test_duplicate_step_id(self, db_path):
        from core.pipeline_validator import validate_pipeline_string

        yaml = """
name: test
steps:
  - id: s1
    agent: android-engineer
  - id: s1
    agent: code-reviewer
"""
        errors = validate_pipeline_string(yaml)
        assert len(errors) > 0
        assert any("duplicate" in e.lower() for e in errors)

    def test_invalid_dependency(self, db_path):
        from core.pipeline_validator import validate_pipeline_string

        yaml = """
name: test
steps:
  - id: s1
    agent: android-engineer
    depends_on: nonexistent-step
"""
        errors = validate_pipeline_string(yaml)
        assert len(errors) > 0
        assert any("not a valid step" in e.lower() for e in errors)

    def test_straight_line_deps(self, db_path):
        from core.pipeline_validator import validate_pipeline_string

        yaml = """
name: sdlc
steps:
  - id: pm
    agent: pm-assistant
  - id: engineer
    agent: android-engineer
    depends_on: pm
  - id: reviewer
    agent: code-reviewer
    depends_on: engineer
  - id: qa
    agent: qa
    depends_on: reviewer
"""
        errors = validate_pipeline_string(yaml)
        assert len(errors) == 0
