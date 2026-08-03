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
    from core.copilot_store import init_copilot_tables
    from core.planner import init_planner_tables
    init_runners_table()
    init_agents_table()
    init_usage_table()
    init_mr_table()
    init_autopilot_table()
    init_copilot_tables()
    init_planner_tables()

    yield str(db)
