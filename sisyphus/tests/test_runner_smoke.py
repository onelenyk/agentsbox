"""Tests for core/runner_smoke.py — the end-to-end runner probe.

The probe exists because core.preflight's "does the binary resolve" check passed
during every runner failure this system has hit, each of which then surfaced as
"worker ended without reporting a result". What matters here is that a failure
is attributed to the right *stage*, since that attribution is the entire point.
"""
import asyncio
import json

from core import runner_smoke


def _runner(**over):
    r = {"name": "probe", "binary": "opencode", "type": "opencode",
         "model": "some/model", "api_key_env": "", "base_url": "", "extra_args": "[]"}
    r.update(over)
    return r


class TestStageAttribution:
    def test_missing_binary_is_a_binary_failure(self):
        res = asyncio.run(runner_smoke.smoke_test(
            _runner(binary="definitely-not-installed-xyz"), timeout=5))
        assert res["ok"] is False
        assert res["stage"] == "binary"
        assert "not found" in res["detail"]

    def test_unlaunchable_type_is_named_not_guessed(self):
        # Previously this would have been fed opencode's flags and failed
        # somewhere deep inside the wrong binary.
        res = asyncio.run(runner_smoke.smoke_test(_runner(type="ollama"), timeout=5))
        assert res["ok"] is False
        assert res["stage"] == "binary"
        assert "ollama" in res["detail"]

    def test_a_hang_is_reported_as_hanging(self, monkeypatch):
        """The signature failure: the CLI produces nothing at all. Reported as a
        hang with a timeout, never as a silent pass."""
        async def fake_exec(*a, **kw):
            class P:
                returncode = None
                async def communicate(self):
                    await asyncio.sleep(30)
                    return b"", b""
                def kill(self): pass
                async def wait(self): return 0
            return P()
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(runner_smoke, "_SMOKE_SERVER", "/dev/null")
        res = asyncio.run(runner_smoke.smoke_test(
            _runner(binary="/bin/sh"), timeout=1))
        assert res["ok"] is False
        assert res["stage"] == "output"
        assert "hanging" in res["detail"]

    def test_silent_exit_surfaces_stderr(self, monkeypatch):
        """When a runner dies quietly, its stderr is the only explanation there
        is — the wrapper-script failure that killed every Claude worker said
        exactly what was wrong and nobody ever saw it."""
        async def fake_exec(*a, **kw):
            class P:
                returncode = 1
                async def communicate(self):
                    return b"", b"Superset: claude not found in PATH."
                def kill(self): pass
                async def wait(self): return 1
            return P()
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        res = asyncio.run(runner_smoke.smoke_test(_runner(binary="/bin/sh"), timeout=5))
        assert res["ok"] is False
        assert res["stage"] == "output"
        assert "not found in PATH" in res["detail"]
        assert res["exit_code"] == 1

    def test_output_without_a_tool_call_is_its_own_stage(self, monkeypatch):
        """A CLI that runs and talks but never reaches our MCP server is a
        different fault from one that never started, and needs a different fix."""
        async def fake_exec(*a, **kw):
            class P:
                returncode = 0
                async def communicate(self):
                    return b'{"type":"text","text":"hello"}', b""
                def kill(self): pass
                async def wait(self): return 0
            return P()
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        res = asyncio.run(runner_smoke.smoke_test(_runner(binary="/bin/sh"), timeout=5))
        assert res["ok"] is False
        assert res["stage"] == "tool_call"
        assert "never called" in res["detail"]

    def test_success_requires_the_tool_to_have_been_called(self, monkeypatch):
        """Passing means the model reached the tool — not merely that the
        process exited zero."""
        import os

        async def fake_exec(*a, **kw):
            # Stand in for the CLI actually driving our stdio MCP server.
            cwd = kw.get("cwd")
            with open(os.path.join(cwd, "called.json"), "w") as fh:
                json.dump({"name": "report", "arguments": {"status": "ok"}}, fh)

            class P:
                returncode = 0
                async def communicate(self):
                    return b'{"type":"result"}', b""
                def kill(self): pass
                async def wait(self): return 0
            return P()
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        res = asyncio.run(runner_smoke.smoke_test(_runner(binary="/bin/sh"), timeout=5))
        assert res["ok"] is True
        assert res["stage"] == "tool_call"
        assert res["tool_called"] is True


class TestProbeUsesTheProductionPath:
    def test_tool_name_follows_the_runners_own_convention(self, monkeypatch):
        """The probe must ask for the tool by the name that runner's CLI would
        actually expose, or it tests nothing about tool naming — which is one of
        the things most likely to be wrong for a new runner."""
        seen = {}

        async def fake_exec(*a, **kw):
            seen["argv"] = a
            class P:
                returncode = 0
                async def communicate(self): return b"out", b""
                def kill(self): pass
                async def wait(self): return 0
            return P()
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

        asyncio.run(runner_smoke.smoke_test(_runner(binary="/bin/sh"), timeout=5))
        assert any("sisyphus_smoke_report" in str(x) for x in seen["argv"])

        seen.clear()
        asyncio.run(runner_smoke.smoke_test(
            _runner(binary="/bin/sh", type="claude-cli"), timeout=5))
        assert any("mcp__sisyphus_smoke__report" in str(x) for x in seen["argv"])

    def test_opencode_probe_config_names_a_model(self, tmp_path):
        """The probe writes the same config shape production does, so it must
        carry a model — an opencode agent without one hangs the CLI."""
        from core import runner_profiles
        runner_profiles.write_mcp_config(
            "opencode", worktree_path=str(tmp_path), output_dir=str(tmp_path),
            server_name="sisyphus_smoke", command=["/bin/true"], env={},
            agent_name="sisyphus-smoke", model="some/model")
        cfg = json.loads((tmp_path / "opencode.json").read_text())
        assert cfg["agent"]["sisyphus-smoke"]["model"] == "some/model"
