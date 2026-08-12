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
