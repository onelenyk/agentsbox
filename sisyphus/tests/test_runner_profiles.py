"""Tests for core/runner_profiles.py — per-runner-type argv, env, MCP config."""
import json

import pytest

from core import runner_profiles as rp


class TestNormalizeType:
    def test_blank_defaults_to_opencode(self):
        assert rp.normalize_type({}) == "opencode"
        assert rp.normalize_type({"type": ""}) == "opencode"
        assert rp.normalize_type({"type": "  "}) == "opencode"

    def test_known_types(self):
        assert rp.normalize_type({"type": "opencode"}) == "opencode"
        assert rp.normalize_type({"type": "claude-cli"}) == "claude-cli"

    def test_unknown_type_raises(self):
        with pytest.raises(rp.UnsupportedRunner) as e:
            rp.normalize_type({"type": "ollama"})
        assert "ollama" in str(e.value)


class TestToolPrefix:
    def test_prefixes_follow_each_cli_convention(self):
        assert rp.tool_prefix("opencode", "sisyphus_agent") == "sisyphus_agent_"
        assert rp.tool_prefix("claude-cli", "sisyphus_agent") == "mcp__sisyphus_agent__"


class TestArgv:
    def test_opencode_shape_unchanged(self):
        args = rp.build_argv("opencode", "/bin/oc", ["--x"], "PROMPT",
                             worktree_path="/wt", agent="sisyphus-worker")
        assert args[:2] == ["/bin/oc", "run"]
        assert "--x" in args and "PROMPT" in args
        assert args[args.index("--format") + 1] == "json"
        assert "--auto" in args
        assert args[args.index("--agent") + 1] == "sisyphus-worker"
        assert args[args.index("--dir") + 1] == "/wt"

    def test_opencode_omits_dir_for_tmp(self):
        args = rp.build_argv("opencode", "oc", [], "P", worktree_path="/tmp")
        assert "--dir" not in args

    def test_claude_is_non_interactive_json(self):
        args = rp.build_argv("claude-cli", "/bin/claude", [], "PROMPT",
                             model="opus", mcp_config="/run/mcp.json")
        assert args[0] == "/bin/claude"
        assert "-p" in args
        assert "PROMPT" in args
        assert args[args.index("--model") + 1] == "opus"

    def test_claude_streams_so_the_idle_timer_stays_honest(self):
        # Plain --output-format json buffers the whole run and flushes only at
        # exit (measured: first byte == last byte), so the launcher's idle
        # timeout would kill any run that thought for longer than it — while
        # short runs passed. stream-json emits per step.
        args = rp.build_argv("claude-cli", "claude", [], "P")
        assert args[args.index("--output-format") + 1] == "stream-json"

    def test_claude_stream_json_carries_required_verbose(self):
        # Not cosmetic: the CLI hard-errors with
        # "--output-format=stream-json requires --verbose" and the run produces
        # nothing at all.
        args = rp.build_argv("claude-cli", "claude", [], "P")
        assert "--verbose" in args

    def test_claude_permission_mode_avoids_interactive_hang(self):
        # In -p mode nothing can answer a permission prompt; a gated tool call
        # would sit until the launcher's idle timeout killed the run.
        args = rp.build_argv("claude-cli", "claude", [], "P")
        assert args[args.index("--permission-mode") + 1] == "bypassPermissions"

    def test_claude_mcp_config_is_strict(self):
        # Without --strict-mcp-config the user's own globally-registered
        # servers load too, which is how the opencode side hit a name
        # collision that left the model with none of our tools.
        args = rp.build_argv("claude-cli", "claude", [], "P", mcp_config="/c.json")
        assert args[args.index("--mcp-config") + 1] == "/c.json"
        assert "--strict-mcp-config" in args

    def test_claude_omits_mcp_flags_without_config(self):
        args = rp.build_argv("claude-cli", "claude", [], "P")
        assert "--mcp-config" not in args
        assert "--strict-mcp-config" not in args

    def test_claude_never_gets_opencode_flags(self):
        args = rp.build_argv("claude-cli", "claude", [], "P", worktree_path="/wt",
                             pure=True, agent="sisyphus-worker", mcp_config="/c.json")
        for flag in ("run", "--auto", "--pure", "--agent", "--dir", "--format"):
            assert flag not in args, f"{flag} leaked into the claude command line"


class TestSuggestedConfig:
    def test_no_runner_is_prefilled_with_a_provider_key(self):
        """Both CLIs hold their own login, and for opencode the provider is
        chosen by the model's prefix — so prefilling one provider's key and
        base URL is wrong for every model routed elsewhere."""
        for rtype in rp.SUPPORTED_TYPES:
            s = rp.suggested_config(rtype)
            assert s["api_key_env"] == "", rtype
            assert s["base_url"] == "", rtype
            assert s["hint"]

    def test_unknown_type_falls_back_to_default(self):
        assert rp.suggested_config("nope") == rp.suggested_config(rp.DEFAULT_TYPE)


class TestCredentialStore:
    def test_every_supported_type_documents_its_login(self):
        # health_check relies on this to downgrade a missing env var from
        # "unhealthy" to a note.
        for rtype in rp.SUPPORTED_TYPES:
            assert rp.credential_store(rtype)

    def test_unknown_type_has_no_store(self):
        assert rp.credential_store("nope") is None


class TestEnv:
    def test_opencode_gets_openrouter_vars(self, monkeypatch):
        monkeypatch.setenv("MY_KEY", "sk-test")
        env = rp.build_env("opencode", {"api_key_env": "MY_KEY",
                                        "base_url": "https://openrouter.example"})
        assert env["MY_KEY"] == "sk-test"
        assert env["OPENROUTER_API_KEY"] == "sk-test"
        assert env["OPENROUTER_BASE_URL"] == "https://openrouter.example"

    def test_claude_never_gets_openrouter_vars(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
        env = rp.build_env("claude-cli", {"api_key_env": "ANTHROPIC_API_KEY",
                                          "base_url": "https://api.anthropic.com"})
        assert env["ANTHROPIC_API_KEY"] == "sk-ant"
        assert env["ANTHROPIC_BASE_URL"] == "https://api.anthropic.com"
        assert "OPENROUTER_API_KEY" not in env
        assert "OPENROUTER_BASE_URL" not in env

    def test_claude_without_key_is_valid(self):
        # Claude Code also authenticates by OAuth subscription from the
        # keychain — no configured key must not be treated as an error.
        env = rp.build_env("claude-cli", {"api_key_env": "", "base_url": ""})
        assert env == {}

    def test_unset_key_env_is_not_exported(self, monkeypatch):
        monkeypatch.delenv("ABSENT_KEY", raising=False)
        env = rp.build_env("opencode", {"api_key_env": "ABSENT_KEY"})
        # No credential material appears; the surface-trimming switches always do.
        assert not any("KEY" in k or "URL" in k for k in env)


class TestWriteMcpConfig:
    def _args(self, tmp_path):
        return dict(
            worktree_path=str(tmp_path / "wt"),
            output_dir=str(tmp_path / "out"),
            server_name="sisyphus_agent",
            command=["/usr/bin/python3", "/srv/bridge.py"],
            env={"SISYPHUS_SESSION_TOKEN": "tok", "SISYPHUS_DAEMON_URL": "http://d"},
            agent_name="sisyphus-worker",
        )

    def test_opencode_writes_project_config_and_returns_none(self, tmp_path):
        a = self._args(tmp_path)
        (tmp_path / "wt").mkdir()
        assert rp.write_mcp_config("opencode", **a) is None
        cfg = json.loads((tmp_path / "wt" / ".opencode" / "opencode.json").read_text())
        assert cfg["plugin"] == []            # disables the global plugin
        assert cfg["agent"]["sisyphus-worker"]["mode"] == "primary"
        server = cfg["mcp"]["sisyphus_agent"]
        assert server["command"] == ["/usr/bin/python3", "/srv/bridge.py"]
        assert server["environment"]["SISYPHUS_SESSION_TOKEN"] == "tok"

    def test_opencode_agent_always_names_a_model(self, tmp_path):
        """A primary agent with no "model" makes `opencode run` hang forever
        with no stdout and no stderr — the launcher's idle timeout eventually
        kills it and the user sees only "decider produced no decision".
        Bisected live: identical config plus a model finishes in 5s."""
        a = self._args(tmp_path)
        (tmp_path / "wt").mkdir()
        rp.write_mcp_config("opencode", model="deepseek/deepseek-v4-pro", **a)
        cfg = json.loads((tmp_path / "wt" / ".opencode" / "opencode.json").read_text())
        assert cfg["agent"]["sisyphus-worker"]["model"] == "deepseek/deepseek-v4-pro"

    def test_opencode_agent_without_model_is_flagged(self, tmp_path, caplog):
        a = self._args(tmp_path)
        (tmp_path / "wt").mkdir()
        with caplog.at_level("WARNING"):
            rp.write_mcp_config("opencode", model="", **a)
        cfg = json.loads((tmp_path / "wt" / ".opencode" / "opencode.json").read_text())
        # No invented default — but the run that is about to hang says so.
        assert "model" not in cfg["agent"]["sisyphus-worker"]
        assert "hangs" in caplog.text

    def _probeable(self, tmp_path, monkeypatch):
        """A worker HOME that carries an opencode config dir AND whose sweep
        did not run.

        In production runner_home.build() now sweeps a stray .config/ away on
        every spawn, so the probe's short-circuit ("no config dir, nothing to
        find") is the normal path. The probe remains the belt to the sweep's
        braces — it is what still disables a foreign roster if a sweep failed
        (an OSError, a file lock) or something recreated the config between
        build and launch. These tests pin that belt, so they bypass the sweep
        by handing the probe a pre-polluted home directly."""
        rp._FOREIGN_CACHE.clear()
        ws = tmp_path / "ws"
        home = ws / "runner-home"
        (home / ".config" / "opencode").mkdir(parents=True)
        monkeypatch.setenv("WORKSPACE_ROOT", str(ws))
        import core.runner_home
        monkeypatch.setattr(core.runner_home, "build",
                            lambda root, real_home="": str(home))

    def test_the_users_global_mcp_roster_is_disabled_by_name(self, tmp_path, monkeypatch):
        """A live run's roster carried figma, playwright, macos-use, engram and
        websearch: opencode merges the user's global config into every run.
        Neither OPENCODE_CONFIG_DIR nor OPENCODE_CONFIG suppresses that (both
        measured); a project-level {"enabled": false} does."""
        self._probeable(tmp_path, monkeypatch)
        fake = tmp_path / "fake-opencode"
        fake.write_text(
            "#!/bin/sh\n"
            'echo \'{"mcp": {"figma": {"type": "remote", "url": "https://figma",'
            ' "enabled": true}, "playwright": {"type": "local",'
            ' "command": ["npx", "playwright"], "environment": {"TOKEN": "sekrit"},'
            ' "enabled": true}}}\'\n')
        fake.chmod(0o755)

        a = self._args(tmp_path)
        (tmp_path / "wt").mkdir()
        rp.write_mcp_config("opencode", binary=str(fake), model="m", **a)
        cfg = json.loads((tmp_path / "wt" / ".opencode" / "opencode.json").read_text())

        assert cfg["mcp"]["figma"] == {"enabled": False}
        assert cfg["mcp"]["playwright"] == {"enabled": False}
        assert cfg["mcp"]["sisyphus_agent"]["enabled"] is True

    def test_disabling_never_copies_the_users_credentials_into_the_worktree(
            self, tmp_path, monkeypatch):
        """The stub is bare on purpose: flipping a flag on a copy of the
        original entry would write the user's own URLs and environment into a
        file inside the worktree, which the agent reads."""
        self._probeable(tmp_path, monkeypatch)
        fake = tmp_path / "fake-opencode"
        fake.write_text(
            "#!/bin/sh\n"
            'echo \'{"mcp": {"playwright": {"type": "local",'
            ' "command": ["npx", "playwright"], "environment": {"TOKEN": "sekrit"},'
            ' "enabled": true}}}\'\n')
        fake.chmod(0o755)

        a = self._args(tmp_path)
        (tmp_path / "wt").mkdir()
        rp.write_mcp_config("opencode", binary=str(fake), model="m", **a)
        written = (tmp_path / "wt" / ".opencode" / "opencode.json").read_text()
        assert "sekrit" not in written and "npx" not in written

    def test_a_binary_that_cannot_be_probed_never_breaks_the_run(self, tmp_path):
        a = self._args(tmp_path)
        (tmp_path / "wt").mkdir()
        rp.write_mcp_config("opencode", binary=str(tmp_path / "nope"), model="m", **a)
        cfg = json.loads((tmp_path / "wt" / ".opencode" / "opencode.json").read_text())
        assert cfg["mcp"]["sisyphus_agent"]["enabled"] is True

    def test_the_probe_runs_under_the_home_the_worker_will_get(self, tmp_path,
                                                              monkeypatch):
        """It used to probe with the DAEMON's home, so once worker runs got a
        home of their own it reported the user's global servers and wrote stubs
        disabling things that were never going to be there — dead weight that
        reads as protection."""
        fake = tmp_path / "fake-opencode"
        fake.write_text('#!/bin/sh\necho "{\\"mcp\\": {\\"$HOME\\": {}}}"\n')
        fake.chmod(0o755)
        self._probeable(tmp_path, monkeypatch)

        seen = rp.foreign_mcp_servers(str(fake))

        assert seen and seen[0].endswith("runner-home"), seen

    def test_a_home_with_no_config_is_not_worth_asking_about(self, tmp_path,
                                                             monkeypatch):
        """The production case. Our allowlist links no config dir, so there is
        nothing a foreign server could come from — and asking anyway costs the
        binary a full package-cache bootstrap against a home it has never
        seen."""
        rp._FOREIGN_CACHE.clear()
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "ws"))
        ran = tmp_path / "ran"
        fake = tmp_path / "fake-opencode"
        fake.write_text(f"#!/bin/sh\ntouch {ran}\necho '{{}}'\n")
        fake.chmod(0o755)

        assert rp.foreign_mcp_servers(str(fake)) == []
        assert not ran.exists(), "it must not have spawned the binary at all"

    def test_the_probe_reads_what_the_binary_resolves(self, tmp_path, monkeypatch):
        """Asked, not assumed: `opencode debug config` prints the configuration
        the binary will actually load, so this cannot drift from wherever
        opencode decides its config files live."""
        self._probeable(tmp_path, monkeypatch)
        fake = tmp_path / "fake-opencode"
        fake.write_text("#!/bin/sh\necho '{\"mcp\": {\"b\": {}, \"a\": {}}}'\n")
        fake.chmod(0o755)
        assert rp.foreign_mcp_servers(str(fake)) == ["a", "b"]
        assert rp.foreign_mcp_servers("") == []

    def test_claude_ignores_model_in_config(self, tmp_path):
        # Claude Code takes its model on the command line, not in MCP config.
        a = self._args(tmp_path)
        (tmp_path / "wt").mkdir()
        cfg = json.loads(open(rp.write_mcp_config("claude-cli", model="opus", **a)).read())
        assert "agent" not in cfg and "model" not in cfg

    def test_claude_writes_outside_the_worktree(self, tmp_path):
        # The worktree is a real git checkout whose porcelain status is diffed
        # and salvaged; config must not show up as an untracked file there.
        a = self._args(tmp_path)
        (tmp_path / "wt").mkdir()
        path = rp.write_mcp_config("claude-cli", **a)
        assert path is not None
        assert not (tmp_path / "wt" / "mcp-config.json").exists()
        assert not (tmp_path / "wt" / ".opencode" / "opencode.json").exists()
        assert str(tmp_path / "out") in path

    def test_claude_config_shape(self, tmp_path):
        a = self._args(tmp_path)
        (tmp_path / "wt").mkdir()
        cfg = json.loads(open(rp.write_mcp_config("claude-cli", **a)).read())
        server = cfg["mcpServers"]["sisyphus_agent"]
        assert server["command"] == "/usr/bin/python3"
        assert server["args"] == ["/srv/bridge.py"]
        assert server["env"]["SISYPHUS_SESSION_TOKEN"] == "tok"


class TestWorkspaceArtifacts:
    """What each CLI leaves in a working tree. core.git_ops excludes these from
    commits; listing only opencode's meant a Claude worker — whose cwd is the
    same worktree — could commit its own state directory."""

    def test_every_supported_type_declares_its_artifacts(self):
        for rtype in rp.SUPPORTED_TYPES:
            assert rp.workspace_artifacts(rtype), rtype

    def test_claude_state_dir_is_known(self):
        assert ".claude" in rp.workspace_artifacts("claude-cli")

    def test_opencode_config_is_known(self):
        assert "opencode.json" in rp.workspace_artifacts("opencode")

    def test_union_covers_all_runners(self):
        allp = rp.workspace_artifacts()
        for rtype in rp.SUPPORTED_TYPES:
            for path in rp.workspace_artifacts(rtype):
                assert path in allp
        assert len(allp) == len(set(allp)), "no duplicates"

    def test_labels_pair_with_paths(self):
        labels = rp.workspace_artifact_labels()
        assert {p for p, _ in labels} == set(rp.workspace_artifacts())
        assert all(lbl for _, lbl in labels)

    def test_unknown_type_has_no_artifacts(self):
        assert rp.workspace_artifacts("nope") == []


def test_opencode_env_trims_the_users_global_tool_surface():
    """A live run's roster showed the user's global plugins and skills merged
    into a worker (call_omo_agent, figma_*, playwright_* — task-60088d8f log).
    The opencode overlay must carry the CLI's own kill switches for the plugin
    and skill share of that; claude-cli needs none (--strict-mcp-config)."""
    from core.runner_profiles import build_env
    env = build_env("opencode", {})
    assert env.get("OPENCODE_DISABLE_DEFAULT_PLUGINS") == "1"
    assert env.get("OPENCODE_DISABLE_EXTERNAL_SKILLS") == "1"
    assert env.get("OPENCODE_DISABLE_CLAUDE_CODE_SKILLS") == "1"
    assert "OPENCODE_DISABLE_DEFAULT_PLUGINS" not in build_env("claude-cli", {})


class TestARepoWithItsOwnOpencodeConfig:
    """The open gap of 2026-08-25, closed: a checkout's own opencode config
    used to win over ours (plugin, by file precedence) or merge into the run
    (mcp, from either file). Our config now occupies the measured winning
    slot — .opencode/opencode.json — replacing the repo's file in the
    throwaway checkout, and every server the repo declared anywhere is
    disabled by name."""

    def _args(self, tmp_path):
        return dict(
            worktree_path=str(tmp_path / "wt"),
            output_dir=str(tmp_path / "out"),
            server_name="sisyphus_agent",
            command=["/usr/bin/python3", "/srv/bridge.py"],
            env={"SISYPHUS_SESSION_TOKEN": "tok"},
            agent_name="sisyphus-worker",
        )

    def test_the_repos_own_winning_slot_is_replaced(self, tmp_path):
        wt = tmp_path / "wt"
        (wt / ".opencode").mkdir(parents=True)
        (wt / ".opencode" / "opencode.json").write_text(json.dumps({
            "plugin": ["oh-my-opencode@latest"],
            "mcp": {"sneaky": {"type": "remote", "url": "http://evil"}},
        }))
        rp.write_mcp_config("opencode", **self._args(tmp_path))
        cfg = json.loads((wt / ".opencode" / "opencode.json").read_text())
        assert cfg["plugin"] == []                      # theirs is gone with the file
        assert cfg["mcp"]["sneaky"] == {"enabled": False}
        assert "url" not in cfg["mcp"]["sneaky"]        # bare stub, no copied config
        assert cfg["mcp"]["sisyphus_agent"]["enabled"] is True

    def test_a_root_config_is_disabled_by_name_not_trusted(self, tmp_path):
        """mcp entries merge across files no matter which one wins, so a
        server declared at the worktree root still needs a by-name stub in
        our winning-slot file."""
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / "opencode.json").write_text(json.dumps({
            "mcp": {"lsp": {}, "sisyphus_agent": {"type": "remote", "url": "http://fake"}},
        }))
        rp.write_mcp_config("opencode", **self._args(tmp_path))
        cfg = json.loads((wt / ".opencode" / "opencode.json").read_text())
        assert cfg["mcp"]["lsp"] == {"enabled": False}
        # a repo redefining OUR server name must not survive either way:
        assert cfg["mcp"]["sisyphus_agent"]["command"] == ["/usr/bin/python3", "/srv/bridge.py"]

    def test_repo_declared_reads_both_files_and_reports_unparseable(self, tmp_path):
        wt = tmp_path / "wt"
        (wt / ".opencode").mkdir(parents=True)
        (wt / "opencode.json").write_text(json.dumps({"mcp": {"a": {}}, "plugin": ["p1"]}))
        (wt / ".opencode" / "opencode.json").write_text("{not json")
        d = rp.repo_declared_opencode(str(wt))
        assert d["mcp"] == ["a"] and d["plugin"] == ["p1"]
        assert d["files"] == ["opencode.json", ".opencode/opencode.json (unparseable)"]

    def test_a_clean_checkout_declares_nothing(self, tmp_path):
        (tmp_path / "wt").mkdir()
        d = rp.repo_declared_opencode(str(tmp_path / "wt"))
        assert d == {"mcp": [], "plugin": [], "files": []}
