"""AI task-parse helper (core/ai_assist.py) — mocked runner, no tokens spent."""
from types import SimpleNamespace

import pytest

from core import ai_assist


def test_find_json_variants():
    assert ai_assist._find_json('{"goal":"g","repo":"","agent":""}')["goal"] == "g"
    assert ai_assist._find_json('```json\n{"goal":"g"}\n```')["goal"] == "g"
    assert ai_assist._find_json('here you go: {"goal":"g","repo":"r"} done')["repo"] == "r"
    assert ai_assist._find_json("no json at all") is None


def test_parse_out_validates_against_lists():
    r = ai_assist._parse_out('{"goal":"do it","repo":"testrepo","agent":"ghost"}',
                             ["testrepo"], ["engineer"])
    assert r == {"goal": "do it", "repo": "testrepo", "agent": ""}  # unknown agent dropped
    assert ai_assist._parse_out('{"goal":"x","repo":"nope"}', ["testrepo"], [])["repo"] == ""


def test_parse_out_no_json_is_empty():
    assert ai_assist._parse_out("model was confused", ["testrepo"], []) == {
        "goal": "", "repo": "", "agent": ""}


def test_parse_task_mocks_runner(monkeypatch):
    monkeypatch.setattr("core.runners.get_default_runner",
                        lambda: {"binary": "opencode", "model": "m", "api_key_env": "", "base_url": ""})
    monkeypatch.setattr("core.runner_detect.resolve", lambda b: "/abs/opencode")
    monkeypatch.setattr(ai_assist.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(
                            stdout='{"goal":"Add multiply and divide with tests","repo":"testrepo","agent":""}',
                            stderr="", returncode=0))
    r = ai_assist.parse_task("add mul div in testrepo", "feature",
                             [{"name": "testrepo"}], [{"name": "engineer"}])
    assert r["goal"].startswith("Add multiply") and r["repo"] == "testrepo" and r["agent"] == ""


def test_parse_task_no_runner(monkeypatch):
    monkeypatch.setattr("core.runners.get_default_runner", lambda: None)
    with pytest.raises(RuntimeError):
        ai_assist.parse_task("x")
