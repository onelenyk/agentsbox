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


class TestReadingJsonOutOfAModelsProse:
    """One parser for the planner, the copilot and the task-structure button.

    Three copies of a regex existed and had drifted apart — planner and copilot
    greedy, ai_assist lazy — so the same model reply was readable by one and
    unreadable by another. Each failure returned None, which every caller
    reports as "the model didn't give me anything usable": a retry over a reply
    that was fine. These are the two shapes that used to break."""

    def test_an_aside_in_braces_no_longer_swallows_the_answer(self):
        """The greedy copies matched from the first brace of the reply to the
        last, so this parsed as nothing at all."""
        out = 'Here is the shape {approach, steps} you asked for:\n{"goal": "fix sync"}'
        assert ai_assist.find_json(out) == {"goal": "fix sync"}

    def test_a_trailing_aside_no_longer_swallows_it_either(self):
        out = '{"goal": "fix sync"}\nHappy to adjust {let me know}.'
        assert ai_assist.find_json(out) == {"goal": "fix sync"}

    def test_a_nested_object_survives(self):
        """The lazy copy stopped at the first `}`, truncating every nested
        object — and a plan brief's steps are objects, so that was all of them."""
        out = 'Here you go:\n{"goal": "fix sync", "meta": {"repo": "sisyphus"}}\nAnything else?'
        assert ai_assist.find_json(out) == {"goal": "fix sync", "meta": {"repo": "sisyphus"}}

    def test_a_brace_inside_a_string_does_not_close_the_object(self):
        assert ai_assist.find_json('{"goal": "use {x} here"}') == {"goal": "use {x} here"}

    def test_a_model_that_corrects_itself_is_read_as_its_correction(self):
        assert ai_assist.find_json('{"a": 1} then actually {"a": 2}') == {"a": 2}

    def test_prose_with_no_json_is_none(self):
        assert ai_assist.find_json("sorry, I can't help with that") is None

    def test_something_that_only_looks_like_json_is_none(self):
        assert ai_assist.find_json("use the {placeholder} syntax") is None

    def test_every_module_that_reads_a_model_uses_this_one(self):
        from core import copilot, planner
        out = 'Sure:\n{"goal": "g", "meta": {"a": 1}}\nand {an aside}.'
        want = {"goal": "g", "meta": {"a": 1}}
        assert planner._find_json(out) == want
        assert copilot._find_json(out) == want
        assert ai_assist._find_json(out) == want
