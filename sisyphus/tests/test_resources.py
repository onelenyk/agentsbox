"""Tests for core.resources — filesystem resource operations."""

import json
from pathlib import Path

import core.resources
from core.resources import (
    get_available_resources,
    init_run_dir,
    list_resources,
    read_resource,
    save_agent_log,
    save_analysis_md,
    save_resource,
    save_result_json,
)
from core.state import create_step


def _patch_workspace(monkeypatch, tmp_path):
    """Point core.resources to tmp_path for clean filesystem tests."""
    root = str(tmp_path)
    monkeypatch.setattr(core.resources, "WORKSPACE_ROOT", root)
    monkeypatch.setattr(core.resources, "RUNS_DIR", Path(root) / "runs")
    monkeypatch.setenv("WORKSPACE_ROOT", root)


# ── init_run_dir ────────────────────────────────────────────────────────


class TestInitRunDir:
    def test_creates_directories(self, monkeypatch, tmp_path, run):
        _patch_workspace(monkeypatch, tmp_path)
        result = init_run_dir(run["id"])
        run_dir = Path(result)
        assert run_dir.exists()
        assert (run_dir / "resources").is_dir()
        assert (run_dir / "logs").is_dir()

    def test_returns_correct_path(self, monkeypatch, tmp_path, run):
        _patch_workspace(monkeypatch, tmp_path)
        result = init_run_dir(run["id"])
        expected = str(Path(tmp_path) / "runs" / run["id"])
        assert result == expected

    def test_idempotent(self, monkeypatch, tmp_path, run):
        _patch_workspace(monkeypatch, tmp_path)
        first = init_run_dir(run["id"])
        second = init_run_dir(run["id"])
        assert first == second
        assert Path(first).exists()


# ── read_resource ───────────────────────────────────────────────────────


class TestReadResource:
    def test_save_then_read(self, db_path, monkeypatch, tmp_path, run):
        _patch_workspace(monkeypatch, tmp_path)
        step = create_step(run["id"], "test", 0)
        save_resource(run["id"], step["id"], "test.txt", "hello world", step_order=0)
        result = read_resource(run["id"], "test.txt", step_id=step["id"])
        assert result == "hello world"

    def test_read_without_step_id(self, monkeypatch, tmp_path, run):
        _patch_workspace(monkeypatch, tmp_path)
        root = Path(tmp_path) / "runs" / run["id"] / "resources"
        root.mkdir(parents=True)
        (root / "data.txt").write_text("direct content", encoding="utf-8")
        result = read_resource(run["id"], "data.txt")
        assert result == "direct content"

    def test_nonexistent_file(self, monkeypatch, tmp_path, run):
        _patch_workspace(monkeypatch, tmp_path)
        result = read_resource(run["id"], "nope.txt")
        assert result is None

    def test_read_with_step_id(self, monkeypatch, tmp_path, run):
        _patch_workspace(monkeypatch, tmp_path)
        step_dir = (
            Path(tmp_path) / "runs" / run["id"] / "resources" / "step-abc"
        )
        step_dir.mkdir(parents=True)
        (step_dir / "output.txt").write_text("step output", encoding="utf-8")
        result = read_resource(run["id"], "output.txt", step_id="step-abc")
        assert result == "step output"


# ── save_agent_log ──────────────────────────────────────────────────────


class TestSaveAgentLog:
    def test_saves_log_content(self, monkeypatch, tmp_path, run):
        _patch_workspace(monkeypatch, tmp_path)
        content = "[INFO] Agent started\n[INFO] Done"
        path_str = save_agent_log(run["id"], "step-1", content)
        log_path = Path(path_str)
        assert log_path.exists()
        assert log_path.read_text(encoding="utf-8") == content

    def test_returns_correct_path(self, monkeypatch, tmp_path, run):
        _patch_workspace(monkeypatch, tmp_path)
        path_str = save_agent_log(run["id"], "step-log", "content")
        expected = str(
            Path(tmp_path) / "runs" / run["id"] / "logs" / "step-log.log"
        )
        assert path_str == expected


# ── save_result_json ────────────────────────────────────────────────────


class TestSaveResultJson:
    def test_saves_valid_json(self, db_path, monkeypatch, tmp_path, run):
        _patch_workspace(monkeypatch, tmp_path)
        step = create_step(run["id"], "test", 0)
        data = {"status": "ok", "count": 42}
        path_str = save_result_json(run["id"], step["id"], data)
        filepath = Path(path_str)
        assert filepath.exists()
        parsed = json.loads(filepath.read_text(encoding="utf-8"))
        assert parsed == data

    def test_returns_correct_path(self, db_path, monkeypatch, tmp_path, run):
        _patch_workspace(monkeypatch, tmp_path)
        step = create_step(run["id"], "test", 0)
        path_str = save_result_json(run["id"], step["id"], {"x": 1})
        expected = str(
            Path(tmp_path)
            / "runs"
            / run["id"]
            / "resources"
            / step["id"]
            / "result.json"
        )
        assert path_str == expected


# ── save_analysis_md ────────────────────────────────────────────────────


class TestSaveAnalysisMd:
    def test_saves_markdown_content(self, db_path, monkeypatch, tmp_path, run):
        _patch_workspace(monkeypatch, tmp_path)
        step = create_step(run["id"], "test", 0)
        md = "# Analysis\n\nFindings..."
        path_str = save_analysis_md(run["id"], step["id"], md)
        filepath = Path(path_str)
        assert filepath.exists()
        assert filepath.read_text(encoding="utf-8") == md

    def test_returns_correct_path(self, db_path, monkeypatch, tmp_path, run):
        _patch_workspace(monkeypatch, tmp_path)
        step = create_step(run["id"], "test", 0)
        path_str = save_analysis_md(run["id"], step["id"], "md")
        expected = str(
            Path(tmp_path)
            / "runs"
            / run["id"]
            / "resources"
            / step["id"]
            / "analysis.md"
        )
        assert path_str == expected


# ── list_resources ──────────────────────────────────────────────────────


class TestListResources:
    def test_empty_when_no_resources(self, db_path, monkeypatch, tmp_path, run):
        _patch_workspace(monkeypatch, tmp_path)
        result = list_resources(run["id"])
        assert result == []

    def test_contains_saved_resource(self, db_path, monkeypatch, tmp_path, run):
        _patch_workspace(monkeypatch, tmp_path)
        step = create_step(run["id"], "test", 0)
        save_resource(run["id"], step["id"], "output.txt", "data", step_order=0)
        result = list_resources(run["id"])
        assert len(result) == 1
        assert result[0]["filename"] == "output.txt"
        assert result[0]["step_id"] == step["id"]
        assert result[0]["run_id"] == run["id"]

    def test_multiple_resources(self, db_path, monkeypatch, tmp_path, run):
        _patch_workspace(monkeypatch, tmp_path)
        s1 = create_step(run["id"], "agent-a", 0)
        s2 = create_step(run["id"], "agent-b", 1)
        save_resource(run["id"], s1["id"], "a.txt", "A", step_order=0)
        save_resource(run["id"], s2["id"], "b.txt", "B", step_order=1)
        result = list_resources(run["id"])
        assert len(result) == 2
        filenames = {r["filename"] for r in result}
        assert filenames == {"a.txt", "b.txt"}


# ── get_available_resources (gap tests) ─────────────────────────────────


class TestGetAvailableResources:
    def test_step0_visible_to_step1(self, db_path, monkeypatch, tmp_path, run):
        """Resource from step 0 should be visible to step 1."""
        _patch_workspace(monkeypatch, tmp_path)
        s0 = create_step(run["id"], "agent-0", 0)
        s1 = create_step(run["id"], "agent-1", 1)
        save_resource(
            run["id"], s0["id"], "from_s0.txt", "from step 0", step_order=0
        )
        available = get_available_resources(
            run["id"], step_order=1, exclude_step_id=s1["id"]
        )
        assert len(available) == 1
        assert available[0]["filename"] == "from_s0.txt"
        assert available[0]["step_id"] == s0["id"]

    def test_sibling_isolation(self, db_path, monkeypatch, tmp_path, run):
        """Resource from one sibling (same step_order) should NOT be visible
        to another sibling at the same step_order."""
        _patch_workspace(monkeypatch, tmp_path)
        s1a = create_step(run["id"], "agent-1a", 1)
        s1b = create_step(run["id"], "agent-1b", 1)
        save_resource(
            run["id"], s1a["id"], "sibling.txt", "from sibling", step_order=1
        )
        # s1b queries available resources at its own step_order, excluding itself
        available = get_available_resources(
            run["id"], step_order=1, exclude_step_id=s1b["id"]
        )
        # Resource from s1a (same step_order=1) is excluded by max_step_order=0
        assert len(available) == 0
