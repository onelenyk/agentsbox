"""Choosing a runner's model and sign-in — core/runner_catalog.py, core/keychain.py,
and how the choice reaches a worker (core.runner_profiles.build_env)."""
import json
import subprocess
from unittest.mock import patch

import pytest

from core import keychain, runner_catalog as rc, runner_profiles

CATALOG = {
    "openrouter": {"name": "OpenRouter", "env": ["OPENROUTER_API_KEY"], "models": {
        "deepseek/deepseek-v4": {"name": "DeepSeek V4", "tool_call": True, "release_date": "2026-05-01",
                                 "limit": {"context": 1_000_000}, "cost": {"input": 0.3, "output": 1.2}},
        "some/embedder": {"name": "Embedder", "tool_call": False},
    }},
    "opencode-go": {"name": "OpenCode Go", "env": ["OPENCODE_API_KEY"], "models": {
        "qwen3.7-max": {"name": "Qwen3.7 Max", "tool_call": True, "limit": {"context": 262_144},
                        "cost": {"input": 0, "output": 0}},
    }},
    "anthropic": {"name": "Anthropic", "env": ["ANTHROPIC_API_KEY"], "models": {
        "claude-opus-5": {"name": "Claude Opus 5", "release_date": "2026-07-24", "tool_call": True},
        "claude-opus-4-8": {"name": "Claude Opus 4.8", "release_date": "2026-05-28", "tool_call": True},
        "claude-haiku-4-5-20251001": {"name": "Claude Haiku 4.5", "release_date": "2025-10-15"},
        "claude-haiku-4-5": {"name": "Claude Haiku 4.5 (latest)", "release_date": "2025-10-15"},
    }},
}


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    path = tmp_path / "models.json"
    path.write_text(json.dumps(CATALOG))
    monkeypatch.setattr(rc, "CATALOG_PATH", path)
    rc.clear_cache()
    yield
    rc.clear_cache()


def _proc(out="", code=0):
    return subprocess.CompletedProcess([], code, stdout=out, stderr="")


class TestModelsComeFromTheRunner:
    def test_opencode_lists_what_it_is_signed_in_for_grouped_and_described(self):
        out = "openrouter/deepseek/deepseek-v4\nopenrouter/some/embedder\nopencode-go/qwen3.7-max\nnoise line\n"
        with patch.object(rc, "_run", return_value=_proc(out)):
            groups = rc.models("opencode", "/bin/opencode")
        assert [g["label"] for g in groups] == ["OpenRouter", "OpenCode Go"]
        ds = rc.find(groups, "openrouter/deepseek/deepseek-v4")
        assert ds["name"] == "DeepSeek V4" and ds["ctx"] == "1M" and ds["price"] == "$0.3 / $1.2"
        assert rc.find(groups, "opencode-go/qwen3.7-max")["price"] == "free"
        assert rc.find(groups, "openrouter/some/embedder") is None      # cannot call a tool

    def test_a_runner_that_will_not_answer_lists_nothing(self):
        with patch.object(rc, "_run", return_value=None):
            assert rc.models("opencode", "/bin/opencode") == []

    def test_claude_offers_family_aliases_then_pinned_versions(self):
        groups = rc.models("claude-cli", "/bin/claude")
        aliases = {m["id"]: m["name"] for m in groups[0]["models"]}
        assert list(aliases) == ["fable", "opus", "sonnet", "haiku"]
        assert aliases["opus"] == "opus → Claude Opus 5"
        pinned = [m["id"] for m in groups[1]["models"]]
        assert pinned[0] == "claude-opus-5" and "claude-haiku-4-5-20251001" not in pinned


class TestSignIn:
    def test_claude_reads_its_own_status(self):
        st = json.dumps({"loggedIn": True, "authMethod": "claude.ai", "email": "me@example.com"})
        with patch.object(rc, "_run", return_value=_proc(st)):
            s = rc.login_status("claude-cli", "/bin/claude")
        assert s["ok"] and s["summary"] == "Signed in as me@example.com"

    def test_claude_signed_out_says_how_to_sign_in(self):
        with patch.object(rc, "_run", return_value=_proc(json.dumps({"loggedIn": False}))):
            s = rc.login_status("claude-cli", "/bin/claude")
        assert not s["ok"] and "auth login" in s["detail"]

    def test_opencode_names_providers_without_reading_secrets(self, tmp_path, monkeypatch):
        auth = tmp_path / "auth.json"
        auth.write_text(json.dumps({"openrouter": {"type": "api", "key": "sk-x"}, "opencode-go": {"type": "api"}}))
        monkeypatch.setattr(rc, "OPENCODE_AUTH_PATH", auth)
        s = rc.login_status("opencode", "/bin/opencode")
        assert s["ok"] and s["detail"] == "OpenRouter, OpenCode Go"
        assert "sk-x" not in json.dumps(s)

    def test_a_key_goes_to_the_variable_its_provider_reads(self):
        assert rc.provider_env("claude-cli", "opus") == "ANTHROPIC_API_KEY"
        assert rc.provider_env("opencode", "opencode-go/qwen3.7-max") == "OPENCODE_API_KEY"
        assert rc.provider_env("opencode", "") == "OPENROUTER_API_KEY"


class TestKeychain:
    def test_the_key_goes_over_stdin_never_argv(self):
        with patch.object(keychain, "available", return_value=True), \
             patch.object(keychain, "_vault_password", return_value="vaultpw"), \
             patch("core.keychain.subprocess.run", return_value=_proc()) as run:
            keychain.store("runner-1", "sk-ant-abc123456")
        argv, kw = run.call_args[0][0], run.call_args[1]
        assert argv == ["security", "-i"]
        assert "sk-ant-abc123456" in kw["input"] and "runner-1" in kw["input"]

    def test_a_paste_accident_is_refused_before_it_reaches_the_keychain(self):
        with patch.object(keychain, "available", return_value=True):
            for bad in ("", "short", 'has "quote" in it', "two words here"):
                with pytest.raises(keychain.KeychainError):
                    keychain.store("r", bad)


class TestTheChoiceReachesTheWorker:
    def test_a_pasted_key_lands_only_in_its_providers_variable(self):
        runner = {"id": "runner-1", "auth_mode": "key", "api_key_env": "OPENCODE_API_KEY"}
        with patch.object(keychain, "read", return_value="sk-go"):
            env = runner_profiles.build_env("opencode", runner)
        assert env["OPENCODE_API_KEY"] == "sk-go" and "OPENROUTER_API_KEY" not in env
        assert env["OPENCODE_DISABLE_DEFAULT_PLUGINS"] == "1"

    def test_own_sign_in_hides_an_inherited_anthropic_key(self):
        runner = {"auth_mode": "login", "api_key_env": ""}
        assert "ANTHROPIC_API_KEY" in runner_profiles.env_to_drop("claude-cli", runner)
        assert runner_profiles.build_env("claude-cli", runner) == {}

    def test_a_row_from_before_the_mode_keeps_its_variable(self, monkeypatch):
        monkeypatch.setenv("MY_KEY", "sk-env")
        runner = {"api_key_env": "MY_KEY"}
        assert runner_profiles.env_to_drop("claude-cli", runner) == ()
        assert runner_profiles.build_env("claude-cli", runner)["ANTHROPIC_API_KEY"] == "sk-env"
