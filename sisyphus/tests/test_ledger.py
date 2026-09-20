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
