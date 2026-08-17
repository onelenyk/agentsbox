"""Runner binary auto-detection (core/runner_detect.py)."""
import os

from core import runner_detect


def test_resolve_absolute_path(tmp_path):
    exe = tmp_path / "mybin"
    exe.write_text("#!/bin/sh\n")
    os.chmod(exe, 0o755)
    assert runner_detect.resolve(str(exe)) == str(exe)
    assert runner_detect.resolve(str(tmp_path / "missing")) is None


def test_resolve_on_path(monkeypatch):
    monkeypatch.setattr(runner_detect.shutil, "which",
                        lambda b: "/usr/bin/opencode" if b == "opencode" else None)
    assert runner_detect.resolve("opencode") == "/usr/bin/opencode"


def test_resolve_from_extra_dir(tmp_path, monkeypatch):
    # not on PATH, but present in a known install dir
    binroot = tmp_path / "bin"
    binroot.mkdir()
    exe = binroot / "opencode"
    exe.write_text("#!/bin/sh\n")
    os.chmod(exe, 0o755)
    monkeypatch.setattr(runner_detect.shutil, "which", lambda b: None)
    monkeypatch.setattr(runner_detect, "_EXTRA_DIRS", [str(binroot)])
    assert runner_detect.resolve("opencode") == str(exe)


def test_resolve_missing_returns_none(monkeypatch):
    monkeypatch.setattr(runner_detect.shutil, "which", lambda b: None)
    monkeypatch.setattr(runner_detect, "_EXTRA_DIRS", [])
    assert runner_detect.resolve("definitely-not-real") is None


def test_detect_lists_found(monkeypatch):
    monkeypatch.setattr(runner_detect, "resolve",
                        lambda b: "/opt/opencode" if b == "opencode" else None)
    found = runner_detect.detect()
    assert found == [{"name": "opencode", "type": "opencode",
                      "binary": "opencode", "path": "/opt/opencode"}]


class TestPathWithInstallDirs:
    """Resolving a runner absolutely only fixes OUR spawn. A runner that is a
    wrapper script (this machine's `claude` is one) still searches PATH for the
    real binary, and under launchd that PATH is /usr/bin:/bin:/usr/sbin:/sbin.
    """

    def test_appends_existing_install_dirs(self, monkeypatch, tmp_path):
        real = tmp_path / "toolbin"
        real.mkdir()
        monkeypatch.setattr(runner_detect, "_EXTRA_DIRS", [str(real), str(tmp_path / "nope")])
        out = runner_detect.path_with_install_dirs("/usr/bin:/bin")
        parts = out.split(os.pathsep)
        assert parts[:2] == ["/usr/bin", "/bin"], "configured PATH must keep priority"
        assert str(real) in parts
        assert str(tmp_path / "nope") not in parts, "must not add dirs that don't exist"

    def test_no_duplicates(self, monkeypatch, tmp_path):
        real = tmp_path / "toolbin"
        real.mkdir()
        monkeypatch.setattr(runner_detect, "_EXTRA_DIRS", [str(real)])
        out = runner_detect.path_with_install_dirs(str(real))
        assert out.split(os.pathsep).count(str(real)) == 1

    def test_empty_base(self, monkeypatch, tmp_path):
        real = tmp_path / "toolbin"
        real.mkdir()
        monkeypatch.setattr(runner_detect, "_EXTRA_DIRS", [str(real)])
        assert runner_detect.path_with_install_dirs("") == str(real)


def test_commit_excludes_cover_every_runner():
    """git_ops must exclude each runner's scaffolding, sourced from the
    profiles so a new runner cannot leave its files committable."""
    from core.git_ops import _COMMIT_EXCLUDES
    from core.runner_profiles import workspace_artifacts
    for path in workspace_artifacts():
        assert f":(exclude){path}" in _COMMIT_EXCLUDES, path
        assert f":(exclude){path}/**" in _COMMIT_EXCLUDES, path


def test_repo_context_recognises_every_runner_config():
    from core.repo_context import KNOWN_RESOURCES
    from core.runner_profiles import workspace_artifacts
    known = {p for p, _ in KNOWN_RESOURCES}
    for path in workspace_artifacts():
        assert path in known, path
