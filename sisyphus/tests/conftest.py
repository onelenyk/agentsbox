import pytest
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

HERE = os.path.dirname(__file__)


def pytest_configure(config):
    tmp = os.environ.get("PYTEST_TMPDIR", os.path.join(HERE, ".pytest_tmp"))
    os.makedirs(tmp, exist_ok=True)
    os.environ["SISYPHUS_DB"] = os.path.join(tmp, "test.db")
    # Never the real login keychain from a test: core.credentials reads it on
    # every config load.
    os.environ["SISYPHUS_KEYCHAIN"] = "off"
    os.environ["WORKSPACE_ROOT"] = os.path.join(tmp, "workspace")
    os.makedirs(os.environ["WORKSPACE_ROOT"], exist_ok=True)


def pytest_collection_modifyitems(config, items):
    """Stamp every test with its tier marker from tests/tiers.py — one map
    instead of 58 files carrying their own pytestmark. An unclassified module
    raises, so a new test file must declare what kind of test it is."""
    from tests.tiers import tier_for
    for item in items:
        module = os.path.basename(str(item.fspath))[:-3]
        item.add_marker(getattr(pytest.mark, tier_for(module)))


@pytest.fixture(autouse=True)
def _utc_clock(monkeypatch):
    """Every test reads clocks in UTC unless it says otherwise, so a test of
    "what time does this say" means the same on any machine."""
    import time
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture(autouse=True)
def _config_never_the_real_config_yaml(monkeypatch):
    """Tests must never read the developer's own untracked config.yaml: the
    suite's result must not depend on whose machine it runs on. When
    core.config.config_file() would resolve to the real config.yaml, return the
    tracked config.example.yaml instead, and reset the cache so every test
    loads the pinned config.

    The shim only substitutes the real config.yaml — a test that needs a
    specific config keeps setting it itself (monkeypatching config_file or
    setting SISYPHUS_CONFIG), and those overrides win."""
    import core.config
    root = Path(core.config.__file__).parent.parent
    real = root / "config.yaml"
    example = root / "config.example.yaml"
    original = core.config.config_file

    def _pinned():
        resolved = original()
        return example if resolved == real else resolved

    monkeypatch.setattr(core.config, "config_file", _pinned)
    core.config._config = None
    yield


@pytest.fixture
def db_path(tmp_path):
    db = tmp_path / "test.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)

    os.environ["SISYPHUS_DB"] = str(db)
    os.environ["WORKSPACE_ROOT"] = str(workspace)

    import core.db
    core.db.DB_PATH = str(db)
    core.db.init_db()

    # Seed runtimes + built-in agents — the worker and agent-MCP resolve
    # personas/tools/runtimes from these tables, as the daemon does at startup.
    from core.runners import init_runners_table
    from core.agents import init_agents_table
    from core.usage import init_usage_table
    from core.mergerequests import init_mr_table
    from core.autopilot import init_autopilot_table
    from core.limits import init_limits_table
    from core.model_classes import init_classes_table
    from core.integrations import init_integrations_table
    from core.jira_intake import init_jira_sync_table
    from core.health import init_health_table
    from core.devices import init_devices_tables
    init_runners_table()
    init_agents_table()
    init_usage_table()
    init_mr_table()
    init_autopilot_table()
    init_limits_table()
    init_classes_table()
    init_integrations_table()
    init_jira_sync_table()
    init_health_table()
    init_devices_tables()

    yield str(db)
