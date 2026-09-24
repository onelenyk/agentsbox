"""Unit tests for core.repos — get_repo, list_repos, load_repos, update_repo_config."""

from unittest.mock import patch

import core.repos


# ---------------------------------------------------------------------------
# load_repos
# ---------------------------------------------------------------------------
class TestLoadRepos:
    """load_repos() delegates to core.config.get_config("repos", [])."""

    def test_returns_list_when_config_has_repos(self):
        repos = [{"name": "test", "source": "git@host:org/repo.git", "branch": "main"}]
        with patch("core.config.get_config", return_value=repos):
            result = core.repos.load_repos()
        assert result == repos

    def test_returns_empty_list_when_config_has_no_repos(self):
        with patch("core.config.get_config", return_value=[]):
            result = core.repos.load_repos()
        assert result == []


# ---------------------------------------------------------------------------
# get_repo
# ---------------------------------------------------------------------------
class TestGetRepo:
    """get_repo(name) iterates load_repos() and returns matching dict."""

    REPOS = [
        {"name": "project-alpha", "source": "git@github.com:org/alpha.git", "branch": "main"},
        {"name": "project-beta", "source": "git@github.com:org/beta.git", "branch": "main"},
    ]

    def test_returns_repo_dict_when_name_matches(self):
        with patch("core.repos.load_repos", return_value=self.REPOS):
            result = core.repos.get_repo("project-alpha")
        assert result == self.REPOS[0]
        assert result["name"] == "project-alpha"
        assert result["source"] == "git@github.com:org/alpha.git"
        assert result["branch"] == "main"

    def test_returns_none_when_name_not_found(self):
        with patch("core.repos.load_repos", return_value=self.REPOS):
            result = core.repos.get_repo("nonexistent")
        assert result is None

    def test_returns_none_when_repo_list_empty(self):
        with patch("core.repos.load_repos", return_value=[]):
            result = core.repos.get_repo("anything")
        assert result is None


# ---------------------------------------------------------------------------
# list_repos
# ---------------------------------------------------------------------------
class TestListRepos:
    """list_repos() adds a "cloned" field based on filesystem checks."""

    REPOS = [
        {"name": "repo-a", "source": "git@host:org/a.git", "branch": "main"},
        {"name": "repo-b", "source": "git@host:org/b.git", "branch": "dev"},
    ]

    def test_adds_cloned_false_when_dirs_do_not_exist(self):
        """When bare repos don't exist on disk, cloned=False."""
        with patch("core.repos.load_repos", return_value=[r.copy() for r in self.REPOS]):
            result = core.repos.list_repos()

        assert len(result) == 2
        for r in result:
            assert "cloned" in r
            assert r["cloned"] is False
            assert "name" in r
            assert "source" in r
            assert "branch" in r
        assert result[0]["name"] == "repo-a"
        assert result[1]["name"] == "repo-b"

    def test_adds_cloned_true_when_bare_repos_exist(self):
        """When bare repo dirs and their HEAD files exist, cloned=True."""
        with patch("core.repos.load_repos", return_value=[r.copy() for r in self.REPOS]):
            with patch("pathlib.Path.exists", return_value=True):
                result = core.repos.list_repos()

        for r in result:
            assert r["cloned"] is True

    def test_reach_is_ok_for_a_real_local_checkout(self, tmp_path):
        """A repo whose source is a live git checkout is ready to work in —
        regardless of whether a persistent bare clone happens to exist, since
        workers clone per task and delete it again on cleanup."""
        src = tmp_path / "proj"
        (src / ".git").mkdir(parents=True)
        with patch("core.repos.load_repos",
                   return_value=[{"name": "p", "source": str(src), "branch": "main"}]):
            r = core.repos.list_repos()[0]
        assert r["reach"] == "ok"
        assert r["cloned"] is False   # and that must not be what the UI reports

    def test_reach_is_missing_when_the_source_is_gone(self, tmp_path):
        with patch("core.repos.load_repos",
                   return_value=[{"name": "p", "source": str(tmp_path / "nope"),
                                  "branch": "main"}]):
            assert core.repos.list_repos()[0]["reach"] == "missing"

    def test_reach_is_remote_for_a_url(self):
        for url in ("https://github.com/o/r.git", "git@github.com:o/r.git"):
            with patch("core.repos.load_repos",
                       return_value=[{"name": "p", "source": url, "branch": "main"}]):
                assert core.repos.list_repos()[0]["reach"] == "remote"

    def test_does_not_mutate_load_repos_result_in_place(self):
        """list_repos adds 'cloned' to the same list objects returned by load_repos."""
        original = [{"name": "x", "source": "s", "branch": "b"}]
        with patch("core.repos.load_repos", return_value=original):
            result = core.repos.list_repos()
        assert "cloned" in original[0]
        assert result is original  # same list object, mutated in place

    def test_includes_stored_platform(self):
        """A stored platform override flows through list_repos untouched."""
        repos = [{"name": "p", "source": "s", "branch": "main", "platform": "github"}]
        with patch("core.repos.load_repos", return_value=[r.copy() for r in repos]):
            result = core.repos.list_repos()
        assert result[0]["platform"] == "github"


# ---------------------------------------------------------------------------
# _save_repo_config
# ---------------------------------------------------------------------------
class TestSaveRepoConfig:
    """_save_repo_config persists the platform override when non-empty."""

    def _call(self, platform):
        with patch("core.repos.yaml.safe_load", return_value={"repos": []}):
            with patch("core.repos.yaml.dump", return_value="dumped-yaml") as mock_dump:
                with patch("core.config.reload_config"):
                    with patch.object(core.repos.Path, "read_text", return_value=""):
                        with patch.object(core.repos.Path, "write_text"):
                            core.repos._save_repo_config("r", "src", platform, "main")
        return mock_dump.call_args[0][0]["repos"][0]

    def test_round_trips_platform_when_set(self):
        entry = self._call("gitlab")
        assert entry == {"name": "r", "source": "src", "branch": "main", "platform": "gitlab"}

    def test_omits_platform_key_when_empty(self):
        entry = self._call("")
        assert "platform" not in entry
        assert entry == {"name": "r", "source": "src", "branch": "main"}

    def test_omits_platform_key_when_none(self):
        entry = self._call(None)
        assert "platform" not in entry


# ---------------------------------------------------------------------------
# add_repo
# ---------------------------------------------------------------------------
class TestAddRepo:
    """add_repo forwards the platform override to _save_repo_config."""

    def test_passes_platform_through_to_save(self):
        with patch("pathlib.Path.exists", return_value=True), \
             patch("core.repos._save_repo_config") as save:
            core.repos.add_repo("r", "src", "gitlab", "main")
        save.assert_called_once_with("r", "src", "gitlab", "main")


# ---------------------------------------------------------------------------
# update_repo_config
# ---------------------------------------------------------------------------
class TestUpdateRepoConfig:
    """update_repo_config modifies config.yaml in place for a given repo."""

    REPOS = [
        {"name": "repo1", "source": "old-source-1", "branch": "old-branch-1"},
        {"name": "repo2", "source": "old-source-2", "branch": "old-branch-2"},
    ]

    def test_updates_source_and_branch_for_matching_repo(self):
        with patch("core.repos.load_repos", return_value=[r.copy() for r in self.REPOS]):
            fake_config = {"repos": [r.copy() for r in self.REPOS], "other": "data"}
            with patch("core.repos.yaml.safe_load", return_value=fake_config):
                with patch("core.repos.yaml.dump", return_value="dumped-yaml") as mock_dump:
                    with patch("core.config.reload_config"):
                        with patch.object(core.repos.Path, "read_text", return_value=""):
                            with patch.object(core.repos.Path, "write_text") as mock_write:
                                core.repos.update_repo_config(
                                    "repo1", source="new-src", branch="new-br"
                                )

        mock_write.assert_called_once()
        dumped_config = mock_dump.call_args[0][0]
        updated_repos = dumped_config["repos"]
        assert updated_repos[0]["source"] == "new-src"
        assert updated_repos[0]["branch"] == "new-br"
        # repo2 untouched
        assert updated_repos[1]["source"] == "old-source-2"
        assert updated_repos[1]["branch"] == "old-branch-2"

    def test_leaves_non_matching_repo_untouched(self):
        with patch("core.repos.load_repos", return_value=[r.copy() for r in self.REPOS]):
            fake_config = {"repos": [r.copy() for r in self.REPOS]}
            with patch("core.repos.yaml.safe_load", return_value=fake_config):
                with patch("core.repos.yaml.dump", return_value="dumped-yaml") as mock_dump:
                    with patch("core.config.reload_config"):
                        with patch.object(core.repos.Path, "read_text", return_value=""):
                            with patch.object(core.repos.Path, "write_text"):
                                core.repos.update_repo_config(
                                    "nonexistent", source="new", branch="new"
                                )

        dumped_config = mock_dump.call_args[0][0]
        updated_repos = dumped_config["repos"]
        assert updated_repos[0]["source"] == "old-source-1"
        assert updated_repos[0]["branch"] == "old-branch-1"
        assert updated_repos[1]["source"] == "old-source-2"
        assert updated_repos[1]["branch"] == "old-branch-2"

    def test_partial_update_only_changes_provided_fields(self):
        with patch("core.repos.load_repos", return_value=[r.copy() for r in self.REPOS]):
            fake_config = {"repos": [r.copy() for r in self.REPOS]}
            with patch("core.repos.yaml.safe_load", return_value=fake_config):
                with patch("core.repos.yaml.dump", return_value="dumped-yaml") as mock_dump:
                    with patch("core.config.reload_config"):
                        with patch.object(core.repos.Path, "read_text", return_value=""):
                            with patch.object(core.repos.Path, "write_text"):
                                core.repos.update_repo_config(
                                    "repo1", branch="updated-branch"
                                )

        dumped_config = mock_dump.call_args[0][0]
        updated_repos = dumped_config["repos"]
        assert updated_repos[0]["source"] == "old-source-1"  # unchanged
        assert updated_repos[0]["branch"] == "updated-branch"

    def test_updates_only_first_matching_repo(self):
        dup_repos = [
            {"name": "dup", "source": "s1", "branch": "b1"},
            {"name": "dup", "source": "s2", "branch": "b2"},
        ]
        with patch("core.repos.load_repos", return_value=dup_repos):
            fake_config = {"repos": dup_repos}
            with patch("core.repos.yaml.safe_load", return_value=fake_config):
                with patch("core.repos.yaml.dump", return_value="dumped-yaml") as mock_dump:
                    with patch("core.config.reload_config"):
                        with patch.object(core.repos.Path, "read_text", return_value=""):
                            with patch.object(core.repos.Path, "write_text"):
                                core.repos.update_repo_config("dup", source="changed")

        dumped_config = mock_dump.call_args[0][0]
        updated_repos = dumped_config["repos"]
        assert updated_repos[0]["source"] == "changed"
        assert updated_repos[1]["source"] == "s2"  # untouched

    def test_writes_other_config_keys_unchanged(self):
        with patch("core.repos.load_repos", return_value=[r.copy() for r in self.REPOS]):
            fake_config = {"repos": [r.copy() for r in self.REPOS], "other": "preserved"}
            with patch("core.repos.yaml.safe_load", return_value=fake_config):
                with patch("core.repos.yaml.dump", return_value="dumped-yaml") as mock_dump:
                    with patch("core.config.reload_config"):
                        with patch.object(core.repos.Path, "read_text", return_value=""):
                            with patch.object(core.repos.Path, "write_text"):
                                core.repos.update_repo_config("repo1", source="new-src")

        dumped_config = mock_dump.call_args[0][0]
        assert dumped_config["other"] == "preserved"

    def test_preserves_existing_platform_when_other_fields_change(self):
        repos = [{"name": "repo1", "source": "old-src", "branch": "old-br", "platform": "gitlab"}]
        with patch("core.repos.load_repos", return_value=[r.copy() for r in repos]):
            fake_config = {"repos": [r.copy() for r in repos]}
            with patch("core.repos.yaml.safe_load", return_value=fake_config):
                with patch("core.repos.yaml.dump", return_value="dumped-yaml") as mock_dump:
                    with patch("core.config.reload_config"):
                        with patch.object(core.repos.Path, "read_text", return_value=""):
                            with patch.object(core.repos.Path, "write_text"):
                                core.repos.update_repo_config("repo1", source="new-src")

        updated = mock_dump.call_args[0][0]["repos"][0]
        assert updated["platform"] == "gitlab"
        assert updated["source"] == "new-src"

    def test_can_change_platform(self):
        repos = [{"name": "repo1", "source": "old-src", "branch": "old-br", "platform": "gitlab"}]
        with patch("core.repos.load_repos", return_value=[r.copy() for r in repos]):
            fake_config = {"repos": [r.copy() for r in repos]}
            with patch("core.repos.yaml.safe_load", return_value=fake_config):
                with patch("core.repos.yaml.dump", return_value="dumped-yaml") as mock_dump:
                    with patch("core.config.reload_config"):
                        with patch.object(core.repos.Path, "read_text", return_value=""):
                            with patch.object(core.repos.Path, "write_text"):
                                core.repos.update_repo_config("repo1", platform="github")

        updated = mock_dump.call_args[0][0]["repos"][0]
        assert updated["platform"] == "github"

    def test_clears_platform_on_empty_string(self):
        repos = [{"name": "repo1", "source": "old-src", "branch": "old-br", "platform": "gitlab"}]
        with patch("core.repos.load_repos", return_value=[r.copy() for r in repos]):
            fake_config = {"repos": [r.copy() for r in repos]}
            with patch("core.repos.yaml.safe_load", return_value=fake_config):
                with patch("core.repos.yaml.dump", return_value="dumped-yaml") as mock_dump:
                    with patch("core.config.reload_config"):
                        with patch.object(core.repos.Path, "read_text", return_value=""):
                            with patch.object(core.repos.Path, "write_text"):
                                core.repos.update_repo_config("repo1", platform="")

        updated = mock_dump.call_args[0][0]["repos"][0]
        assert "platform" not in updated

    def test_toggling_auto_merge_keeps_every_other_key(self):
        """_save_repo_config rebuilds the entry and would drop `check`, so the
        toggle must route through update_repo_config's in-place edit — and the
        save must keep check/setup/platform untouched while flipping the flag."""
        repos = [{"name": "repo1", "source": "old-src", "branch": "old-br",
                  "platform": "gitlab", "check": "pytest -q",
                  "setup": {"copy": ["local.properties"]}}]
        with patch("core.repos.load_repos", return_value=[r.copy() for r in repos]):
            fake_config = {"repos": [r.copy() for r in repos]}
            with patch("core.repos.yaml.safe_load", return_value=fake_config):
                with patch("core.repos.yaml.dump", return_value="dumped-yaml") as mock_dump:
                    with patch("core.config.reload_config"):
                        with patch.object(core.repos.Path, "read_text", return_value=""):
                            with patch.object(core.repos.Path, "write_text"):
                                core.repos.update_repo_config("repo1", auto_merge=True)

        updated = mock_dump.call_args[0][0]["repos"][0]
        assert updated["auto_merge"] is True
        assert updated["check"] == "pytest -q"
        assert updated["setup"] == {"copy": ["local.properties"]}
        assert updated["platform"] == "gitlab"
        assert updated["source"] == "old-src"
        assert updated["branch"] == "old-br"

    def test_unchecking_auto_merge_sets_it_false(self):
        repos = [{"name": "repo1", "source": "old-src", "branch": "old-br",
                  "check": "pytest -q", "auto_merge": True}]
        with patch("core.repos.load_repos", return_value=[r.copy() for r in repos]):
            fake_config = {"repos": [r.copy() for r in repos]}
            with patch("core.repos.yaml.safe_load", return_value=fake_config):
                with patch("core.repos.yaml.dump", return_value="dumped-yaml") as mock_dump:
                    with patch("core.config.reload_config"):
                        with patch.object(core.repos.Path, "read_text", return_value=""):
                            with patch.object(core.repos.Path, "write_text"):
                                core.repos.update_repo_config("repo1", auto_merge=False)

        updated = mock_dump.call_args[0][0]["repos"][0]
        assert updated["auto_merge"] is False
        assert updated["check"] == "pytest -q"

    def test_auto_merge_absent_by_default(self):
        """A repo entry without the key reads as off — auto_merge is an opt-in."""
        with patch("core.repos.load_repos",
                   return_value=[{"name": "p", "source": "s", "branch": "main"}]):
            assert core.repos.get_repo("p").get("auto_merge") in (None, False)


class TestRepoCredential:
    """A single global token per forge holds only until there are two
    organisations, or a work account and a personal one. The repository that
    needs a credential should carry it; the global block stays as a fallback."""

    def _cfg(self, monkeypatch, repos, forge=None):
        from core import repos as repos_mod
        monkeypatch.setattr(repos_mod, "get_repo", lambda n: next((r for r in repos if r["name"] == n), None))
        conf = {"github": forge or {}, "gitlab": forge or {}}
        monkeypatch.setattr("core.config.get_config",
                            lambda key=None, default=None: conf.get(key, default))

    def test_repo_token_wins_over_the_global_one(self, monkeypatch):
        from core.repos import repo_credential
        self._cfg(monkeypatch,
                  [{"name": "a", "source": "git@github.com:o/a.git", "token": "repo-tok"}],
                  forge={"token": "global-tok"})
        c = repo_credential("a")
        assert c["token"] == "repo-tok" and c["source"] == "repo"

    def test_falls_back_to_the_global_token(self, monkeypatch):
        from core.repos import repo_credential
        self._cfg(monkeypatch, [{"name": "a", "source": "git@github.com:o/a.git"}],
                  forge={"token": "global-tok"})
        c = repo_credential("a")
        assert c["token"] == "global-tok" and c["source"] == "global"

    def test_unresolved_placeholder_is_not_a_credential(self, monkeypatch):
        """core.config leaves ${VAR} literal when the variable is unset — that
        string must never be handed to a forge as a token."""
        from core.repos import repo_credential
        self._cfg(monkeypatch,
                  [{"name": "a", "source": "git@gitlab.com:o/a.git", "token": "${NOPE}"}],
                  forge={"token": "${ALSO_NOPE}"})
        c = repo_credential("a")
        assert c["token"] is None and c["source"] is None

    def test_missing_credential_names_the_variable_to_set(self, monkeypatch):
        from core.repos import repo_credential
        self._cfg(monkeypatch, [{"name": "a", "source": "git@gitlab.com:o/a.git"}])
        assert repo_credential("a")["env_hint"] == "GITLAB_TOKEN"

    def test_local_repo_needs_no_credential(self, monkeypatch):
        from core.repos import repo_credential
        self._cfg(monkeypatch, [{"name": "a", "source": "/on/disk"}])
        c = repo_credential("a")
        assert c["platform"] == "local" and c["token"] is None and c["env_hint"] == ""

    def test_explicit_platform_beats_detection(self, monkeypatch):
        from core.repos import repo_credential
        self._cfg(monkeypatch,
                  [{"name": "a", "source": "/on/disk", "platform": "gitlab"}],
                  forge={"token": "g"})
        assert repo_credential("a")["platform"] == "gitlab"


class TestForgePublishIsRecordedLocally:
    """A GitHub/GitLab change used to exist on the forge and nowhere in
    Sisyphus — list_mrs() never returned it, so core.forge_poll had nothing to
    follow. Only the local publisher kept a row."""

    def test_a_successful_forge_publish_creates_an_mr_row(self, monkeypatch, tmp_path, db_path):
        from core import mergerequests, providers
        mergerequests.init_mr_table()
        providers._record_forge_mr(
            task_id="task-x", repo_name="acme", branch="sisyphus/task-x",
            base_branch="main", title="Add thing", bare="/bare", source="/src",
            url="https://github.com/o/r/pull/7")
        mr = mergerequests.latest_for_task("task-x")
        assert mr and mr["source_branch"] == "sisyphus/task-x"
        assert mr["forge_url"] == "https://github.com/o/r/pull/7"

    def test_republishing_updates_the_url_rather_than_duplicating(self, db_path):
        from core import mergerequests, providers
        mergerequests.init_mr_table()
        for url in ("https://github.com/o/r/pull/7", "https://github.com/o/r/pull/7"):
            providers._record_forge_mr("task-y", "acme", "sisyphus/task-y", "main",
                                       "T", "/bare", "/src", url)
        assert len(mergerequests.list_mrs()) == 1

    def test_bookkeeping_failure_never_fails_a_published_change(self, monkeypatch, db_path):
        """The change is already on the forge; losing our own row must not turn
        a successful publish into an error."""
        from core import providers
        monkeypatch.setattr("core.mergerequests.find_open_mr",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")))
        providers._record_forge_mr("task-z", "acme", "b", "main", "T", "/bare", "/src", "u")

    def test_no_task_id_records_nothing(self, db_path):
        from core import mergerequests, providers
        mergerequests.init_mr_table()
        providers._record_forge_mr("", "acme", "b", "main", "T", "/bare", "/src", "u")
        assert mergerequests.list_mrs() == []
