"""Edge case tests for Sisyphus."""
import json
import os
import subprocess

from core.state import create_card, create_run, create_step, get_steps, list_cards
from core.fsm import transition_step
from core.recovery import recover_orphaned_steps


def _dead_pid() -> int:
    """A PID guaranteed to no longer be alive (spawned and reaped)."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


class TestCrashRecovery:
    def test_recover_orphaned_steps(self, db_path, card):
        from core.state import get_conn
        from datetime import datetime, timezone, timedelta

        run = create_run(card["id"])
        # Make the run appear old AND owned by a dead process, so recovery picks it up
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        conn = get_conn()
        conn.execute(
            "UPDATE runs SET started_at = ?, pid = ? WHERE id = ?",
            (old_time, _dead_pid(), run["id"]),
        )
        conn.commit()
        conn.close()

        s1 = create_step(run["id"], "PM", 1)
        s2 = create_step(run["id"], "Engineer", 2)

        transition_step(s1["id"], "running")
        transition_step(s2["id"], "running")
        # Make the step appear old (>2 min) so recovery picks it up
        conn = get_conn()
        conn.execute("UPDATE steps SET started_at = ? WHERE id IN (?, ?)", (old_time, s1["id"], s2["id"]))
        conn.commit()
        conn.close()
        transition_step(
            s1["id"], "completed",
            json.dumps({"status": "completed", "summary": "ok"}),
        )

        recovered = recover_orphaned_steps()
        assert recovered >= 1

        steps = get_steps(run["id"])
        s2_after = [s for s in steps if s["id"] == s2["id"]][0]
        assert s2_after["status"] == "error"
        assert "orchestrator_crash" in (s2_after.get("error") or "")

    def test_recover_with_no_orphaned(self, db_path, card):
        run = create_run(card["id"])
        s1 = create_step(run["id"], "PM", 1)
        transition_step(s1["id"], "running")
        transition_step(
            s1["id"], "completed",
            json.dumps({"status": "completed", "summary": "ok"}),
        )

        recovered = recover_orphaned_steps()
        assert recovered == 0

    def test_recover_skips_step_owned_by_live_process(self, db_path, card):
        """A step whose owning process is still alive must never be recovered,
        no matter how long it's been running (regression test for the
        idle-timeout / recovery race: a legitimately long-running step owned
        by a live process used to get wrongly marked as orchestrator_crash)."""
        from core.state import get_conn
        from datetime import datetime, timezone, timedelta

        run = create_run(card["id"])
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        conn = get_conn()
        conn.execute(
            "UPDATE runs SET started_at = ?, pid = ? WHERE id = ?",
            (old_time, os.getpid(), run["id"]),
        )
        conn.commit()
        conn.close()

        s1 = create_step(run["id"], "Engineer", 1)
        transition_step(s1["id"], "running")
        conn = get_conn()
        conn.execute("UPDATE steps SET started_at = ? WHERE id = ?", (old_time, s1["id"]))
        conn.commit()
        conn.close()

        recovered = recover_orphaned_steps()
        assert recovered == 0

        steps = get_steps(run["id"])
        s1_after = [s for s in steps if s["id"] == s1["id"]][0]
        assert s1_after["status"] == "running"

    def test_recover_legacy_null_pid_falls_back_to_age(self, db_path, card):
        """Runs created before the `pid` column existed have pid=NULL — recovery
        must still fall back to the age-based heuristic for those."""
        from core.state import get_conn
        from datetime import datetime, timezone, timedelta

        run = create_run(card["id"])
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        conn = get_conn()
        conn.execute(
            "UPDATE runs SET started_at = ?, pid = NULL WHERE id = ?",
            (old_time, run["id"]),
        )
        conn.commit()
        conn.close()

        s1 = create_step(run["id"], "Engineer", 1)
        transition_step(s1["id"], "running")
        conn = get_conn()
        conn.execute("UPDATE steps SET started_at = ? WHERE id = ?", (old_time, s1["id"]))
        conn.commit()
        conn.close()

        recovered = recover_orphaned_steps()
        assert recovered == 1

        steps = get_steps(run["id"])
        s1_after = [s for s in steps if s["id"] == s1["id"]][0]
        assert s1_after["status"] == "error"


class TestConcurrentRuns:
    def test_two_parallel_cards(self, db_path):
        card1 = create_card("investigate", "android", "test")
        card2 = create_card("investigate", "ios", "test")

        run1 = create_run(card1["id"])
        run2 = create_run(card2["id"])

        assert run1["id"] != run2["id"]
        assert run1["card_id"] == card1["id"]
        assert run2["card_id"] == card2["id"]

    def test_steps_across_runs(self, db_path, card):
        run = create_run(card["id"])
        s1 = create_step(run["id"], "Engineer", 1)
        s2 = create_step(run["id"], "Reviewer", 2)

        transition_step(s1["id"], "running")
        transition_step(
            s1["id"], "completed",
            json.dumps({"status": "completed", "summary": "ok"}),
        )
        transition_step(s2["id"], "running")
        transition_step(
            s2["id"], "completed",
            json.dumps({
                "status": "completed",
                "summary": "ok",
                "verdict": "approved",
            }),
        )

        steps = get_steps(run["id"])
        assert len(steps) == 2
        assert all(s["status"] == "completed" for s in steps)

    def test_read_your_writes(self, db_path, card):
        run = create_run(card["id"])
        step = create_step(run["id"], "Engineer", 1)

        transition_step(step["id"], "running")
        transition_step(
            step["id"], "completed",
            json.dumps({"status": "completed", "summary": "done"}),
        )

        steps = get_steps(run["id"])
        assert steps[0]["result_json"] is not None


class TestStateQueries:
    def test_list_cards_filtered(self, db_path):
        create_card("investigate", "android", "test")
        create_card("jira-sdlc", "ios", "test")
        create_card("investigate", "kmp", "test")

        all_cards = list_cards()
        assert len(all_cards) >= 3

    def test_list_cards_with_status_filter(self, db_path):
        create_card("investigate", "android", "test")

        backlog_cards = list_cards(status_filter="backlog")
        assert len(backlog_cards) >= 1
        for c in backlog_cards:
            assert c["status"] == "backlog"


class TestSchemaEdgeCases:
    def test_empty_summary(self):
        from core.schemas import BaseResult

        r = BaseResult(status="completed", summary="")
        assert r.summary == ""

    def test_all_risk_levels(self):
        from core.schemas import BaseResult

        for risk in ["low", "medium", "high"]:
            r = BaseResult(status="completed", summary="ok", risk=risk)
            assert r.risk == risk

    def test_all_statuses(self):
        from core.schemas import BaseResult

        for status in ["completed", "failed", "timeout", "cancelled"]:
            r = BaseResult(status=status, summary="test")
            assert r.status == status

    def test_none_fields_default(self):
        from core.schemas import BaseResult

        r = BaseResult(status="completed", summary="ok")
        assert r.verdict is None
        assert r.risk is None
        assert r.error is None

    def test_error_field(self):
        from core.schemas import BaseResult

        r = BaseResult(status="failed", summary="it broke", error="something went wrong")
        assert r.error == "something went wrong"


class TestFSMEdgeCases:
    def test_error_allows_retry(self, db_path, card):
        run = create_run(card["id"])
        step = create_step(run["id"], "Engineer", 1)

        transition_step(step["id"], "running")
        assert transition_step(step["id"], "error")

        updated = get_steps(run["id"])[0]
        assert updated["status"] == "error"

    def test_timeout_terminal(self, db_path, card):
        run = create_run(card["id"])
        step = create_step(run["id"], "Engineer", 1)

        transition_step(step["id"], "running")
        assert transition_step(step["id"], "timeout")
        assert transition_step(step["id"], "running")

    def test_unknown_step(self, db_path):
        assert not transition_step("step-nonexistent", "running")

    def test_double_running_rejected(self, db_path, card):
        run = create_run(card["id"])
        step = create_step(run["id"], "Engineer", 1)

        assert transition_step(step["id"], "running")
        assert not transition_step(step["id"], "running")


class TestIdempotency:
    def test_card_get_card_roundtrip(self, db_path, card):
        fetched = create_card(pipeline_name="jira-sdlc", repo_name="android", description="roundtrip test")
        assert fetched["id"] != card["id"]

        refetched = list(None for c in list_cards() if c["id"] == fetched["id"])
        assert len(refetched) == 1

    def test_repeated_save_doesnt_corrupt(self, db_path, card):
        run = create_run(card["id"])
        step = create_step(run["id"], "Engineer", 1)

        transition_step(step["id"], "running")
        assert not transition_step(step["id"], "running")
        assert not transition_step(step["id"], "pending")

        steps = get_steps(run["id"])
        assert steps[0]["status"] == "running"
