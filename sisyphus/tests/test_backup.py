"""Backup/restore of the SQLite database (core/backup.py)."""
import sqlite3

from core import backup, store


def _tables(path):
    c = sqlite3.connect(str(path))
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        c.close()


def test_snapshot_creates_valid_copy(db_path):
    store.create_task("in the snapshot", repo="r")
    info = backup.snapshot(label="manual")
    assert info["name"].startswith("sisyphus-") and info["name"].endswith(".db")
    assert info["label"] == "manual"
    assert info["size"] > 0
    # the snapshot is a real, openable DB carrying our schema
    from pathlib import Path
    snap = Path(backup._backup_dir()) / info["name"]
    assert "tasks" in _tables(snap)


def test_list_backups_newest_first(db_path):
    a = backup.snapshot()
    b = backup.snapshot()
    names = [x["name"] for x in backup.list_backups()]
    assert names == sorted([a["name"], b["name"]], reverse=True)


def test_restore_swaps_db_and_keeps_safety_copy(db_path):
    # state A → snapshot → mutate to state B → restore A
    t = store.create_task("state A task", repo="r")
    snap = backup.snapshot(label="stateA")

    store.delete_task(t["id"])
    assert store.get_task(t["id"]) is None  # now state B

    res = backup.restore(snap["name"])
    assert res["restored"] == snap["name"]
    assert store.get_task(t["id"]) is not None  # state A is back

    # the restore saved a reversible safety copy of state B
    safety = [x for x in backup.list_backups() if x["label"] == "pre-restore"]
    assert safety, "restore should leave a pre-restore safety snapshot"


def test_restore_rejects_bad_name(db_path):
    import pytest
    for bad in ("../evil.db", "evil.db", "sisyphus-x.db/..", "nope"):
        with pytest.raises((ValueError, FileNotFoundError)):
            backup.restore(bad)


def test_delete_backup(db_path):
    info = backup.snapshot()
    assert backup.delete_backup(info["name"]) is True
    assert info["name"] not in [x["name"] for x in backup.list_backups()]
    assert backup.delete_backup("sisyphus-nope.db") is False
    assert backup.delete_backup("../etc/passwd") is False


def test_rotation_keeps_only_retention(db_path, monkeypatch):
    monkeypatch.setattr(backup, "RETENTION", 3)
    made = [backup.snapshot(label=f"n{i}") for i in range(6)]
    kept = {x["name"] for x in backup.list_backups()}
    assert len(kept) == 3
    # the three most recent survive
    assert {m["name"] for m in made[-3:]} == kept


def test_backup_tick_snapshots_with_the_auto_label(db_path):
    info = backup.backup_tick()
    assert info is not None
    assert "auto" in info["name"]
    from pathlib import Path
    snap = Path(backup._backup_dir()) / info["name"]
    assert snap.exists()


def test_backup_tick_failure_reports_outward_and_returns_none(db_path, monkeypatch):
    from core import notify

    # Track notify.send calls
    calls = []

    def mock_send(event, task_id="", title="", reason="", wait=False):
        calls.append({"event": event, "task_id": task_id, "title": title, "reason": reason, "wait": wait})
        return False  # no webhook

    monkeypatch.setattr(backup, "snapshot", lambda label="": (_ for _ in ()).throw(RuntimeError("disk full")))
    monkeypatch.setattr(notify, "send", mock_send)

    result = backup.backup_tick()
    assert result is None
    assert len(calls) == 1
    assert calls[0]["event"] == "backup_failed"
    assert "disk full" in calls[0]["reason"]


def test_restore_brings_back_the_snapshotted_state(db_path):
    # Create initial state and snapshot
    t1 = store.create_task("before snapshot", repo="r")
    snap = backup.snapshot(label="e2e")

    # Mutate to second state
    t2 = store.create_task("after snapshot", repo="r")

    # Restore from snapshot
    restore_result = backup.restore(snap["name"])
    assert "pre-restore" in restore_result["safety"]

    # Verify the pre-restore safety snapshot exists and is valid
    from pathlib import Path
    safety_path = Path(backup._backup_dir()) / restore_result["safety"]
    assert safety_path.exists()
    assert backup._integrity_ok(safety_path)

    # Check state: before snapshot task is present, after snapshot task is gone
    assert store.get_task(t1["id"]) is not None
    assert store.get_task(t2["id"]) is None


def test_disabled_interval_means_no_loop(db_path, monkeypatch):
    import asyncio

    def mock_get_config(k, d=None):
        if k == "backup.interval_seconds":
            return 0
        return d

    monkeypatch.setattr("core.config.get_config", mock_get_config)

    # This should return immediately, not hang
    asyncio.run(backup.backup_forever())


def test_restore_upgrades_the_snapshot_on_the_spot(db_path):
    """A restored old snapshot must not run live against new code with an old
    schema until the next daemon restart — restore() migrates immediately."""
    from core import migrations, store
    store.create_task("pre-snapshot state")
    info = backup.snapshot(label="verstamp")
    # Simulate an old snapshot: strip the version stamp from the backup file.
    import core.db
    from pathlib import Path
    bfile = str(Path(core.db.DB_PATH).parent / "backups" / info["name"])
    c = sqlite3.connect(bfile)
    c.execute("PRAGMA user_version = 0")
    c.close()
    backup.restore(info["name"])
    assert migrations.db_version() == migrations.CURRENT_VERSION
