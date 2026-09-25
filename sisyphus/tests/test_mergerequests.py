"""Tests for core/mergerequests.py — the local Merge Request entity."""
import pathlib
import re

import pytest

from core.mergerequests import (
    create_mr,
    find_open_mr,
    get_mr,
    latest_for_task,
    list_mrs,
    mark_merged,
    set_status,
    set_target,
    update_mr,
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

    def test_find_open_mr_returns_none_when_none_open(self, db_path):
        assert find_open_mr("task-x", "branch-x", "main") is None

    def test_find_open_mr_returns_matching_row(self, db_path):
        _mk(task_id="t1", source_branch="b1", target_branch="main")
        mr = find_open_mr("t1", "b1", "main")
        assert mr is not None
        assert mr["task_id"] == "t1"

    def test_find_open_mr_ignores_closed(self, db_path):
        mr = _mk(task_id="t1", source_branch="b1", target_branch="main")
        set_status(mr["id"], "closed")
        assert find_open_mr("t1", "b1", "main") is None

    def test_update_mr_refreshes_title_and_bumps_updated_at(self, db_path):
        mr = _mk(title="original")
        import time
        time.sleep(0.01)
        updated = update_mr(mr["id"], title="refreshed")
        assert updated["title"] == "refreshed"
        assert updated["id"] == mr["id"]
        assert updated["updated_at"] > mr["updated_at"]


def test_mark_merged_reached_only_through_record_merge_success():
    """Every path that marks an MR merged must flow through record_merge_success,
    the single place that owns 'a merge happened' — and that clears the
    self-update cache for the daemon's own checkout. A call to mark_merged
    anywhere else (the merge-resolve route once did) silently skips that clear,
    leaving the banner/restart endpoint stale for the cache TTL.

    Structural, grep-based guard over core/ and web/: a future call site must
    be routed through record_merge_success, not added next to it.
    """
    roots = [pathlib.Path(__file__).resolve().parents[1] / "core",
             pathlib.Path(__file__).resolve().parents[1] / "web"]
    offenders = []
    for root in roots:
        for py in sorted(root.glob("*.py")):
            text = py.read_text()
            for i, line in enumerate(text.splitlines(), 1):
                if "mark_merged(" not in line or "def mark_merged" in line:
                    continue
                enclosing = _enclosing_def(text, i)
                if enclosing != "record_merge_success":
                    offenders.append(f"{py.name}:{i} calls mark_merged from {enclosing}")
    assert offenders == [], (
        "mark_merged must only be reached through record_merge_success (which "
        f"clears the self-update cache), found: {offenders}")


def _enclosing_def(text: str, lineno: int) -> str:
    """The innermost `def` whose body contains *lineno*, by indentation."""
    lines = text.splitlines()
    for start in range(lineno - 1, -1, -1):
        m = re.match(r"^(\s*)def\s+(\w+)", lines[start])
        if m:
            return m.group(2)
    return "?"
