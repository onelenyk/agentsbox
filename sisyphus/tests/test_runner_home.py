"""The worker's own HOME.

opencode resolves config, cache, data and state from $HOME, ahead of every
variable meant to move them: a live roster was unchanged by XDG_CONFIG_HOME,
XDG_CACHE_HOME, OPENCODE_CONFIG, OPENCODE_CONFIG_DIR, --pure, an empty project
"plugin": [] and all three OPENCODE_DISABLE_* switches. Under a private HOME the
same probe returns `plugin: []` and only our own MCP server.
"""
import errno
import logging
import os
import pathlib
import shutil

from core import runner_home


def _fake_home(tmp_path):
    home = tmp_path / "real-home"
    (home / ".local" / "share" / "opencode").mkdir(parents=True)
    (home / ".local" / "share" / "opencode" / "auth.json").write_text("{}")
    (home / ".claude").mkdir()
    (home / ".gitconfig").write_text("[user]\n\tname = t\n")
    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".config" / "opencode" / "opencode.json").write_text(
        '{"plugin": ["oh-my-opencode@latest"]}')
    (home / ".aws").mkdir()
    (home / ".aws" / "credentials").write_text("[default]\n")
    return home


class TestWhatAWorkerInherits:
    def test_the_credentials_it_needs_come_through(self, tmp_path):
        home = runner_home.build(str(tmp_path / "ws"), real_home=str(_fake_home(tmp_path)))
        assert os.path.exists(os.path.join(home, ".local/share/opencode/auth.json"))
        assert os.path.exists(os.path.join(home, ".gitconfig"))

    def test_the_config_carrying_plugins_does_not(self, tmp_path):
        """The whole point. ~/.config/opencode is where the plugin list lives,
        and it is not on the allowlist."""
        home = runner_home.build(str(tmp_path / "ws"), real_home=str(_fake_home(tmp_path)))
        assert not os.path.exists(os.path.join(home, ".config/opencode/opencode.json"))

    def test_nothing_arrives_that_was_not_named(self, tmp_path):
        """An allowlist, not a denylist: a worker gets what someone decided it
        should have, never whatever the machine happens to hold."""
        home = runner_home.build(str(tmp_path / "ws"), real_home=str(_fake_home(tmp_path)))
        assert not os.path.exists(os.path.join(home, ".aws"))
        assert sorted(os.listdir(home)) == [".gitconfig", ".local"]

    def test_it_links_rather_than_copies(self, tmp_path):
        """A copy of a credential file is a second credential file, and it goes
        stale the first time the user logs in again."""
        real = _fake_home(tmp_path)
        home = runner_home.build(str(tmp_path / "ws"), real_home=str(real))
        link = os.path.join(home, ".gitconfig")
        assert os.path.islink(link)
        assert os.readlink(link) == str(real / ".gitconfig")


class TestRebuilding:
    def test_running_twice_changes_nothing(self, tmp_path):
        real = _fake_home(tmp_path)
        first = runner_home.build(str(tmp_path / "ws"), real_home=str(real))
        before = sorted(os.listdir(first))
        assert runner_home.build(str(tmp_path / "ws"), real_home=str(real)) == first
        assert sorted(os.listdir(first)) == before

    def test_a_stale_link_is_repointed(self, tmp_path):
        """The home outlives a run; the thing it points at may move."""
        real = _fake_home(tmp_path)
        home = runner_home.build(str(tmp_path / "ws"), real_home=str(real))
        link = os.path.join(home, ".gitconfig")
        os.unlink(link)
        os.symlink(str(tmp_path / "somewhere-else"), link)

        runner_home.build(str(tmp_path / "ws"), real_home=str(real))
        assert os.readlink(link) == str(real / ".gitconfig")

    def test_a_real_file_someone_put_there_is_left_alone(self, tmp_path):
        real = _fake_home(tmp_path)
        home = runner_home.build(str(tmp_path / "ws"), real_home=str(real))
        link = os.path.join(home, ".gitconfig")
        os.unlink(link)
        with open(link, "w") as fh:
            fh.write("[user]\n\tname = deliberate\n")

        runner_home.build(str(tmp_path / "ws"), real_home=str(real))
        assert not os.path.islink(link)
        assert "deliberate" in open(link).read()

    def test_a_missing_source_is_skipped_quietly(self, tmp_path):
        """Most machines will not have every entry on the list."""
        bare = tmp_path / "bare-home"
        bare.mkdir()
        home = runner_home.build(str(tmp_path / "ws"), real_home=str(bare))
        assert os.listdir(home) == []

    def test_an_unbuildable_home_falls_back_to_the_real_one(self, tmp_path):
        """Better a run that works with too much than no run at all — and the
        fallback is loud in the log rather than silent."""
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("")
        real = str(_fake_home(tmp_path))
        assert runner_home.build(str(blocker), real_home=real) == real


class TestSweepingStrays:
    """The home persists between runs, and a run can write into it: a repo's
    own opencode config once made opencode install a plugin, leaving .config/,
    .cache/opencode/packages/, .omo/ and .npm/ behind for every LATER worker
    on every repo to inherit (docs/known-gaps.md). build() sweeps what the
    allowlist did not put there, so each spawn starts from the allowlist."""

    def test_a_previous_runs_leavings_are_swept(self, tmp_path):
        real = _fake_home(tmp_path)
        home = runner_home.build(str(tmp_path / "ws"), real_home=str(real))
        # what the hostile-config probe measured being left behind:
        for stray in (".config/opencode/node_modules", ".cache/opencode/packages",
                      ".omo", ".npm", "Library"):
            (pathlib.Path(home) / stray).mkdir(parents=True)
        (pathlib.Path(home) / ".wget-hsts").write_text("x")

        runner_home.build(str(tmp_path / "ws"), real_home=str(real))

        left = set(os.listdir(home))
        assert not left & {".config", ".cache", ".omo", ".npm", "Library", ".wget-hsts"}

    def test_the_allowlist_survives_the_sweep(self, tmp_path):
        real = _fake_home(tmp_path)
        home = runner_home.build(str(tmp_path / "ws"), real_home=str(real))
        before = sorted(os.listdir(home))
        (pathlib.Path(home) / ".cache").mkdir()
        runner_home.build(str(tmp_path / "ws"), real_home=str(real))
        assert sorted(os.listdir(home)) == before

    def test_a_dir_that_fails_once_is_swept(self, tmp_path, caplog, monkeypatch):
        """A directory whose contents change while it is removed raises
        ENOTEMPTY; the retry removes whatever appeared in between, so the
        stray is gone and no error is logged (it still counts as swept)."""
        real = _fake_home(tmp_path)
        home = runner_home.build(str(tmp_path / "ws"), real_home=str(real))
        stray = pathlib.Path(home) / ".config" / "opencode" / "node_modules"
        stray.mkdir(parents=True)
        (stray / "x").write_text("x")

        real_rmtree = shutil.rmtree
        state = {"fails": 1}

        def flaky_rmtree(path, *args, **kwargs):
            if state["fails"]:
                state["fails"] -= 1
                raise OSError(errno.ENOTEMPTY, "Directory not empty", str(path))
            return real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(shutil, "rmtree", flaky_rmtree)
        runner_home.build(str(tmp_path / "ws"), real_home=str(real))

        assert not stray.exists()
        assert not any(r.levelno >= logging.ERROR for r in caplog.records)
        assert ".config" in caplog.text  # still counted in the swept summary

    def test_a_dir_that_will_not_die_logs_one_warning_no_traceback(self, tmp_path, caplog, monkeypatch):
        """If the retry also fails, one WARNING naming the directory and
        nothing else — no ERROR, no traceback."""
        real = _fake_home(tmp_path)
        home = runner_home.build(str(tmp_path / "ws"), real_home=str(real))
        stray = pathlib.Path(home) / ".config"
        stray.mkdir(parents=True)
        (stray / "opencode").mkdir()

        def always_fail(path, *args, **kwargs):
            raise OSError(errno.ENOTEMPTY, "Directory not empty", str(path))

        monkeypatch.setattr(shutil, "rmtree", always_fail)
        runner_home.build(str(tmp_path / "ws"), real_home=str(real))

        assert stray.exists()
        warnings = [r for r in caplog.records
                    if r.levelno == logging.WARNING and "could not sweep" in r.getMessage()]
        assert len(warnings) == 1
        assert ".config" in warnings[0].getMessage()
        assert warnings[0].exc_info is None
        assert not any(r.levelno >= logging.ERROR for r in caplog.records)
