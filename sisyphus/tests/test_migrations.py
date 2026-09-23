import sqlite3
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "schema_v1.sql"


@pytest.fixture
def old_db(tmp_path, monkeypatch):
    """A database as a 2026-07-era daemon would have left it: three tables,
    none of the later columns, real rows, no version stamp."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(FIXTURE.read_text())
    conn.execute("INSERT INTO tasks (id, goal, repo, status, created_at, updated_at) "
                 "VALUES ('task-old1', 'a pre-migration goal', '', 'done', 't', 't')")
    conn.execute("INSERT INTO messages (task_id, seq, author, kind, text, created_at) "
                 "VALUES ('task-old1', 1, 'human', 'note', 'hello', 't')")
    conn.commit()
    conn.close()
    monkeypatch.setenv("SISYPHUS_DB", str(db))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    import core.db
    monkeypatch.setattr(core.db, "DB_PATH", str(db))
    return str(db)


def test_an_old_database_upgrades_in_place(old_db):
    """An old database gets all missing tables created and columns added."""
    from core import migrations

    assert migrations.db_version() == 0
    migrations.migrate_all()

    conn = sqlite3.connect(old_db)
    conn.row_factory = sqlite3.Row

    # Check tasks table has migrated columns
    tasks_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    assert "parent_id" in tasks_cols
    assert "agent" in tasks_cols
    assert "base_branch" in tasks_cols
    assert "title" in tasks_cols

    # Check merge_requests table has all migrated columns
    mr_cols = {r["name"] for r in conn.execute("PRAGMA table_info(merge_requests)").fetchall()}
    assert "review_status" in mr_cols
    assert "check_status" in mr_cols
    assert "check_output" in mr_cols
    assert "check_command" in mr_cols
    assert "check_ran_at" in mr_cols
    assert "check_head" in mr_cols
    assert "forge_url" in mr_cols
    assert "forge_state" in mr_cols
    assert "forge_checked_at" in mr_cols
    assert "merge_resolved" in mr_cols

    # Check all 12 tables exist
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()}
    required_tables = {
        "tasks", "messages", "runners", "agents", "usage", "merge_requests",
        "autopilots",
    }
    assert required_tables.issubset(tables), f"Missing tables: {required_tables - tables}"

    # Check version is stamped
    assert migrations.db_version() == migrations.CURRENT_VERSION

    conn.close()


def test_data_survives_the_upgrade(old_db):
    """Old data is preserved through the upgrade."""
    from core import migrations

    migrations.migrate_all()

    conn = sqlite3.connect(old_db)
    conn.row_factory = sqlite3.Row

    # Check the task still exists
    task_row = conn.execute("SELECT * FROM tasks WHERE id = 'task-old1'").fetchone()
    assert task_row is not None
    task = dict(task_row)
    assert task["goal"] == "a pre-migration goal"

    # Check the message is still there
    msg_row = conn.execute(
        "SELECT * FROM messages WHERE task_id = 'task-old1' AND seq = 1"
    ).fetchone()
    assert msg_row is not None
    msg = dict(msg_row)
    assert msg["text"] == "hello"

    conn.close()


def test_migrate_all_is_idempotent(old_db):
    """Calling migrate_all() twice produces no changes."""
    from core import migrations

    migrations.migrate_all()

    # Check row counts before second call
    conn = sqlite3.connect(old_db)
    tasks_before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    messages_before = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.close()

    # Second call
    result = migrations.migrate_all()
    assert result == migrations.CURRENT_VERSION

    # Check row counts are unchanged
    conn = sqlite3.connect(old_db)
    tasks_after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    messages_after = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.close()

    assert tasks_before == tasks_after == 1
    assert messages_before == messages_after == 1


def test_a_current_database_just_gets_stamped(tmp_path, monkeypatch):
    """A fresh database from init functions gets stamped without error."""
    db = tmp_path / "fresh.db"
    monkeypatch.setenv("SISYPHUS_DB", str(db))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    import core.db
    monkeypatch.setattr(core.db, "DB_PATH", str(db))

    from core import migrations

    result = migrations.migrate_all()
    assert result == migrations.CURRENT_VERSION
    assert migrations.db_version() == migrations.CURRENT_VERSION

    # Check all 12 tables exist
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()}
    required_tables = {
        "tasks", "messages", "runners", "agents", "usage", "merge_requests",
        "autopilots",
    }
    assert required_tables.issubset(tables)
    conn.close()
