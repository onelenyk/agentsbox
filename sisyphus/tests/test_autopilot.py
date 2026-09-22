"""Autopilot core — schedule math, CRUD, firing, tick loop (core/autopilot.py)."""
from datetime import datetime, timezone

from core import autopilot as ap
from core import store


def _emit_recorder(monkeypatch):
    calls = []
    monkeypatch.setattr("core.orchestrator.emit", lambda tid, ev: calls.append((tid, ev)))
    return calls


# ── schedule math ──────────────────────────────────────────────────────

def test_compute_next_run_cron(db_path):
    a = {"cron": "0 3 * * *", "interval_seconds": 0}
    after = datetime(2026, 8, 2, 15, 0, tzinfo=timezone.utc)
    assert ap.compute_next_run(a, after) == "2026-08-03T03:00:00+00:00"


def test_compute_next_run_interval(db_path):
    a = {"cron": "", "interval_seconds": 3600}
    after = datetime(2026, 8, 2, 15, 0, tzinfo=timezone.utc)
    assert ap.compute_next_run(a, after) == "2026-08-02T16:00:00+00:00"


def test_is_schedule(db_path):
    assert ap.is_schedule({"trigger_type": "schedule", "cron": "* * * * *", "interval_seconds": 0})
    assert ap.is_schedule({"trigger_type": "schedule", "cron": "", "interval_seconds": 60})
    assert not ap.is_schedule({"trigger_type": "webhook", "cron": "", "interval_seconds": 0})
    assert not ap.is_schedule({"trigger_type": "schedule", "cron": "", "interval_seconds": 0})


# ── template ───────────────────────────────────────────────────────────

def test_render_template_nested_and_missing(db_path):
    ctx = {"issue": {"key": "M-1", "summary": "Offline bug"}}
    assert ap.render_template("Analyze {{ issue.key }}: {{ issue.summary }}", ctx) == "Analyze M-1: Offline bug"
    assert ap.render_template("x {{ nope.field }} y", ctx) == "x  y"


# ── CRUD ───────────────────────────────────────────────────────────────

def test_crud_roundtrip(db_path):
    a = ap.create_autopilot(name="Nightly", trigger_type="schedule",
                            cron="0 3 * * *", goal_template="scan repos", repo="r")
    assert a["id"].startswith("ap-") and a["enabled"] == 1
    assert a["next_run"]  # scheduled -> has a next run
    assert ap.get_autopilot(a["id"])["name"] == "Nightly"
    assert len(ap.list_autopilots()) == 1

    ap.update_autopilot(a["id"], name="Nightly v2", interval_seconds=0)
    assert ap.get_autopilot(a["id"])["name"] == "Nightly v2"

    ap.set_enabled(a["id"], False)
    assert ap.get_autopilot(a["id"])["enabled"] == 0
    assert ap.list_autopilots(enabled=True) == []

    assert ap.delete_autopilot(a["id"]) is True
    assert ap.get_autopilot(a["id"]) is None


# ── firing ─────────────────────────────────────────────────────────────

def test_fire_creates_task_and_emits(db_path, monkeypatch):
    calls = _emit_recorder(monkeypatch)
    a = ap.create_autopilot(name="AP", trigger_type="schedule", interval_seconds=3600,
                            goal_template="do {{ x }}", repo="myrepo")
    tid = ap.fire(a, context={"x": "thing"})
    assert tid
    t = store.get_task(tid)
    assert t["goal"] == "do thing" and t["repo"] == "myrepo"
    assert calls == [(tid, "created")]
    after = ap.get_autopilot(a["id"])
    assert after["run_count"] == 1 and after["last_task_id"] == tid


def test_fire_skips_when_previous_still_running(db_path, monkeypatch):
    _emit_recorder(monkeypatch)
    a = ap.create_autopilot(name="Monitor", trigger_type="schedule", interval_seconds=60,
                            goal_template="check", skip_if_running=1)
    first = ap.fire(a)  # creates an active task
    assert first
    a = ap.get_autopilot(a["id"])
    assert ap.fire(a) is None  # prev task still active -> skip
    assert ap.get_autopilot(a["id"])["run_count"] == 1

    store.set_status(first, "done")
    a = ap.get_autopilot(a["id"])
    assert ap.fire(a) is not None  # prev done -> fires again


# ── tick loop ──────────────────────────────────────────────────────────

def test_tick_fires_due_and_advances(db_path, monkeypatch):
    _emit_recorder(monkeypatch)
    a = ap.create_autopilot(name="Due", trigger_type="schedule", interval_seconds=3600,
                            goal_template="run")
    ap._set_next_run(a["id"], "2000-01-01T00:00:00+00:00")  # force due
    assert ap.tick_once() == 1
    # next_run advanced into the future -> not due again
    now = datetime.now(timezone.utc).isoformat()
    assert ap.get_autopilot(a["id"])["next_run"] > now
    assert ap.tick_once() == 0


def test_tick_ignores_disabled_and_future(db_path, monkeypatch):
    _emit_recorder(monkeypatch)
    disabled = ap.create_autopilot(name="Off", trigger_type="schedule",
                                   interval_seconds=60, enabled=0)
    ap._set_next_run(disabled["id"], "2000-01-01T00:00:00+00:00")
    future = ap.create_autopilot(name="Later", trigger_type="schedule", interval_seconds=99999)
    assert ap.tick_once() == 0
    assert ap.get_autopilot(future["id"])["run_count"] == 0


# ── webhook triggers ───────────────────────────────────────────────────

def test_event_key(db_path):
    assert ap.event_key("github", {"action": "opened"}) == "opened"
    assert ap.event_key("gitlab", {"object_kind": "merge_request"}) == "merge_request"
    assert ap.event_key("jira", {"webhookEvent": "jira:issue_created"}) == "jira:issue_created"
    assert ap.event_key("bitbucket", {"x": 1}) == ""


def test_webhook_autopilot_fires_and_renders(db_path, monkeypatch):
    _emit_recorder(monkeypatch)
    ap.create_autopilot(name="Jira intake", trigger_type="webhook",
                        webhook_platform="jira", webhook_event="issue_created",
                        goal_template="Analyze {{ issue.key }}: {{ issue.fields.summary }}")
    payload = {"webhookEvent": "jira:issue_created",
               "issue": {"key": "SLN-42", "fields": {"summary": "Offline bug"}}}
    fired = ap.fire_webhook_autopilots("jira", payload)
    assert len(fired) == 1
    t = store.get_task(fired[0]["task_id"])
    assert t["goal"] == "Analyze SLN-42: Offline bug"


def test_webhook_autopilot_respects_filters(db_path, monkeypatch):
    _emit_recorder(monkeypatch)
    ap.create_autopilot(name="only opened", trigger_type="webhook",
                        webhook_platform="github", webhook_event="opened",
                        goal_template="pr {{ action }}")
    # wrong event -> no fire
    assert ap.fire_webhook_autopilots("github", {"action": "closed"}) == []
    # wrong platform -> no fire
    assert ap.fire_webhook_autopilots("gitlab", {"object_kind": "merge_request"}) == []
    # matching -> fires
    assert len(ap.fire_webhook_autopilots("github", {"action": "opened"})) == 1


def test_webhook_ignores_schedule_and_disabled(db_path, monkeypatch):
    _emit_recorder(monkeypatch)
    ap.create_autopilot(name="sched", trigger_type="schedule", interval_seconds=60,
                        goal_template="x")
    ap.create_autopilot(name="off", trigger_type="webhook", webhook_platform="jira",
                        goal_template="x", enabled=0)
    assert ap.fire_webhook_autopilots("jira", {"webhookEvent": "jira:issue_created"}) == []


def test_reconcile_skips_missed(db_path):
    a = ap.create_autopilot(name="Nightly", trigger_type="schedule", cron="0 3 * * *")
    ap._set_next_run(a["id"], "2000-01-01T00:00:00+00:00")  # a long-missed fire
    ap.reconcile_next_runs()
    now = datetime.now(timezone.utc).isoformat()
    assert ap.get_autopilot(a["id"])["next_run"] > now  # recomputed to the future, not replayed


# ── display title ───────────────────────────────────────────────────────

def test_fire_sets_title_for_schedule(db_path, monkeypatch):
    _emit_recorder(monkeypatch)
    a = ap.create_autopilot(name="Nightly Scan", trigger_type="schedule",
                            interval_seconds=3600, goal_template="scan all")
    tid = ap.fire(a)
    t = store.get_task(tid)
    assert t["title"]
    assert t["title"].startswith("Nightly Scan")
    assert "·" in t["title"]
    assert len(t["title"]) <= 60


def test_fire_sets_title_for_webhook(db_path, monkeypatch):
    _emit_recorder(monkeypatch)
    a = ap.create_autopilot(name="Jira Bot", trigger_type="webhook",
                            webhook_platform="jira", webhook_event="issue_created",
                            goal_template="handle {{ issue.key }}")
    payload = {"webhookEvent": "jira:issue_created", "issue": {"key": "SLN-1"}}
    tid = ap.fire(a, context=payload)
    t = store.get_task(tid)
    assert t["title"]
    assert "Jira Bot" in t["title"]
    assert len(t["title"]) <= 60


def test_display_title_falls_back_when_no_title(db_path):
    from core.store import task_display_title
    t = store.create_task("Do the thing\nsecond line item")
    assert task_display_title(t) == "Do the thing"
    assert "second" not in task_display_title(t)


def test_display_title_uses_explicit_title(db_path):
    from core.store import task_display_title
    t = store.create_task("Long goal that spans\nmultiple lines", title="Short Title")
    assert task_display_title(t) == "Short Title"


def test_fire_sets_title_for_schedule_via_webhook_path(db_path, monkeypatch):
    _emit_recorder(monkeypatch)
    a = ap.create_autopilot(name="MR Monitor", trigger_type="schedule",
                            interval_seconds=3600, goal_template="review MRs")
    ap._set_next_run(a["id"], "2000-01-01T00:00:00+00:00")
    ap.tick_once()
    tasks = store.list_tasks()
    assert len(tasks) == 1
    assert tasks[0]["title"]
    assert tasks[0]["title"].startswith("MR Monitor")


# ── an automation that can never fire ───────────────────────────────────

class TestAnAutomationThatCanNeverFireSaysSo:
    """A schedule whose cron the scheduler cannot parse is stored with
    next_run NULL, and NULL rendered as the same "—" a webhook autopilot and a
    paused one show. Three states behind one dash, two of them healthy. The
    create-time warning is the only thing ever said about the third, and it
    scrolls out of the log — tick_once cannot repeat it, because it only looks
    at rows whose next_run is set."""

    def test_a_cron_the_scheduler_cannot_parse_is_named(self, db_path):
        a = ap.create_autopilot(name="typo", trigger_type="schedule", cron="0 3 * * MONDAY")
        problem = ap.schedule_problem(a)
        assert problem and "can never fire" in problem
        assert "MONDAY" in problem          # says which cron, not just that one is bad
        assert "weekday" in problem.lower()  # and what the parser objected to

    def test_a_schedule_with_neither_cron_nor_interval_is_named(self, db_path):
        a = ap.create_autopilot(name="empty", trigger_type="schedule")
        assert "can never fire" in ap.schedule_problem(a)

    def test_a_webhook_autopilot_is_not_a_problem(self, db_path):
        """It fires on a callback, so having no next_run is its healthy state."""
        a = ap.create_autopilot(name="hook", trigger_type="webhook", webhook_platform="jira")
        assert a["next_run"] is None
        assert ap.schedule_problem(a) is None

    def test_a_paused_schedule_is_not_a_problem(self, db_path):
        """`enabled` is the operator's own choice — a pause is not a defect."""
        a = ap.create_autopilot(name="nightly", trigger_type="schedule",
                                cron="0 3 * * *", enabled=0)
        assert ap.schedule_problem(a) is None

    def test_a_valid_schedule_is_not_a_problem(self, db_path):
        assert ap.schedule_problem(ap.create_autopilot(
            name="cron", trigger_type="schedule", cron="0 3 * * *")) is None
        assert ap.schedule_problem(ap.create_autopilot(
            name="interval", trigger_type="schedule", interval_seconds=3600)) is None

    def test_the_broken_row_is_otherwise_silent_forever(self, db_path, monkeypatch):
        """The behaviour that made this worth surfacing: the row sits enabled,
        the tick passes over it every time, and nothing warns again."""
        _emit_recorder(monkeypatch)
        a = ap.create_autopilot(name="typo", trigger_type="schedule", cron="0 3 * * MONDAY")
        assert a["enabled"] == 1 and a["next_run"] is None
        ap.reconcile_next_runs()
        assert ap.get_autopilot(a["id"])["next_run"] is None
        assert ap.tick_once() == 0
        assert store.list_tasks() == []

    def test_the_parser_that_judges_is_the_parser_that_fires(self, db_path):
        """schedule_problem and compute_next_run must not be able to disagree:
        anything called fireable produces a next run, and anything called
        broken produces none."""
        for cron in ("0 3 * * *", "*/5 * * * *", "0 3 * * MONDAY", "not a cron", "0 3 * *"):
            a = {"cron": cron, "interval_seconds": 0, "trigger_type": "schedule"}
            assert (ap.schedule_problem(a) is None) == (ap.compute_next_run(a) is not None), cron


class TestTheOperatorIsToldWhileStillLooking:
    """The card carries the reason, but a form submit is the moment the typo
    is fixable — so create and update say it too rather than saving quietly."""

    def _client(self):
        from fastapi.testclient import TestClient
        from web.app import app
        return TestClient(app)

    def _form(self, **over):
        f = {"name": "nightly", "trigger_type": "schedule", "cron": "0 3 * * *",
             "interval_seconds": "0", "goal_template": "scan"}
        f.update(over)
        return f

    def test_creating_a_broken_schedule_is_saved_and_reported(self, db_path):
        r = self._client().post("/api/autopilots", data=self._form(cron="0 3 * * MONDAY"))
        assert r.status_code == 200
        assert "can never fire" in r.text
        assert len(ap.list_autopilots()) == 1   # saved, not thrown away

    def test_creating_a_valid_schedule_is_not_reported(self, db_path):
        r = self._client().post("/api/autopilots", data=self._form())
        assert r.status_code == 200
        assert "can never fire" not in r.text

    def test_editing_a_good_schedule_into_a_broken_one_is_reported(self, db_path):
        a = ap.create_autopilot(name="nightly", trigger_type="schedule", cron="0 3 * * *")
        r = self._client().post(f"/api/autopilots/{a['id']}",
                                data=self._form(cron="0 3 * * MONDAY"))
        assert "can never fire" in r.text

    def test_the_card_carries_the_reason_afterwards(self, db_path):
        ap.create_autopilot(name="typo", trigger_type="schedule", cron="0 3 * * MONDAY")
        r = self._client().get("/api/autopilots/list")
        assert "can never fire" in r.text
        assert "never" in r.text


# ── the unattended tick loop ────────────────────────────────────────────

class TestOneBrokenAutopilotCannotStallTheNightlyLoop:
    """tick_once runs unattended every 30s. One automation that throws must not
    take the others down, and must not be retried on every tick until morning."""

    def test_a_fire_that_raises_is_logged_and_the_clock_still_advances(
            self, db_path, monkeypatch, caplog):
        import logging
        a = ap.create_autopilot(name="boom", trigger_type="schedule", cron="0 3 * * *")
        ap._set_next_run(a["id"], "2000-01-01T00:00:00+00:00")

        def _raise(_ap, context=None):
            raise RuntimeError("the runner binary is gone")
        monkeypatch.setattr(ap, "fire", _raise)

        with caplog.at_level(logging.ERROR, logger="core.autopilot"):
            assert ap.tick_once() == 0
        assert "fire failed" in caplog.text
        assert "the runner binary is gone" in caplog.text
        # advanced past now, so the next tick 30s later passes over it
        assert ap.get_autopilot(a["id"])["next_run"] > datetime.now(timezone.utc).isoformat()

    def test_the_other_due_automations_still_fire(self, db_path, monkeypatch):
        _emit_recorder(monkeypatch)
        bad = ap.create_autopilot(name="bad", trigger_type="schedule",
                                  cron="0 3 * * *", goal_template="x")
        good = ap.create_autopilot(name="good", trigger_type="schedule",
                                   cron="0 4 * * *", goal_template="work")
        for a in (bad, good):
            ap._set_next_run(a["id"], "2000-01-01T00:00:00+00:00")
        real_fire = ap.fire

        def _selective(a, context=None):
            if a["id"] == bad["id"]:
                raise RuntimeError("boom")
            return real_fire(a, context)
        monkeypatch.setattr(ap, "fire", _selective)

        assert ap.tick_once() == 1
        tasks = store.list_tasks()
        assert len(tasks) == 1 and tasks[0]["title"].startswith("good")

    def test_a_webhook_row_is_passed_over_by_the_clock(self, db_path, monkeypatch):
        """A next_run left on a row that has since become a webhook trigger is
        not a licence to fire it on a schedule."""
        _emit_recorder(monkeypatch)
        a = ap.create_autopilot(name="hook", trigger_type="webhook",
                                webhook_platform="jira", goal_template="x")
        ap._set_next_run(a["id"], "2000-01-01T00:00:00+00:00")
        assert ap.tick_once() == 0
        assert store.list_tasks() == []


class TestWhenTheNextAutomationIsDue:
    """next_due_at feeds the heartbeat note — the operator's answer to "is
    anything going to happen tonight?"."""

    def test_reports_the_soonest_enabled_one(self, db_path):
        far = ap.create_autopilot(name="far", trigger_type="schedule", cron="0 3 * * *")
        near = ap.create_autopilot(name="near", trigger_type="schedule", interval_seconds=60)
        ap._set_next_run(far["id"], "2030-01-01T00:00:00+00:00")
        ap._set_next_run(near["id"], "2027-01-01T00:00:00+00:00")
        assert ap.next_due_at() == "2027-01-01T00:00:00+00:00"

    def test_ignores_a_paused_one(self, db_path):
        paused = ap.create_autopilot(name="paused", trigger_type="schedule", cron="0 3 * * *")
        ap._set_next_run(paused["id"], "2027-01-01T00:00:00+00:00")
        ap.set_enabled(paused["id"], False)
        ap._set_next_run(paused["id"], "2027-01-01T00:00:00+00:00")  # set_enabled recomputes
        assert ap.next_due_at() is None

    def test_is_none_when_only_webhooks_exist(self, db_path):
        ap.create_autopilot(name="hook", trigger_type="webhook", webhook_platform="jira")
        assert ap.next_due_at() is None

    def test_is_none_with_no_autopilots_at_all(self, db_path):
        assert ap.next_due_at() is None


class TestTheTitleAnAutomationGivesItsTask:

    def test_an_unnamed_autopilot_gets_no_title(self, db_path):
        assert ap._make_title({"name": "  ", "trigger_type": "schedule"}) is None

    def test_a_webhook_with_no_context_falls_back_to_the_name(self, db_path):
        assert ap._make_title({"name": "Jira Bot", "trigger_type": "webhook",
                               "webhook_platform": "jira"}) == "Jira Bot"

    def test_a_webhook_whose_payload_names_no_event_falls_back_too(self, db_path):
        assert ap._make_title({"name": "Jira Bot", "trigger_type": "webhook",
                               "webhook_platform": "bitbucket"}, context={"x": 1}) == "Jira Bot"

    def test_an_unrecognised_trigger_type_gets_no_title(self, db_path):
        assert ap._make_title({"name": "Manual", "trigger_type": "manual"}) is None

    def test_a_long_name_is_truncated_to_sixty(self, db_path):
        title = ap._make_title({"name": "N" * 80, "trigger_type": "schedule"})
        assert len(title) == 60 and title.endswith("…")


class TestSayingWhenAnAutomationFires:
    """`0 3 * * *` is a fact about cron, not an answer to "when does this
    run" — and the board has room for the answer, not the fact."""

    def _ap(self, **kw):
        return {"trigger_type": "schedule", **kw}

    def test_a_daily_cron_reads_as_a_time(self):
        assert ap.describe_schedule(self._ap(cron="0 3 * * *")) == "daily 03:00"
        assert ap.describe_schedule(self._ap(cron="7 6 * * *")) == "daily 06:07"

    def test_a_weekday_cron_names_the_day(self):
        assert ap.describe_schedule(self._ap(cron="0 9 * * 1")) == "Mon 09:00"

    def test_step_and_hourly_crons(self):
        assert ap.describe_schedule(self._ap(cron="*/15 * * * *")) == "every 15m"
        assert ap.describe_schedule(self._ap(cron="30 * * * *")) == "hourly at :30"
        assert ap.describe_schedule(self._ap(cron="0 */4 * * *")) == "every 4h at :00"

    def test_an_interval_needs_no_cron(self):
        assert ap.describe_schedule(self._ap(interval_seconds=1800)) == "every 30m"
        assert ap.describe_schedule(self._ap(interval_seconds=7200)) == "every 2h"

    def test_a_webhook_says_what_calls_it(self):
        phrase = ap.describe_schedule(
            {"trigger_type": "webhook", "webhook_platform": "jira", "webhook_event": "issue_created"})
        assert phrase == "webhook · jira · issue_created"

    def test_anything_unphraseable_falls_back_to_the_cron_itself(self):
        """Better the raw field than a confident guess about when it fires."""
        assert ap.describe_schedule(self._ap(cron="5 4 3 2 1")) == "5 4 3 2 1"
        assert ap.describe_schedule(self._ap(cron="totally bogus")) == "totally bogus"
        assert ap.describe_schedule(self._ap()) == "no schedule"


class TestTheClockIsTheReaders:
    """Cron is read in UTC by the scheduler; a person reads their own clock."""

    def _in_kyiv(self, monkeypatch):
        import time
        monkeypatch.setenv("TZ", "Europe/Kyiv")
        time.tzset()

    def test_a_utc_cron_is_said_in_local_time(self, monkeypatch):
        from core.autopilot import describe_schedule
        self._in_kyiv(monkeypatch)
        # 06:07 UTC is 09:07 in Kyiv in summer and 08:07 in winter
        assert describe_schedule({"trigger_type": "schedule", "cron": "7 6 * * *"}) in ("daily 09:07", "daily 08:07")

    def test_a_weekday_moves_when_the_shift_crosses_midnight(self, monkeypatch):
        from core.autopilot import describe_schedule
        self._in_kyiv(monkeypatch)
        assert describe_schedule({"trigger_type": "schedule", "cron": "30 22 * * 1"}).startswith("Tue 0")

    def test_explain_says_when_next_and_last(self):
        from datetime import datetime, timezone
        from core.autopilot import explain
        now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
        x = explain({"trigger_type": "schedule", "cron": "7 6 * * *", "enabled": 1,
                     "next_run": "2026-09-23T06:07:00+00:00", "last_run": "2026-09-22T06:07:06+00:00"}, now)
        assert x["sentence"] == "Every day at 06:07 (UTC)"
        assert x["detail"] == "cron “7 6 * * *”, read in UTC"
        assert x["next"] == {"day": "tomorrow", "time": "06:07", "rel": "in 18h 07m"}
        assert x["last"]["day"] == "today" and x["last"]["rel"] == "5h 52m ago"

    def test_a_webhook_says_what_sets_it_off_and_where_it_listens(self):
        from core.autopilot import explain
        x = explain({"trigger_type": "webhook", "webhook_platform": "jira", "webhook_event": "issue_created"})
        assert x["sentence"] == "When a Jira issue is created"
        assert x["detail"] == "listens at POST /api/v1/webhooks/jira for “issue_created”"
        assert x["next"] is None and x["last"] is None
