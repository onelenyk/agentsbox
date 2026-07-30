"""Tests for core/mergerequests.py — the local Merge Request entity."""
import pytest

from core.mergerequests import (
    create_mr,
    get_mr,
    latest_for_task,
    list_mrs,
    mark_merged,
    set_status,
    set_target,
)


def _mk(task_id="task-1", **kw):
    kw.setdefault("repo", "sisyphus")
    kw.setdefault("source_branch", "sisyphus/task-1")
    kw.setdefault("target_branch", "main")
    return create_mr(task_id=task_id, **kw)


class TestEntity:
    def test_create_defaults_open(self, db_path):
        mr = _mk(title="Add glossary")
        assert mr["id"].startswith("mr-")
        assert mr["status"] == "open"
        assert mr["source_branch"] == "sisyphus/task-1"
        assert mr["target_branch"] == "main"
        assert get_mr(mr["id"])["title"] == "Add glossary"

    def test_latest_for_task(self, db_path):
        _mk(task_id="t", title="first")
        b = _mk(task_id="t", title="second")
        assert latest_for_task("t")["id"] == b["id"]
        assert latest_for_task("nope") is None

    def test_set_target(self, db_path):
        mr = _mk()
        set_target(mr["id"], "develop")
        assert get_mr(mr["id"])["target_branch"] == "develop"

    def test_set_status_and_list(self, db_path):
        a = _mk(task_id="a")
        b = _mk(task_id="b")
        set_status(b["id"], "closed")
        assert {m["id"] for m in list_mrs(status="open")} == {a["id"]}
        assert {m["id"] for m in list_mrs(status="closed")} == {b["id"]}

    def test_invalid_status_rejected(self, db_path):
        mr = _mk()
        with pytest.raises(ValueError):
            set_status(mr["id"], "bogus")

    def test_mark_merged(self, db_path):
        mr = _mk()
        mark_merged(mr["id"], "deadbeef1234")
        got = get_mr(mr["id"])
        assert got["status"] == "merged"
        assert got["merge_sha"] == "deadbeef1234"
        assert got["merged_at"]
