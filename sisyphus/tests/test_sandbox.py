"""Sandbox backend — profile shape, plus a real end-to-end enforcement check
that sandbox-exec actually denies a write to a protected path on this host."""
import os
import platform
import subprocess

import pytest

from core import sandbox


def test_profile_protects_paths_and_reallows_workspace(tmp_path):
    ws = tmp_path / "workspace"
    repo = tmp_path / "repo"          # a "repo source" to protect
    prof = sandbox.build_seatbelt_profile(
        str(ws / "wt"), str(ws / "out"), workspace_root=str(ws),
        home=str(tmp_path), protected=[str(repo)])
    assert "(allow default)" in prof
    assert "(deny file-write*" in prof and str(repo.resolve()) in prof
    # workspace is re-allowed AFTER the deny (last matching rule wins in SBPL)
    assert prof.index("(deny file-write*") < prof.index("(allow file-write*")
    assert str(ws.resolve()) in prof.split("(allow file-write*")[1]


def test_wrap_is_noop_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("SISYPHUS_SANDBOX", "0")
    args = ["opencode", "run", "hi"]
    assert sandbox.wrap(args, str(tmp_path), str(tmp_path)) == args


def test_wrap_prefixes_sandbox_exec_when_enabled(monkeypatch, tmp_path):
    if not sandbox.available():
        pytest.skip("no sandbox backend on this platform")
    monkeypatch.delenv("SISYPHUS_SANDBOX", raising=False)
    wrapped = sandbox.wrap(["echo", "hi"], str(tmp_path), str(tmp_path))
    assert wrapped[0] == "/usr/bin/sandbox-exec" and wrapped[1] == "-f"
    assert wrapped[-2:] == ["echo", "hi"]
    assert (tmp_path / "sandbox.sb").exists()


def _sandbox_can_apply() -> bool:
    """Seatbelt cannot nest: inside an existing sandbox-exec, sandbox_apply is
    refused by the kernel. Probe with a trivial allow-all profile so the
    enforcement test SKIPS in that environment instead of failing with a
    misleading 'workspace write was blocked' assertion (workers running this
    suite under the daemon's sandbox hit exactly that)."""
    r = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)", "/usr/bin/true"],
        capture_output=True, text=True,
    )
    return r.returncode == 0


@pytest.mark.skipif(platform.system() != "Darwin" or not os.path.exists("/usr/bin/sandbox-exec"),
                    reason="Seatbelt only on macOS")
def test_enforcement_blocks_protected_write_allows_workspace(tmp_path):
    if not _sandbox_can_apply():
        pytest.skip("nested sandbox — sandbox_apply refused, enforcement can't be probed here")
    """The real proof: under the profile a write into the workspace succeeds and
    a write into a protected 'repo source' is refused by the kernel — even though
    the workspace lives inside that same protected tree."""
    repo = tmp_path / "repo"                 # stands in for a real source repo
    ws = repo / "sub" / "workspace"          # workspace nested inside the repo, re-allowed
    ws.mkdir(parents=True)
    out = tmp_path / "out"; out.mkdir()
    profile = out / "p.sb"
    profile.write_text(sandbox.build_seatbelt_profile(
        str(ws), str(out), workspace_root=str(ws), home=str(tmp_path), protected=[str(repo)]))

    def run_write(target):
        return subprocess.run(
            ["/usr/bin/sandbox-exec", "-f", str(profile), "/bin/sh", "-c", f"echo x > {target}"],
            capture_output=True, text=True)

    ok = run_write(ws / "ok.txt")
    assert ok.returncode == 0, f"write inside workspace was blocked: {ok.stderr}"
    assert (ws / "ok.txt").exists()

    escape = repo / "escape.txt"             # inside the protected repo, outside workspace
    blocked = run_write(escape)
    assert blocked.returncode != 0, "write into the protected repo was NOT blocked — sandbox failed"
    assert not escape.exists()
