"""Unit and process tests for sandbox read isolation (opt-in read-deny for home)."""
import os
import platform
import subprocess
import tempfile

import pytest

from core import sandbox


class TestReadIsolationProfileShape:
    """Unit tests for read_isolation profile generation — flag behavior and rule
    ordering without requiring sandbox-exec to actually enforce anything."""

    def test_flag_off_no_read_deny_in_profile(self, monkeypatch, tmp_path):
        """When read_isolation is off (the default), the profile is unchanged
        from the original behavior — no file-read* deny and no extra allow."""
        monkeypatch.setattr("core.config.get_config",
                           lambda key=None, default=None: False if key == "sandbox.read_isolation" else default)
        ws = tmp_path / "workspace"
        prof = sandbox.build_seatbelt_profile(
            str(ws / "wt"), str(ws / "out"), workspace_root=str(ws),
            home=str(tmp_path), protected=[])
        # Should have only write rules, no read rules
        assert "(deny file-read*" not in prof
        assert "(allow file-read*" not in prof
        assert "(allow default)" in prof

    def test_flag_on_includes_read_deny_and_allowlist(self, monkeypatch, tmp_path):
        """When read_isolation is on, the profile includes:
        - (deny file-read* (subpath $HOME))
        - (allow file-read* (subpath $HOME/.local/share/opencode) ...) — all allowlisted paths
        - workspace paths are re-allowed for reads"""
        monkeypatch.setattr("core.config.get_config",
                           lambda key=None, default=None: True if key == "sandbox.read_isolation" else default)
        # Create some of the allowlisted paths so they're included in the profile
        home = tmp_path
        (home / ".local" / "share" / "opencode").mkdir(parents=True, exist_ok=True)
        (home / ".claude").mkdir(parents=True, exist_ok=True)
        (home / ".gitconfig").touch()

        ws = tmp_path / "workspace"
        prof = sandbox.build_seatbelt_profile(
            str(ws / "wt"), str(ws / "out"), workspace_root=str(ws),
            home=str(home), protected=[])

        # Read deny rule must be present
        assert "(deny file-read*" in prof
        assert f"(subpath \"{home.resolve()}\")" in prof

        # Allowlisted paths must be re-allowed
        assert "(allow file-read*" in prof
        assert str((home / ".local" / "share" / "opencode").resolve()) in prof
        assert str((home / ".claude").resolve()) in prof
        assert str((home / ".gitconfig").resolve()) in prof

    def test_workspace_paths_are_reallowed_for_reads(self, monkeypatch, tmp_path):
        """Even when home is read-denied, workspace paths are re-allowed for reads.
        This is critical because the workspace is likely under a protected repo
        source, and the worker needs to read its own worktree."""
        monkeypatch.setattr("core.config.get_config",
                           lambda key=None, default=None: True if key == "sandbox.read_isolation" else default)
        home = tmp_path / "home"
        home.mkdir()
        ws = tmp_path / "repo" / "workspace"  # workspace inside a protected repo
        ws.mkdir(parents=True)
        wt = ws / "wt"
        out = ws / "out"

        prof = sandbox.build_seatbelt_profile(
            str(wt), str(out), workspace_root=str(ws),
            home=str(home), protected=[str(tmp_path / "repo")])

        # Workspace paths must appear in the read-allow rule
        allow_read_section = prof[prof.find("(allow file-read*"):prof.find("(allow file-read*") + 1000]
        assert str(ws.resolve()) in allow_read_section
        assert str(wt.resolve()) in allow_read_section
        assert str(out.resolve()) in allow_read_section

    def test_read_isolation_rule_ordering(self, monkeypatch, tmp_path):
        """The read deny must come AFTER write rules but BEFORE workspace paths
        are re-allowed. Verify the rough ordering by position in the profile."""
        monkeypatch.setattr("core.config.get_config",
                           lambda key=None, default=None: True if key == "sandbox.read_isolation" else default)
        home = tmp_path
        (home / ".local").mkdir()
        ws = tmp_path / "ws"

        prof = sandbox.build_seatbelt_profile(
            str(ws / "wt"), str(ws / "out"), workspace_root=str(ws),
            home=str(home), protected=[])

        allow_default_at = prof.find("(allow default)")
        write_deny_at = prof.find("(deny file-write*")
        read_deny_at = prof.find("(deny file-read*")
        read_allow_at = prof.find("(allow file-read*")

        # Order must be: allow default → write deny → write allow (not checked here)
        # → read deny → read allow
        assert allow_default_at >= 0
        if write_deny_at >= 0:
            assert allow_default_at < write_deny_at
        if read_deny_at >= 0:
            assert allow_default_at < read_deny_at
        if read_allow_at >= 0 and read_deny_at >= 0:
            assert read_deny_at < read_allow_at


@pytest.mark.skipif(platform.system() != "Darwin" or not os.path.exists("/usr/bin/sandbox-exec"),
                    reason="Seatbelt only on macOS")
class TestReadIsolationEnforcement:
    """Process tests that actually run sandbox-exec to verify enforcement.
    Skipped on non-Darwin or if sandbox-exec is not available."""

    def _sandbox_can_apply(self) -> bool:
        """Seatbelt cannot nest. Probe with a trivial allow-all profile."""
        r = subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)", "/usr/bin/true"],
            capture_output=True, text=True,
        )
        return r.returncode == 0

    def test_read_isolation_blocks_unallowlisted_home_read_allows_workspace_read(self):
        """Live enforcement: a file outside the allowlist under $HOME is
        unreadable, a file inside the workspace is readable."""
        if not self._sandbox_can_apply():
            pytest.skip("nested sandbox — sandbox_apply refused, enforcement can't be probed here")

        home = tempfile.mkdtemp(prefix="sisyphus-sandbox-test-")
        try:
            ws = os.path.join(home, "workspace")
            os.makedirs(ws)

            # Create a temp file under home that is NOT in the allowlist
            secret_dir = os.path.join(home, "secret-project")
            os.makedirs(secret_dir)
            secret_file = os.path.join(secret_dir, "secret.txt")
            with open(secret_file, "w") as f:
                f.write("confidential")

            # Create a file in the workspace
            ws_file = os.path.join(ws, "workspace.txt")
            with open(ws_file, "w") as f:
                f.write("workspace")

            # Build profile with read_isolation on (force it via monkeypatch during
            # profile generation)
            import core.sandbox
            original_func = core.sandbox._read_isolation_enabled
            try:
                core.sandbox._read_isolation_enabled = lambda: True
                prof = core.sandbox.build_seatbelt_profile(
                    os.path.join(ws, "wt"), os.path.join(ws, "out"),
                    workspace_root=ws, home=home, protected=[])
            finally:
                core.sandbox._read_isolation_enabled = original_func

            prof_path = os.path.join(ws, "p.sb")
            with open(prof_path, "w") as f:
                f.write(prof)

            # Try to read the secret file through sandbox-exec
            def run_read(target):
                return subprocess.run(
                    ["/usr/bin/sandbox-exec", "-f", prof_path, "/bin/cat", target],
                    capture_output=True, text=True)

            # Secret file should be denied (unless it's allowlisted, which it shouldn't be)
            secret_blocked = run_read(secret_file)
            assert secret_blocked.returncode != 0, f"secret file was readable: {secret_blocked.stderr}"
            assert not os.path.exists(os.path.join(secret_dir, "copy.txt")), \
                "write was somehow allowed"

            # Workspace file should be allowed
            ws_allowed = run_read(ws_file)
            assert ws_allowed.returncode == 0, f"workspace file was blocked: {ws_allowed.stderr}"
            assert "workspace" in ws_allowed.stdout
        finally:
            import shutil
            shutil.rmtree(home, ignore_errors=True)

    def test_allowlisted_paths_are_readable_under_isolation(self):
        """Live enforcement: an allowlisted path like .gitconfig IS readable
        even when read_isolation is on."""
        if not self._sandbox_can_apply():
            pytest.skip("nested sandbox — sandbox_apply refused, enforcement can't be probed here")

        home = tempfile.mkdtemp(prefix="sisyphus-sandbox-test-")
        try:
            ws = os.path.join(home, "workspace")
            os.makedirs(ws)

            # Create .gitconfig which is allowlisted
            gitconfig = os.path.join(home, ".gitconfig")
            with open(gitconfig, "w") as f:
                f.write("[user]\n  name = Test")

            # Build profile with read_isolation on
            import core.sandbox
            original_func = core.sandbox._read_isolation_enabled
            try:
                core.sandbox._read_isolation_enabled = lambda: True
                prof = core.sandbox.build_seatbelt_profile(
                    os.path.join(ws, "wt"), os.path.join(ws, "out"),
                    workspace_root=ws, home=home, protected=[])
            finally:
                core.sandbox._read_isolation_enabled = original_func

            prof_path = os.path.join(ws, "p.sb")
            with open(prof_path, "w") as f:
                f.write(prof)

            # Try to read .gitconfig through sandbox-exec
            result = subprocess.run(
                ["/usr/bin/sandbox-exec", "-f", prof_path, "/bin/cat", gitconfig],
                capture_output=True, text=True)

            assert result.returncode == 0, f".gitconfig was blocked: {result.stderr}"
            assert "[user]" in result.stdout
        finally:
            import shutil
            shutil.rmtree(home, ignore_errors=True)
