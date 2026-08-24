"""The written-down backlog, and the fact that anything can now read it.

A gap gets into docs/known-gaps.md because someone used the system and found
something wrong. That makes it the only backlog the project really has — and it
was readable only by a human opening the file, or by tools/goal_report.py
counting it. Nothing that answers "what next" could see it.
"""
from core import ledger

GAPS = """# Known gaps

## Section

### A thing that is fixed
**Status:** CLOSED 2026-08-24 — done.

### A thing that is not
**Status:** open, found 2026-08-24.
Details.

### A thing that forgot to say
Someone wrote it down and never added a status line.
"""


class TestReadingTheLedger:
    def test_an_entry_without_closed_counts_as_open(self, tmp_path):
        p = tmp_path / "known-gaps.md"
        p.write_text(GAPS)
        g = ledger.open_gaps(str(p))
        assert g["closed"] == 1
        assert g["titles"] == ["A thing that is not", "A thing that forgot to say"]

    def test_silence_counts_as_open_not_closed(self, tmp_path):
        """The safe way to be wrong about our own ledger: an entry with no
        status line is owed, not done."""
        p = tmp_path / "known-gaps.md"
        p.write_text("### No status here\nbody\n")
        assert ledger.open_gaps(str(p))["open"] == 1

    def test_a_missing_file_is_not_a_crash_and_not_a_zero_claim(self, tmp_path):
        g = ledger.open_gaps(str(tmp_path / "nope.md"))
        assert g == {"open": 0, "closed": 0, "titles": []}


class TestFindingTheLedger:
    def test_at_the_repo_root(self, tmp_path):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "known-gaps.md").write_text("### x\n")
        assert ledger.ledger_path({"source": str(tmp_path)})

    def test_one_level_down(self, tmp_path):
        """A repo root is not always the project root — this very project is
        added as `agentsbox` and keeps its ledger in `sisyphus/docs/`."""
        (tmp_path / "sisyphus" / "docs").mkdir(parents=True)
        (tmp_path / "sisyphus" / "docs" / "known-gaps.md").write_text("### x\n")
        found = ledger.ledger_path({"source": str(tmp_path)})
        assert found.endswith("sisyphus/docs/known-gaps.md")

    def test_never_deeper_than_one(self, tmp_path):
        """Bounded so it cannot wander into a vendored copy and report someone
        else's backlog as ours."""
        (tmp_path / "a" / "b" / "docs").mkdir(parents=True)
        (tmp_path / "a" / "b" / "docs" / "known-gaps.md").write_text("### x\n")
        assert ledger.ledger_path({"source": str(tmp_path)}) == ""

    def test_a_remote_url_has_no_file_here(self):
        assert ledger.ledger_path({"source": "https://github.com/x/y.git"}) == ""

    def test_a_repo_with_no_ledger_is_absent_not_zero(self, tmp_path):
        """No row at all, so nobody can read "keeps none" as "has nothing
        open"."""
        assert ledger.ledger_path({"source": str(tmp_path)}) == ""


class TestTheCopilotCanSeeIt:
    def test_the_snapshot_carries_gaps_failures_and_open_mrs(self, db_path, tmp_path,
                                                             monkeypatch):
        from core import copilot, mergerequests, store

        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "known-gaps.md").write_text(
            "### The plugin leaks its tools\n**Status:** open.\n")
        monkeypatch.setattr("core.repos.list_repos",
                            lambda: [{"name": "r", "source": str(tmp_path)}])

        t = store.create_task("ship it", repo="r")
        mr = mergerequests.create_mr(t["id"], "r", "b", "main", "Ship it")
        mergerequests.set_review_status(mr["id"], "approved")
        mergerequests.set_check_result(mr["id"], "passed", command="pytest -q")

        dead = store.create_task("this one died", repo="r")
        store.post_message(dead["id"], "orchestrator", kind="note",
                           text="[fail] engineer ended without a result")
        store.set_status(dead["id"], "failed")

        snap = copilot.board_snapshot()
        assert "ready to merge (gate open)" in snap
        assert "ended without a result" in snap
        assert "The plugin leaks its tools" in snap

    def test_a_failure_is_dated(self, db_path, monkeypatch):
        """Without a date a failure from ten days ago reads as current. The
        first press of "What next?" proposed fixing a cause that had been fixed
        that morning, because every line looked like today."""
        from core import copilot, store
        monkeypatch.setattr("core.repos.list_repos", lambda: [])
        dead = store.create_task("died", repo="r")
        store.post_message(dead["id"], "orchestrator", kind="note",
                           text="[fail] engineer ended without a result")
        store.set_status(dead["id"], "failed")

        snap = copilot.board_snapshot()
        section = snap.split("HOW RECENT RUNS DIED")[1]
        line = next(l for l in section.splitlines() if dead["id"] in l)
        assert line.strip().startswith("- 20")          # a date leads the row
        assert "check the git log" in snap

    def test_a_blocked_branch_is_reported_as_blocked_with_the_reason(self, db_path,
                                                                    monkeypatch):
        from core import copilot, mergerequests, store
        monkeypatch.setattr("core.repos.list_repos", lambda: [])
        t = store.create_task("ship it", repo="r")
        mergerequests.create_mr(t["id"], "r", "b", "main", "Ship it")

        snap = copilot.board_snapshot()
        assert "BLOCKED: no review verdict is recorded" in snap
        assert "gate open" not in snap
