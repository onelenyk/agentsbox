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

    # Seed the built-in agents (explorer/engineer/reviewer) — the worker and
    # agent-MCP now resolve personas/tools from the agents table, as at startup.
    from core.agents import init_agents_table
    init_agents_table()

    yield str(db)
