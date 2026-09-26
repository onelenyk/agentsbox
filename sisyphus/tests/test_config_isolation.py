"""The suite must never read the developer's own config.yaml.

core.config.config_file() returns the untracked sisyphus/config.yaml whenever
it exists, so on the operator's machine a config.yaml with
``repos: - name: sisyphus … auto_merge: true`` flipped the "done requires the
MR to be merged" rule for every test that uses the repo name "sisyphus" — 7
tests failed there while every worker worktree (no config.yaml) passed. The
autouse fixture in conftest.py pins the suite to config.example.yaml; these
tests guard that pin.

SISYPHUS_CONFIG is a production escape hatch (see core.config.config_file):
when set it names the config file to use. The fixture pins via config.example.yaml;
a test that wants a specific config sets SISYPHUS_CONFIG itself.
"""

from pathlib import Path

import core.config


def _auto_merge_on_sisyphus(config) -> bool:
    """True when the loaded config opts repo "sisyphus" into auto_merge."""
    for repo in config.get("repos", []):
        if repo.get("name") == "sisyphus" and repo.get("auto_merge"):
            return True
    return False


def _repo_config() -> Path:
    """The developer's real config.yaml path — the one the suite must never read."""
    return Path(core.config.__file__).parent.parent / "config.yaml"


def test_config_file_never_resolves_to_the_real_config_yaml():
    """The suite's config resolution never lands on the developer's config.yaml,
    and the loaded config has no auto_merge for repo "sisyphus"."""
    assert core.config.config_file() != _repo_config()
    config = core.config.load_config()
    assert not _auto_merge_on_sisyphus(config)


def test_operator_auto_merge_config_never_reaches_the_suite(tmp_path, monkeypatch):
    """Simulate the operator's machine: a config.yaml with auto_merge on repo
    "sisyphus" sitting where config_file() would normally look. The fixture
    keeps the suite on config.example.yaml, so the loaded config shows no
    auto_merge — even though the env override proves a config.yaml with it
    WOULD change behavior if it ever became the effective config."""
    operator = tmp_path / "config.yaml"
    operator.write_text("repos:\n  - name: sisyphus\n    auto_merge: true\n")

    # The fixture's pin: no env override, so the resolver stays on the example.
    assert core.config.config_file() == _repo_config().parent / "config.example.yaml"
    assert not _auto_merge_on_sisyphus(core.config.load_config())

    # The escape hatch works: SISYPHUS_CONFIG points at the operator-like config
    # and config_file() follows it — so a config.yaml with auto_merge on
    # "sisyphus" WOULD be read if it were ever the effective config.
    monkeypatch.setenv("SISYPHUS_CONFIG", str(operator))
    core.config._config = None
    assert core.config.config_file() == operator
    assert _auto_merge_on_sisyphus(core.config.load_config())

    # Back to the pinned default: no env override, no auto_merge on "sisyphus".
    monkeypatch.delenv("SISYPHUS_CONFIG")
    core.config._config = None
    assert core.config.config_file() != _repo_config()
    assert not _auto_merge_on_sisyphus(core.config.load_config())
