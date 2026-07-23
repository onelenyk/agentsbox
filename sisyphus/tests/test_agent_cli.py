"""Tests for tools/agent_cli.py — the agent toolbelt (comment / finish)."""
import json
import subprocess
import sys
from pathlib import Path

CLI = str(Path(__file__).parent.parent / "tools" / "agent_cli.py")


def _run(args, output_dir=None):
    env = {"PATH": "/usr/bin:/bin"}
    if output_dir is not None:
        env["SISYPHUS_OUTPUT_DIR"] = str(output_dir)
    return subprocess.run(
        [sys.executable, CLI, *args],
        capture_output=True, text=True, env=env,
    )


class TestFinish:
    def test_happy_path_writes_result_json(self, tmp_path):
        proc = _run(["finish", "--status", "completed", "--summary", "all good"], tmp_path)
        assert proc.returncode == 0, proc.stderr
        result = json.loads((tmp_path / "result.json").read_text())
        assert result == {"status": "completed", "summary": "all good"}

    def test_optional_fields_included(self, tmp_path):
        proc = _run(
            ["finish", "--status", "failed", "--summary", "s",
             "--verdict", "changes_requested", "--risk", "high", "--error", "boom"],
            tmp_path,
        )
        assert proc.returncode == 0
        result = json.loads((tmp_path / "result.json").read_text())
        assert result["verdict"] == "changes_requested"
        assert result["risk"] == "high"
        assert result["error"] == "boom"

    def test_invalid_status_rejected(self, tmp_path):
        proc = _run(["finish", "--status", "bogus", "--summary", "s"], tmp_path)
        assert proc.returncode != 0
        assert not (tmp_path / "result.json").exists()

    def test_invalid_verdict_rejected(self, tmp_path):
        proc = _run(
            ["finish", "--status", "completed", "--summary", "s", "--verdict", "maybe"],
            tmp_path,
        )
        assert proc.returncode != 0
        assert not (tmp_path / "result.json").exists()

    def test_empty_summary_rejected(self, tmp_path):
        proc = _run(["finish", "--status", "completed", "--summary", "  "], tmp_path)
        assert proc.returncode == 2
        assert not (tmp_path / "result.json").exists()

    def test_missing_env_exits_2(self):
        proc = _run(["finish", "--status", "completed", "--summary", "s"])
        assert proc.returncode == 2
        assert "SISYPHUS_OUTPUT_DIR" in proc.stderr


class TestComment:
    def test_appends_well_formed_lines(self, tmp_path):
        assert _run(["comment", "first"], tmp_path).returncode == 0
        assert _run(["comment", "second"], tmp_path).returncode == 0

        lines = (tmp_path / "events.jsonl").read_text().strip().split("\n")
        events = [json.loads(l) for l in lines]
        assert [e["text"] for e in events] == ["first", "second"]
        assert all(e["type"] == "comment" for e in events)
        assert all("ts" in e for e in events)

    def test_resource_repeatable(self, tmp_path):
        (tmp_path / "a.md").write_text("x")
        (tmp_path / "b.md").write_text("y")
        proc = _run(["comment", "docs", "--resource", "a.md", "--resource", "b.md"], tmp_path)
        assert proc.returncode == 0
        event = json.loads((tmp_path / "events.jsonl").read_text())
        assert event["resources"] == ["a.md", "b.md"]

    def test_path_separator_in_resource_rejected(self, tmp_path):
        proc = _run(["comment", "sneaky", "--resource", "../etc/passwd"], tmp_path)
        assert proc.returncode == 2
        assert not (tmp_path / "events.jsonl").exists()

    def test_missing_resource_warns_but_succeeds(self, tmp_path):
        proc = _run(["comment", "early", "--resource", "later.md"], tmp_path)
        assert proc.returncode == 0
        assert "warning" in proc.stderr
        event = json.loads((tmp_path / "events.jsonl").read_text())
        assert event["resources"] == ["later.md"]

    def test_missing_env_exits_2(self):
        proc = _run(["comment", "hello"])
        assert proc.returncode == 2


def test_choices_match_base_result_schema():
    """Guard against drift between the CLI's duplicated literals and BaseResult."""
    import typing
    from core.schemas import BaseResult

    def _literals(annotation):
        out = set()
        for arg in typing.get_args(annotation):
            if isinstance(arg, str):
                out.add(arg)
            elif arg is not type(None):
                out |= _literals(arg)
        return out

    spec = {
        field: _literals(BaseResult.model_fields[field].annotation)
        for field in ("status", "verdict", "risk")
    }

    import importlib.util
    module_spec = importlib.util.spec_from_file_location("agent_cli", CLI)
    agent_cli = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(agent_cli)

    assert set(agent_cli.VALID_STATUSES) <= spec["status"]
    assert set(agent_cli.VALID_VERDICTS) == spec["verdict"]
    assert set(agent_cli.VALID_RISKS) == spec["risk"]
