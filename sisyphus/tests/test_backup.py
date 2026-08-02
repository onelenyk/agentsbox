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
