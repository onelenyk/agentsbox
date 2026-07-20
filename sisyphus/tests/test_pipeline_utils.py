"""Pure unit tests for pipeline utility functions.

Tests the five private utility functions exported by core.pipeline:
  _topological_sort, _find_engineer_step, _find_step_def,
  _get_step_order, _agent_timeout.

No DB or conftest fixtures needed — these are pure (or mockable) functions.
"""
from unittest.mock import patch

import yaml

from core.pipeline import (
    _topological_sort,
    _find_engineer_step,
    _find_step_def,
    _get_step_order,
    _agent_timeout,
)
from core.pipeline_validator import validate_pipeline_string


class TestTopologicalSort:
    """_topological_sort — BFS wave-based topological sort."""

    def test_single_step_no_deps(self):
        """A single step with no dependencies → one wave."""
        steps = [{"id": "step-a"}]
        waves = _topological_sort(steps)
        assert len(waves) == 1
        assert len(waves[0]) == 1
        assert waves[0][0]["id"] == "step-a"

    def test_linear_chain_three_waves(self):
        """A → B → C should produce three sequential waves."""
        steps = [
            {"id": "A"},
            {"id": "B", "depends_on": ["A"]},
            {"id": "C", "depends_on": ["B"]},
        ]
        waves = _topological_sort(steps)
        assert len(waves) == 3
        assert [s["id"] for s in waves[0]] == ["A"]
        assert [s["id"] for s in waves[1]] == ["B"]
        assert [s["id"] for s in waves[2]] == ["C"]

    def test_diamond_three_waves(self):
        """Diamond: A→B, A→C, B→D, C→D → 3 waves, B/C parallel."""
        steps = [
            {"id": "A"},
            {"id": "B", "depends_on": ["A"]},
            {"id": "C", "depends_on": ["A"]},
            {"id": "D", "depends_on": ["B", "C"]},
        ]
        waves = _topological_sort(steps)
        assert len(waves) == 3
        assert [s["id"] for s in waves[0]] == ["A"]
        # B and C can run in parallel after A
        assert sorted(s["id"] for s in waves[1]) == ["B", "C"]
        assert [s["id"] for s in waves[2]] == ["D"]

    def test_two_independent_steps_one_wave(self):
        """Two steps with no deps → single wave, both parallel."""
        steps = [{"id": "A"}, {"id": "B"}]
        waves = _topological_sort(steps)
        assert len(waves) == 1
        assert len(waves[0]) == 2
        ids = sorted(s["id"] for s in waves[0])
        assert ids == ["A", "B"]

    def test_empty_list_zero_waves(self):
        """Empty step list → zero waves."""
        waves = _topological_sort([])
        assert waves == []

    def test_string_depends_on_handled_gracefully(self):
        """A string depends_on (not list) is normalized to a list."""
        steps = [
            {"id": "A"},
            {"id": "B", "depends_on": "A"},
        ]
        waves = _topological_sort(steps)
        assert len(waves) == 2
        assert [s["id"] for s in waves[0]] == ["A"]
        assert [s["id"] for s in waves[1]] == ["B"]

    def test_nonexistent_dep_ignored(self):
        """A dep that references a missing step id is silently skipped."""
        steps = [
            {"id": "A", "depends_on": ["nonexistent"]},
        ]
        waves = _topological_sort(steps)
        assert len(waves) == 1
        assert waves[0][0]["id"] == "A"

    def test_unordered_steps_go_to_earliest_wave(self):
        """Step C defined before B → C's dep on B skipped, C lands in wave 0."""
        steps = [
            {"id": "C", "depends_on": ["B"]},
            {"id": "A"},
            {"id": "B", "depends_on": ["A"]},
        ]
        waves = _topological_sort(steps)
        # C enters wave 0 because B was not yet in step_map when C was iterated
        assert len(waves) == 2
        wave0_ids = sorted(s["id"] for s in waves[0])
        assert wave0_ids == ["A", "C"]
        assert [s["id"] for s in waves[1]] == ["B"]

    def test_multiple_deps_per_step(self):
        """A step with multiple deps waits for all of them."""
        steps = [
            {"id": "A"},
            {"id": "B"},
            {"id": "C", "depends_on": ["A", "B"]},
        ]
        waves = _topological_sort(steps)
        assert len(waves) == 2
        wave0_ids = sorted(s["id"] for s in waves[0])
        assert wave0_ids == ["A", "B"]
        assert [s["id"] for s in waves[1]] == ["C"]

    def test_all_deps_nonexistent_still_emitted(self):
        """Even when every dep is missing, the step is still emitted (wave 0)."""
        steps = [
            {"id": "orphan", "depends_on": ["ghost1", "ghost2"]},
        ]
        waves = _topological_sort(steps)
        assert len(waves) == 1
        assert waves[0][0]["id"] == "orphan"


class TestFindEngineerStep:
    """_find_engineer_step — returns the first engineer dep for a reviewer."""

    def test_happy_path_returns_first_dep(self):
        """Reviewer with one dependency returns that dep's id."""
        steps = [
            {"id": "engineer"},
            {"id": "reviewer", "depends_on": ["engineer"]},
        ]
        assert _find_engineer_step("reviewer", steps) == "engineer"

    def test_multiple_deps_returns_first(self):
        """When reviewer depends on multiple steps, only the first is returned."""
        steps = [
            {"id": "engineer-a"},
            {"id": "engineer-b"},
            {"id": "reviewer", "depends_on": ["engineer-a", "engineer-b"]},
        ]
        assert _find_engineer_step("reviewer", steps) == "engineer-a"

    def test_no_deps_returns_none(self):
        """Reviewer with no depends_on returns None."""
        steps = [{"id": "reviewer"}]
        assert _find_engineer_step("reviewer", steps) is None

    def test_step_not_found_returns_none(self):
        """Sid not present in steps → None."""
        steps = [{"id": "other"}]
        assert _find_engineer_step("missing", steps) is None

    def test_string_depends_on_also_works(self):
        """String-typed depends_on is normalized the same way."""
        steps = [
            {"id": "engineer"},
            {"id": "reviewer", "depends_on": "engineer"},
        ]
        assert _find_engineer_step("reviewer", steps) == "engineer"

    def test_explicit_empty_deps_list_returns_none(self):
        """depends_on = [] → None."""
        steps = [{"id": "reviewer", "depends_on": []}]
        assert _find_engineer_step("reviewer", steps) is None


class TestFindStepDef:
    """_find_step_def — locate a step definition dict by id."""

    def test_found_returns_dict(self):
        """Returns the full step dict when sid matches."""
        steps = [
            {"id": "step-a", "agent": "explorer", "extra": "value"},
            {"id": "step-b"},
        ]
        result = _find_step_def("step-a", steps)
        assert result is not None
        assert result["id"] == "step-a"
        assert result["agent"] == "explorer"
        assert result["extra"] == "value"

    def test_not_found_returns_none(self):
        """Returns None when no step has the given sid."""
        steps = [{"id": "step-a"}]
        assert _find_step_def("missing", steps) is None

    def test_empty_steps_returns_none(self):
        """Empty list → None."""
        assert _find_step_def("anything", []) is None


class TestGetStepOrder:
    """_get_step_order — 0-based positional index of a step."""

    def test_first_step_returns_zero(self):
        steps = [{"id": "A"}, {"id": "B"}, {"id": "C"}]
        assert _get_step_order("A", steps) == 0

    def test_middle_step_returns_correct_index(self):
        steps = [{"id": "A"}, {"id": "B"}, {"id": "C"}]
        assert _get_step_order("B", steps) == 1

    def test_last_step_returns_correct_index(self):
        steps = [{"id": "A"}, {"id": "B"}, {"id": "C"}]
        assert _get_step_order("C", steps) == 2

    def test_not_found_returns_zero(self):
        """Unknown sid returns 0 (safe default, won't crash)."""
        steps = [{"id": "A"}]
        assert _get_step_order("missing", steps) == 0

    def test_empty_steps_returns_zero(self):
        """Empty steps → 0."""
        assert _get_step_order("anything", []) == 0


class TestAgentTimeout:
    """_agent_timeout — extract timeout from agent config via load_agent."""

    def test_explicit_timeout_from_agent_config(self):
        """When opencode_config.timeout is set, that value is returned."""
        step_def = {"agent": "custom-agent"}
        with patch("core.pipeline.load_agent") as mock_load:
            mock_load.return_value = {
                "name": "custom-agent",
                "opencode_config": {"timeout": 600},
            }
            assert _agent_timeout(step_def) == 600

    def test_default_timeout_when_no_config_key(self):
        """When opencode_config has no timeout key → default 300."""
        step_def = {"agent": "default-agent"}
        with patch("core.pipeline.load_agent") as mock_load:
            mock_load.return_value = {
                "name": "default-agent",
                "opencode_config": {"model": "gpt-4"},
            }
            assert _agent_timeout(step_def) == 300

    def test_default_timeout_when_no_opencode_config(self):
        """When agent_def has no opencode_config at all → default 300."""
        step_def = {"agent": "bare-agent"}
        with patch("core.pipeline.load_agent") as mock_load:
            mock_load.return_value = {"name": "bare-agent"}
            assert _agent_timeout(step_def) == 300

    def test_default_agent_when_no_agent_key(self):
        """When step_def has no 'agent' key, defaults to 'android-engineer'."""
        step_def = {}
        with patch("core.pipeline.load_agent") as mock_load:
            mock_load.return_value = {
                "name": "android-engineer",
                "opencode_config": {"timeout": 120},
            }
            assert _agent_timeout(step_def) == 120
            mock_load.assert_called_once_with("android-engineer")

    def test_timeout_zero_is_honored(self):
        """A timeout of 0 (if configured) is returned as-is."""
        step_def = {"agent": "zero-timeout"}
        with patch("core.pipeline.load_agent") as mock_load:
            mock_load.return_value = {
                "name": "zero-timeout",
                "opencode_config": {"timeout": 0},
            }
            assert _agent_timeout(step_def) == 0


class TestSharedWorktreeValidation:
    """validate_pipeline_string — shared_worktree and branch validation."""

    def test_shared_worktree_default_false(self):
        """A pipeline without shared_worktree should validate cleanly."""
        yaml_str = yaml.dump({
            "name": "test", "steps": [{"id": "s1", "agent": "agent-a"}]
        })
        with patch("core.pipeline_validator.load_agent",
                   return_value={"name": "agent-a"}):
            errors = validate_pipeline_string(yaml_str, "test.yaml")
            assert errors == []

    def test_shared_worktree_accepts_bool_true(self):
        """shared_worktree: true should validate cleanly."""
        yaml_str = yaml.dump({
            "name": "test", "shared_worktree": True,
            "steps": [{"id": "s1", "agent": "agent-a"}],
        })
        with patch("core.pipeline_validator.load_agent",
                   return_value={"name": "agent-a"}):
            errors = validate_pipeline_string(yaml_str, "test.yaml")
            assert errors == []

    def test_shared_worktree_rejects_non_bool(self):
        """shared_worktree: 'yes' (string) should produce a boolean error."""
        yaml_str = yaml.dump({
            "name": "test", "shared_worktree": "yes",
            "steps": [{"id": "s1", "agent": "agent-a"}],
        })
        with patch("core.pipeline_validator.load_agent",
                   return_value={"name": "agent-a"}):
            errors = validate_pipeline_string(yaml_str, "test.yaml")
            assert any("shared_worktree" in e and "boolean" in e for e in errors)

    def test_branch_accepts_string(self):
        """branch: develop should validate cleanly."""
        yaml_str = yaml.dump({
            "name": "test", "branch": "develop",
            "steps": [{"id": "s1", "agent": "agent-a"}],
        })
        with patch("core.pipeline_validator.load_agent",
                   return_value={"name": "agent-a"}):
            errors = validate_pipeline_string(yaml_str, "test.yaml")
            assert errors == []

    def test_branch_rejects_non_string(self):
        """branch: 123 (number) should produce a string error."""
        yaml_str = yaml.dump({
            "name": "test", "branch": 123,
            "steps": [{"id": "s1", "agent": "agent-a"}],
        })
        with patch("core.pipeline_validator.load_agent",
                   return_value={"name": "agent-a"}):
            errors = validate_pipeline_string(yaml_str, "test.yaml")
            assert any("branch" in e and "string" in e for e in errors)

    def test_shared_worktree_rejects_step_branch_mismatch(self):
        """shared_worktree true + step with different branch → error."""
        yaml_str = yaml.dump({
            "name": "test", "shared_worktree": True, "branch": "main",
            "steps": [{"id": "s1", "agent": "agent-a", "branch": "develop"}],
        })
        with patch("core.pipeline_validator.load_agent",
                   return_value={"name": "agent-a"}):
            errors = validate_pipeline_string(yaml_str, "test.yaml")
            assert any(
                "shared_worktree requires" in e or "same branch" in e
                for e in errors
            )

    def test_shared_worktree_allows_same_branch_on_step(self):
        """shared_worktree true + step with same branch → clean."""
        yaml_str = yaml.dump({
            "name": "test", "shared_worktree": True, "branch": "main",
            "steps": [{"id": "s1", "agent": "agent-a", "branch": "main"}],
        })
        with patch("core.pipeline_validator.load_agent",
                   return_value={"name": "agent-a"}):
            errors = validate_pipeline_string(yaml_str, "test.yaml")
            assert errors == []
