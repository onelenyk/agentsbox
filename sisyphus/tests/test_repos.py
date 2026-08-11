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
