"""Subscription limits and model classes — which runner a spawn gets.

The failure this guards: a runner pinned to an agent kept being used after its
subscription ran dry, and switching meant a human editing the runner by hand.
Now an agent's class lists runner+model options in order, and a spawn takes
the first one whose provider still has room.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from core import agents, limits, model_classes, orchestrator, store, usage
from core.runners import add_runner


def _iso(delta_minutes):
    return (datetime.now(timezone.utc) + timedelta(minutes=delta_minutes)).isoformat()


@pytest.fixture
def two_runners(db_path):
    claude = add_runner("claude", "/bin/true", runner_type="claude-cli")
    oc = add_runner("oc", "/bin/true", runner_type="opencode",
                    model="opencode-go/deepseek-v4-flash")
    return claude, oc


class TestProviderOf:
    def test_claude_on_its_login_spends_the_subscription(self, db_path):
        assert limits.provider_of({"type": "claude-cli"}) == "claude"

    def test_claude_on_a_key_is_billed_per_token(self, db_path):
        assert limits.provider_of({"type": "claude-cli", "auth_mode": "key"}) == "anthropic"

    def test_opencode_names_it_in_the_model_prefix(self, db_path):
        r = {"type": "opencode", "model": "openrouter/x"}
        assert limits.provider_of(r) == "openrouter"
        assert limits.provider_of(r, "opencode-go/deepseek-v4-flash") == "opencode-go"


class TestClaudeWindowsFromTheRun:
    def _event(self, status="allowed", five=0.03, week=0.48, hit="five_hour"):
        reset = int((datetime.now(timezone.utc) + timedelta(hours=2)).timestamp())
        return json.dumps({"type": "rate_limit_event", "rate_limit_info": {
            "status": status, "rateLimitType": hit, "resetsAt": reset,
            "unifiedWindows": {"five_hour": {"utilization": five, "resetsAt": reset},
                               "seven_day": {"utilization": week, "resetsAt": reset}}}})

    def test_utilisation_is_a_fraction(self, db_path):
        assert limits.observe_run("claude", "noise\n" + self._event() + "\n")
        spans = {w["span"]: w for w in limits.windows("claude")}
        assert round(spans["week"]["percent"]) == 48
        assert round(spans["5h"]["percent"]) == 3
        assert limits.blocked_until("claude") is None

    def test_a_rejected_run_marks_its_window_full(self, db_path):
        limits.observe_run("claude", self._event(status="rejected", five=0.4))
        spans = {w["span"]: w for w in limits.windows("claude")}
        assert spans["5h"]["status"] == "rejected" and spans["5h"]["percent"] == 100
        assert limits.blocked_until("claude") is not None

    def test_other_providers_are_not_read_as_claude(self, db_path):
        assert not limits.observe_run("opencode-go", self._event())
        assert limits.windows("claude") == []


class TestBlockedUntil:
    def test_a_full_window_blocks_until_it_resets(self, db_path):
        limits.record("opencode-go", "month", 99, _iso(60))
        assert limits.blocked_until("opencode-go") is not None

    def test_a_full_window_whose_reset_has_passed_is_stale(self, db_path):
        limits.record("opencode-go", "month", 99, _iso(-5))
        assert limits.blocked_until("opencode-go") is None

    def test_below_the_threshold_is_usable(self, db_path):
        limits.record("opencode-go", "week", 80, _iso(60))
        assert limits.blocked_until("opencode-go") is None

    def test_a_key_billed_provider_is_never_blocked(self, db_path):
        limits.record("openrouter", "month", 100, _iso(60))
        assert limits.blocked_until("openrouter") is None


class TestChoose:
    def test_an_empty_class_uses_the_default_runner(self, two_runners):
        c = model_classes.choose("engineer")
        assert c.source == "default" and c.runner is not None

    def test_the_first_option_with_room_wins(self, two_runners):
        claude, oc = two_runners
        model_classes.set_options("worker", [
            {"runner_id": oc["id"], "model": "opencode-go/deepseek-v4-flash"},
            {"runner_id": claude["id"], "model": "claude-haiku-4-5"}])
        c = model_classes.choose("engineer")
        assert (c.runner_name, c.model, c.provider) == ("oc", "opencode-go/deepseek-v4-flash", "opencode-go")

        limits.record("opencode-go", "5h", 100, _iso(30), "rejected")
        c = model_classes.choose("engineer")
        assert (c.runner_name, c.model) == ("claude", "claude-haiku-4-5")
        assert "opencode-go limit" in c.skipped[0][1]

    def test_every_option_spent_gives_no_runner_and_when(self, two_runners):
        claude, oc = two_runners
        model_classes.set_options("worker", [
            {"runner_id": oc["id"], "model": "opencode-go/deepseek-v4-flash"},
            {"runner_id": claude["id"], "model": ""}])
        limits.record("opencode-go", "5h", 100, _iso(30), "rejected")
        limits.record("claude", "week", 97, _iso(90))
        c = model_classes.choose("engineer")
        assert c.runner is None and c.source == "class"
        assert c.until is not None
        assert "no runner in class 'worker'" in c.why_none("worker")

    def test_the_orchestrator_reads_its_own_class(self, two_runners):
        claude, oc = two_runners
        model_classes.set_options("orchestrator", [{"runner_id": claude["id"], "model": "claude-fable-5-1"}])
        model_classes.set_options("worker", [{"runner_id": oc["id"], "model": ""}])
        assert model_classes.choose("orchestrator").model == "claude-fable-5-1"
        assert model_classes.choose("engineer").runner_name == "oc"

    def test_an_agent_can_name_the_other_class(self, two_runners):
        claude, oc = two_runners
        model_classes.set_options("orchestrator", [{"runner_id": claude["id"], "model": "claude-fable-5-1"}])
        agents.update_agent("engineer", model_class="orchestrator")
        assert model_classes.choose("engineer").model == "claude-fable-5-1"

    def test_a_pin_wins_over_the_class(self, two_runners):
        claude, oc = two_runners
        model_classes.set_options("worker", [{"runner_id": oc["id"], "model": ""}])
        agents.update_agent("engineer", runner_id=claude["id"], model="claude-haiku-4-5")
        c = model_classes.choose("engineer")
        assert (c.source, c.runner_name) == ("pin", "claude")


class TestParking:
    def _exhaust(self, two_runners):
        claude, oc = two_runners
        model_classes.set_options("worker", [{"runner_id": oc["id"], "model": "opencode-go/x"}])
        limits.record("opencode-go", "5h", 100, _iso(30), "rejected")

    def test_a_spawn_with_no_runner_parks_instead_of_running(self, two_runners, monkeypatch):
        self._exhaust(two_runners)
        spawned = []

        async def fake_spawn(*a, **kw):
            spawned.append(a)
        monkeypatch.setattr(orchestrator, "spawn_worker", fake_spawn)
        t = store.create_task("g", repo="r")
        asyncio.run(orchestrator._spawn_worker_safe(t["id"], "engineer", "x", None))

        assert spawned == []
        task = store.get_task(t["id"])
        assert (task["status"], task["pause_kind"]) == ("paused", "limits")
        kinds = [m["kind"] for m in store.get_thread(t["id"])]
        # No queued marker and no worker_finished: nothing ran, and a pinned
        # task's attempts are not spent on a run that never happened.
        assert "worker_queued" not in kinds and "worker_finished" not in kinds
        assert any("[limits]" in m["text"] and "resumes on its own" in m["text"]
                   for m in store.get_thread(t["id"]))

    def test_a_freed_window_resumes_the_task(self, two_runners, monkeypatch):
        self._exhaust(two_runners)
        emitted = []
        monkeypatch.setattr(orchestrator, "emit", lambda tid, ev: emitted.append((tid, ev)))
        t = store.create_task("g", repo="r")
        orchestrator._park_for_limits(t["id"], "engineer", "spent")

        assert limits.resume_ready() == 0          # still spent
        limits.record("opencode-go", "5h", 10, _iso(300))
        assert limits.resume_ready() == 1
        assert store.get_task(t["id"])["status"] == "active"
        assert emitted == [(t["id"], "continue")]
        assert limits.resume_ready() == 0          # parked once, resumed once

    def test_a_task_someone_moved_on_is_just_forgotten(self, two_runners, monkeypatch):
        self._exhaust(two_runners)
        monkeypatch.setattr(orchestrator, "emit", lambda *a: None)
        t = store.create_task("g", repo="r")
        orchestrator._park_for_limits(t["id"], "engineer", "spent")
        store.set_status(t["id"], "failed")
        assert limits.resume_ready() == 0
        assert store.get_task(t["id"])["status"] == "failed"


class TestBudgetsCountRealMoney:
    def _run(self, task_id, cost, provider):
        usage.record_run(task_id, "engineer", "m", {
            "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
            "cache_read": 0, "cache_write": 0, "total_tokens": 0,
            "cost": cost, "steps": 1}, provider=provider)

    def test_subscription_runs_are_left_out(self, db_path):
        t = store.create_task("g", repo="r")
        self._run(t["id"], 4.0, "claude")
        self._run(t["id"], 3.0, "opencode-go")
        self._run(t["id"], 0.5, "openrouter")
        self._run(t["id"], 0.25, "")              # a row from before the column
        assert usage.task_cost(t["id"]) == pytest.approx(0.75)
        assert usage.today_cost() == pytest.approx(0.75)


class TestRunnersPage:
    def test_the_plan_renders_and_edits(self, two_runners):
        from fastapi.testclient import TestClient
        from web.app import app
        claude, oc = two_runners
        limits.record("opencode-go", "week", 42, _iso(600))
        client = TestClient(app)

        r = client.post("/api/classes/worker/add", data={"runner_id": oc["id"], "model": "opencode-go/x"})
        assert r.status_code == 200 and "opencode-go/x" in r.text
        client.post("/api/classes/worker/add", data={"runner_id": claude["id"], "model": "claude-haiku-4-5"})
        client.post("/api/classes/worker/1/up")
        assert [o["runner_id"] for o in model_classes.get_class("worker")["options"]] == [claude["id"], oc["id"]]
        r = client.post("/api/classes/worker/0/remove")
        assert [o["runner_id"] for o in model_classes.get_class("worker")["options"]] == [oc["id"]]

        page = client.get("/api/runners/plan").text
        assert "OpenCode Go" in page and "42%" in page
        assert "Worker class" in page and "in use" in page

    def test_unpinning_an_agent_from_the_edit_form_clears_the_pin(self, two_runners):
        from fastapi.testclient import TestClient
        from web.app import app
        claude, _ = two_runners
        agents.update_agent("engineer", runner_id=claude["id"])
        TestClient(app).post("/api/agents/engineer/edit",
                             data={"persona": "p", "runner_id": "", "model": "", "model_class": ""})
        assert not agents.get_agent("engineer")["runner_id"]
