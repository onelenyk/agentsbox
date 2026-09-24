import pytest
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

HERE = os.path.dirname(__file__)


def pytest_configure(config):
    tmp = os.environ.get("PYTEST_TMPDIR", os.path.join(HERE, ".pytest_tmp"))
    os.makedirs(tmp, exist_ok=True)
    os.environ["SISYPHUS_DB"] = os.path.join(tmp, "test.db")
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
    init_runners_table()
    init_agents_table()
    init_usage_table()
    init_mr_table()
    init_autopilot_table()
    init_limits_table()
    init_classes_table()
    init_integrations_table()
    init_jira_sync_table()

    yield str(db)
