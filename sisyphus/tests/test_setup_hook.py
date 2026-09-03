"""Tests for core/setup_hook.py — giving a fresh worktree the files git does not carry.

Two things carry the weight here, and they are the two ways this feature could
be worse than not having it:

  * a repo with no hook must be indistinguishable from today (ran=False, no
    files touched, no reassuring thread line about work that never happened);
  * a hook that is configured and does NOT fully complete must stop the worker,
    not hand an agent a worktree missing exactly the file it was told to expect.

The path-confinement tests are not modelling a hostile agent — these paths come
from the human's config.yaml — they pin that a typo cannot write outside the
worktree.
"""
import stat
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from core import repos as repos_mod
from core import setup_hook
from core.setup_hook import SetupHookError, apply_setup, summarise


@pytest.fixture
def source(tmp_path):
    """A stand-in for the human's real checkout: tracked file + gitignored ones."""
    src = tmp_path / "checkout"
    (src / "app").mkdir(parents=True)
    (src / "local.properties").write_text("sdk.dir=/opt/android\n")
    (src / "app" / "google-services.json").write_text('{"key": "x"}\n')
    secret = src / "credentials.env"
    secret.write_text("TOKEN=hunter2\n")
    secret.chmod(0o600)
    return src


@pytest.fixture
def worktree(tmp_path):
    wt = tmp_path / "worktree"
    wt.mkdir()
    (wt / "build.gradle").write_text("// tracked\n")
    return wt


@pytest.fixture
def configure(monkeypatch, source):
    """Install a single repo named 'r' with the given `setup:` block."""
    def _configure(setup=None, name="r", src=None):
        entry = {"name": name, "source": str(source if src is None else src), "branch": "main"}
        if setup is not None:
            entry["setup"] = setup
        monkeypatch.setattr(repos_mod, "load_repos", lambda: [entry])
        return entry
    return _configure


class TestNoHookIsNotASuccess:
    def test_repo_without_setup_block_runs_nothing(self, configure, worktree):
        configure(setup=None)
        before = sorted(p.name for p in worktree.iterdir())
        res = apply_setup("r", str(worktree))
        assert res["ran"] is False
        assert res["copied"] == [] and res["command"] == ""
        assert "no setup hook" in res["reason"]
        assert sorted(p.name for p in worktree.iterdir()) == before

    def test_summary_never_reads_as_a_success(self, configure, worktree):
        configure(setup=None)
        line = summarise(apply_setup("r", str(worktree)))
        assert "No repo setup hook ran" in line
        assert "copied" not in line

    def test_unknown_repo_is_a_no_hook_not_a_crash(self, configure, worktree):
        configure(setup={"copy": ["local.properties"]})
        # spawn_worker already refuses an unresolvable repo before it gets here;
        # this must not be the thing that explodes.
        assert apply_setup("nope", str(worktree))["ran"] is False
        assert apply_setup(None, str(worktree))["ran"] is False


class TestCopy:
    def test_relative_source_is_read_from_the_repo_source_dir(self, configure, worktree):
        configure(setup={"copy": ["local.properties"]})
        res = apply_setup("r", str(worktree))
        assert res["ran"] is True
        assert (worktree / "local.properties").read_text() == "sdk.dir=/opt/android\n"
        assert res["copied"] == ["local.properties"]

    def test_nested_destination_directories_are_created(self, configure, worktree, tmp_path):
        outside = tmp_path / "vault" / "app.env"
        outside.parent.mkdir()
        outside.write_text("A=1\n")
        configure(setup={"copy": [{"from": str(outside), "to": "deep/nest/.env"}]})
        apply_setup("r", str(worktree))
        assert (worktree / "deep" / "nest" / ".env").read_text() == "A=1\n"

    def test_directory_source_is_copied_whole(self, configure, worktree, source):
        configure(setup={"copy": [{"from": "app", "to": "app"}]})
        apply_setup("r", str(worktree))
        assert (worktree / "app" / "google-services.json").exists()

    def test_credential_mode_is_preserved(self, configure, worktree):
        configure(setup={"copy": [{"from": "credentials.env", "to": ".env"}]})
        apply_setup("r", str(worktree))
        mode = stat.S_IMODE((worktree / ".env").stat().st_mode)
        # copy2, not a widening rewrite: a 600 secret must not land world-readable.
        assert mode & (stat.S_IRGRP | stat.S_IROTH) == 0

    def test_existing_tracked_files_are_untouched(self, configure, worktree):
        configure(setup={"copy": ["local.properties"]})
        apply_setup("r", str(worktree))
        assert (worktree / "build.gradle").read_text() == "// tracked\n"


class TestMalformedConfigIsLoud:
    def test_setup_as_a_string_is_an_error_not_a_no_op(self, configure, worktree):
        configure(setup="local.properties")
        with pytest.raises(SetupHookError, match="must be a mapping"):
            apply_setup("r", str(worktree))

    def test_empty_setup_block_is_an_error(self, configure, worktree):
        configure(setup={})
        with pytest.raises(SetupHookError, match="present but empty"):
            apply_setup("r", str(worktree))

    def test_unknown_top_level_key_is_an_error(self, configure, worktree):
        configure(setup={"copie": ["local.properties"]})
        with pytest.raises(SetupHookError, match="unknown key"):
            apply_setup("r", str(worktree))

    def test_unknown_entry_key_is_an_error(self, configure, worktree):
        configure(setup={"copy": [{"src": "local.properties", "to": "x"}]})
        with pytest.raises(SetupHookError, match="unknown key"):
            apply_setup("r", str(worktree))

    def test_copy_must_be_a_list(self, configure, worktree):
        configure(setup={"copy": "local.properties"})
        with pytest.raises(SetupHookError, match="must be a list"):
            apply_setup("r", str(worktree))

    def test_missing_source_stops_the_worker(self, configure, worktree):
        configure(setup={"copy": ["never-created.properties"]})
        with pytest.raises(SetupHookError, match="does not exist"):
            apply_setup("r", str(worktree))
        assert not (worktree / "never-created.properties").exists()

    def test_absolute_source_without_a_destination_is_refused(self, configure, worktree, tmp_path):
        secret = tmp_path / "vault.env"
        secret.write_text("A=1\n")
        configure(setup={"copy": [str(secret)]})
        with pytest.raises(SetupHookError, match="needs an explicit destination"):
            apply_setup("r", str(worktree))


class TestPathsStayWhereConfigured:
    def test_destination_cannot_escape_the_worktree(self, configure, worktree, tmp_path):
        configure(setup={"copy": [{"from": "local.properties", "to": "../escaped.properties"}]})
        with pytest.raises(SetupHookError, match="outside the worktree"):
            apply_setup("r", str(worktree))
        assert not (tmp_path / "escaped.properties").exists()

    def test_absolute_destination_is_refused(self, configure, worktree, tmp_path):
        target = tmp_path / "absolute.properties"
        configure(setup={"copy": [{"from": "local.properties", "to": str(target)}]})
        with pytest.raises(SetupHookError, match="must be relative"):
            apply_setup("r", str(worktree))
        assert not target.exists()

    def test_relative_source_cannot_escape_the_repo_source_dir(self, configure, worktree, tmp_path):
        (tmp_path / "elsewhere.txt").write_text("nope\n")
        configure(setup={"copy": [{"from": "../elsewhere.txt", "to": "x.txt"}]})
        with pytest.raises(SetupHookError, match="outside the repo source dir"):
            apply_setup("r", str(worktree))
        assert not (worktree / "x.txt").exists()

    def test_symlinked_relative_source_pointing_out_of_tree_is_refused(
            self, configure, worktree, source, tmp_path):
        (tmp_path / "outside.env").write_text("A=1\n")
        (source / "linked.env").symlink_to(tmp_path / "outside.env")
        configure(setup={"copy": [{"from": "linked.env", "to": ".env"}]})
        with pytest.raises(SetupHookError, match="outside the repo source dir"):
            apply_setup("r", str(worktree))

    def test_nothing_is_copied_when_a_later_entry_is_bad(self, configure, worktree):
        # The whole plan resolves before the first write, so a bad third entry
        # cannot leave a half-prepared worktree behind.
        configure(setup={"copy": ["local.properties",
                                  {"from": "credentials.env", "to": ".env"},
                                  {"from": "local.properties", "to": "../escaped"}]})
        with pytest.raises(SetupHookError):
            apply_setup("r", str(worktree))
        assert not (worktree / "local.properties").exists()
        assert not (worktree / ".env").exists()


class TestRunCommand:
    def test_command_runs_in_the_worktree(self, configure, worktree):
        configure(setup={"run": "printf 'made-by-hook' > generated.txt"})
        res = apply_setup("r", str(worktree))
        assert res["ran"] is True and res["exit_code"] == 0
        assert (worktree / "generated.txt").read_text() == "made-by-hook"

    def test_command_runs_after_the_copies(self, configure, worktree):
        configure(setup={"copy": ["local.properties"],
                         "run": "test -f local.properties && printf ok > order.txt"})
        apply_setup("r", str(worktree))
        assert (worktree / "order.txt").read_text() == "ok"

    def test_failing_command_raises_with_its_output(self, configure, worktree):
        configure(setup={"run": "echo 'boom: no signing key' >&2; exit 3"})
        with pytest.raises(SetupHookError, match="exit 3") as e:
            apply_setup("r", str(worktree))
        assert "boom: no signing key" in str(e.value)

    def test_hanging_command_is_killed_and_raises(self, configure, worktree):
        configure(setup={"run": "sleep 30"})
        with pytest.raises(SetupHookError, match="exceeded 1s"):
            apply_setup("r", str(worktree), timeout=1)

    def test_summary_names_what_happened(self, configure, worktree):
        configure(setup={"copy": ["local.properties"], "run": "true"})
        line = summarise(apply_setup("r", str(worktree)))
        assert "local.properties" in line and "`true`" in line and "exit 0" in line


def _make_git_worktree(tmp_path, gitignore="local.properties\n", tag="a"):
    """A real linked worktree, as a worker gets: bare clone + `git worktree add`.

    Needed because the thing under test (info/exclude) only exists in a real
    repo, and because `git add -A` is what would leak a copied secret.
    """
    src = tmp_path / f"gitsrc-{tag}"
    src.mkdir()
    run = lambda *a: subprocess.run(list(a), capture_output=True, text=True, check=False)
    run("git", "init", "-q", "-b", "main", str(src))
    run("git", "-C", str(src), "config", "user.email", "t@t")
    run("git", "-C", str(src), "config", "user.name", "t")
    (src / "build.gradle").write_text("// tracked\n")
    (src / ".gitignore").write_text(gitignore)
    run("git", "-C", str(src), "add", "-A")
    run("git", "-C", str(src), "commit", "-qm", "init")
    bare = tmp_path / f"bare-{tag}"
    run("git", "clone", "-q", "--bare", str(src), str(bare))
    wt = tmp_path / f"gitwt-{tag}"
    run("git", "-C", str(bare), "worktree", "add", "-q", "--detach", str(wt), "main")
    return wt


@pytest.fixture
def git_worktree(tmp_path):
    return _make_git_worktree(tmp_path)


def _exclude_file(wt):
    common = subprocess.run(["git", "-C", str(wt), "rev-parse", "--git-common-dir"],
                            capture_output=True, text=True).stdout.strip()
    return Path(common) / "info" / "exclude"


def _staged_paths(wt):
    subprocess.run(["git", "-C", str(wt), "add", "-A", "--", "."], capture_output=True)
    out = subprocess.run(["git", "-C", str(wt), "diff", "--cached", "--name-only"],
                         capture_output=True, text=True)
    return out.stdout.split()


class TestCopiedFilesDoNotEnterCommits:
    def test_copied_secret_is_not_staged_by_add_all(self, configure, git_worktree):
        # core.git_ops.commit_all stages with `git add -A -- .` — without the
        # info/exclude entry this credentials file would ride into the MR.
        configure(setup={"copy": [{"from": "credentials.env", "to": "app/.env"}]})
        res = apply_setup("r", str(git_worktree))
        assert res["warning"] == ""
        assert (git_worktree / "app" / ".env").exists()
        assert "app/.env" not in _staged_paths(git_worktree)

    def test_a_copied_directory_does_not_swallow_the_agents_new_files(
            self, configure, git_worktree):
        # A directory copy must NOT be excluded: an anchored ignore on `app/`
        # would also hide files the agent creates inside it, and dropping the
        # agent's own work from its commit is worse than the leak this guards.
        configure(setup={"copy": [{"from": "app", "to": "app"}]})
        apply_setup("r", str(git_worktree))
        (git_worktree / "app" / "Feature.kt").write_text("// written by the agent\n")
        assert "app/Feature.kt" in _staged_paths(git_worktree)

    def test_already_ignored_paths_are_not_re_excluded(self, configure, git_worktree):
        # local.properties is in the repo's own tracked .gitignore — the common
        # case; the hook has nothing to add.
        configure(setup={"copy": ["local.properties"]})
        apply_setup("r", str(git_worktree))
        exclude = _exclude_file(git_worktree)
        assert "local.properties" not in (exclude.read_text() if exclude.exists() else "")
        assert "local.properties" not in _staged_paths(git_worktree)

    def test_repeated_workers_do_not_duplicate_exclude_lines(self, configure, tmp_path):
        # Every worker on the repo runs the hook against the same bare clone, so
        # the exclude file must not grow per run. A negated pattern in the repo's
        # own .gitignore is the case that keeps check-ignore answering "not
        # ignored" forever, so the write path is reached every single time.
        wt = _make_git_worktree(tmp_path, gitignore="*.env\n!app/.env\n", tag="neg")
        configure(setup={"copy": [{"from": "credentials.env", "to": "app/.env"}]})
        for _ in range(3):
            apply_setup("r", str(wt))
        assert _exclude_file(wt).read_text().count("/app/.env") == 1

    def test_unwritable_exclude_file_warns_and_says_so_on_the_thread(
            self, configure, git_worktree):
        # Losing the safety net is not the same as losing a requested file: the
        # copies are in place, so this warns loudly instead of failing the task.
        exclude = _exclude_file(git_worktree)
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("")
        exclude.chmod(0o400)
        try:
            configure(setup={"copy": [{"from": "credentials.env", "to": "app/.env"}]})
            res = apply_setup("r", str(git_worktree))
        finally:
            exclude.chmod(0o644)
        assert res["ran"] is True
        assert (git_worktree / "app" / ".env").exists()   # the file was still delivered
        assert "check them before merging" in res["warning"]
        assert "WARNING" in summarise(res)

    def test_non_git_worktree_is_left_alone(self, configure, worktree):
        # The repo-less scratch dir has no .git; there is nothing to exclude and
        # nothing to warn about.
        configure(setup={"copy": [{"from": "credentials.env", "to": ".env"}]})
        assert apply_setup("r", str(worktree))["warning"] == ""


class TestReposAccessor:
    def test_repo_setup_returns_the_raw_block(self, configure):
        configure(setup={"copy": ["local.properties"]})
        assert repos_mod.repo_setup("r") == {"copy": ["local.properties"]}

    def test_repo_setup_is_none_without_a_block_and_for_unknown_repos(self, configure):
        configure(setup=None)
        assert repos_mod.repo_setup("r") is None
        assert repos_mod.repo_setup("ghost") is None

    def test_malformed_block_is_handed_through_unvalidated(self, configure):
        # repos.py must NOT normalise this to None: "typo" reaching a caller as
        # "no hook configured" is the silent failure this feature exists to kill.
        configure(setup="oops")
        assert repos_mod.repo_setup("r") == "oops"


class TestWorkerIntegration:
    """spawn_worker's wiring: the hook runs for every worker on the repo, after
    the worktree exists and after set_access, and a failure stops the launch."""

    def _patches(self, worktree, monkeypatch, source, setup=None):
        entry = {"name": "myrepo", "source": str(source), "branch": "main"}
        if setup is not None:
            entry["setup"] = setup
        monkeypatch.setattr(repos_mod, "load_repos", lambda: [entry])
        return entry

    @pytest.mark.asyncio
    async def test_hook_files_are_present_before_the_agent_starts(
            self, db_path, tmp_path, monkeypatch, source, worktree):
        from core import store
        from core.launcher import AgentResult
        from core.worker import spawn_worker

        self._patches(worktree, monkeypatch, source, setup={"copy": ["local.properties"]})
        task = store.create_task("build it", repo="myrepo")
        seen = {}

        async def _launch(*args, **kwargs):
            seen["exists"] = (Path(args[2]) / "local.properties").exists()
            return AgentResult(status="completed", summary="ok")

        with patch("core.worker.get_repo", repos_mod.get_repo), \
             patch("core.worker.init_bare_clone"), \
             patch("core.git_ops.ensure_task_branch", return_value="sisyphus/x"), \
             patch("core.worker.create_worktree", return_value=str(worktree)), \
             patch("core.worker.set_access"), \
             patch("core.worker.launch_agent", _launch):
            await spawn_worker(task["id"], "engineer", "go", repo_name="myrepo")

        assert seen["exists"] is True
        texts = [m["text"] for m in store.get_thread(task["id"])]
        assert any("Repo setup hook" in t and "local.properties" in t for t in texts)

    @pytest.mark.asyncio
    async def test_hook_runs_after_set_access_so_modes_are_not_widened(
            self, db_path, tmp_path, monkeypatch, source, worktree):
        from core import store
        from core.launcher import AgentResult
        from core.worker import spawn_worker

        self._patches(worktree, monkeypatch, source,
                      setup={"copy": [{"from": "credentials.env", "to": ".env"}]})
        task = store.create_task("build it", repo="myrepo")
        order = {}

        def _set_access(path, level):
            order["secret_present_at_chmod"] = (Path(path) / ".env").exists()

        with patch("core.worker.get_repo", repos_mod.get_repo), \
             patch("core.worker.init_bare_clone"), \
             patch("core.git_ops.ensure_task_branch", return_value="sisyphus/x"), \
             patch("core.worker.create_worktree", return_value=str(worktree)), \
             patch("core.worker.set_access", _set_access), \
             patch("core.worker.launch_agent",
                   AsyncMock(return_value=AgentResult(status="completed", summary="ok"))):
            await spawn_worker(task["id"], "engineer", "go", repo_name="myrepo")

        # set_access rewrites every mode in the tree; the secret must arrive after it.
        assert order["secret_present_at_chmod"] is False
        assert (worktree / ".env").exists()

    @pytest.mark.asyncio
    async def test_failing_hook_stops_the_launch(
            self, db_path, tmp_path, monkeypatch, source, worktree):
        from core import store
        from core.launcher import AgentResult
        from core.worker import spawn_worker

        self._patches(worktree, monkeypatch, source, setup={"copy": ["absent.properties"]})
        task = store.create_task("build it", repo="myrepo")
        mock_launch = AsyncMock(return_value=AgentResult(status="completed", summary="ok"))

        with patch("core.worker.get_repo", repos_mod.get_repo), \
             patch("core.worker.init_bare_clone"), \
             patch("core.git_ops.ensure_task_branch", return_value="sisyphus/x"), \
             patch("core.worker.create_worktree", return_value=str(worktree)), \
             patch("core.worker.set_access"), \
             patch("core.worker.launch_agent", mock_launch):
            with pytest.raises(SetupHookError, match="absent.properties"):
                await spawn_worker(task["id"], "engineer", "go", repo_name="myrepo")

        mock_launch.assert_not_called()          # no agent on a half-prepared worktree
        kinds = [m["kind"] for m in store.get_thread(task["id"])]
        assert "worker_started" not in kinds     # and no session was minted

    @pytest.mark.asyncio
    async def test_repo_without_a_hook_behaves_exactly_as_before(
            self, db_path, tmp_path, monkeypatch, source, worktree):
        from core import store
        from core.launcher import AgentResult
        from core.worker import spawn_worker

        self._patches(worktree, monkeypatch, source, setup=None)
        task = store.create_task("build it", repo="myrepo")
        mock_launch = AsyncMock(return_value=AgentResult(status="completed", summary="ok"))

        with patch("core.worker.get_repo", repos_mod.get_repo), \
             patch("core.worker.init_bare_clone"), \
             patch("core.git_ops.ensure_task_branch", return_value="sisyphus/x"), \
             patch("core.worker.create_worktree", return_value=str(worktree)), \
             patch("core.worker.set_access"), \
             patch("core.worker.launch_agent", mock_launch):
            await spawn_worker(task["id"], "engineer", "go", repo_name="myrepo")

        mock_launch.assert_called_once()
        texts = [m["text"] for m in store.get_thread(task["id"])]
        assert not any("setup hook" in t for t in texts)   # silence, not a reassuring line
        assert sorted(p.name for p in worktree.iterdir() if p.name != "opencode.json") \
            == ["build.gradle"]

    @pytest.mark.asyncio
    async def test_repo_less_task_never_consults_a_hook(self, db_path, tmp_path, monkeypatch):
        from core import store
        from core.launcher import AgentResult
        from core.worker import spawn_worker

        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        called = []
        monkeypatch.setattr(setup_hook, "apply_setup",
                            lambda *a, **k: called.append(a) or {"ran": False})
        task = store.create_task("just think")
        with patch("core.worker.launch_agent",
                   AsyncMock(return_value=AgentResult(status="completed", summary="ok"))):
            await spawn_worker(task["id"], "explorer", "think")
        assert called == []
