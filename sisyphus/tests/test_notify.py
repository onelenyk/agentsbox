"""Outbound notifications (core/notify.py) and the choke points that fire them.

The module exists because 8 of this daemon's 9 historical failures were silent.
So what these tests pin is exactly that promise: every path that fails or parks
a task also reports outward, a delivery failure never becomes a second incident,
and an unconfigured webhook keeps everything a no-op.
"""
import pytest

from core import notify, orchestrator, store, usage


class Recorder:
    """Stands in for notify.http_post; remembers every delivery."""

    def __init__(self, error: Exception = None):
        self.calls = []
        self.error = error

    def __call__(self, url, payload, timeout=notify.HTTP_TIMEOUT):
        self.calls.append((url, payload))
        if self.error:
            raise self.error


@pytest.fixture
def hook(monkeypatch):
    """Configure a webhook URL and capture what gets posted to it."""
    rec = Recorder()
    monkeypatch.setattr(notify, "http_post", rec)
    monkeypatch.setattr(notify, "webhook_url", lambda: "http://hook.test/x")
    return rec


# ── configuration states ────────────────────────────────────────────────


def test_unset_and_unresolved_placeholder_mean_off(monkeypatch):
    monkeypatch.setattr("core.config.get_config", lambda k, d=None: None)
    assert notify.webhook_url() is None and not notify.enabled()
    # .env without SISYPHUS_NOTIFY_URL leaves the literal ${...} in config —
    # that must read as "off", not as a URL to POST to (the auth.py rule).
    monkeypatch.setattr("core.config.get_config",
                        lambda k, d=None: "${SISYPHUS_NOTIFY_URL}")
    assert notify.webhook_url() is None

    monkeypatch.setattr("core.config.get_config",
                        lambda k, d=None: "http://hook.test/x")
    assert notify.webhook_url() == "http://hook.test/x"


def test_send_without_a_webhook_is_a_no_op(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(notify, "http_post", rec)
    monkeypatch.setattr(notify, "webhook_url", lambda: None)
    assert notify.send("task_failed", task_id="t-1", wait=True) is False
    assert rec.calls == []


# ── delivery ────────────────────────────────────────────────────────────


def test_payload_carries_event_reason_and_a_task_link(hook):
    assert notify.send("task_failed", task_id="task-ab12", title="fix the gate",
                       reason="pass budget exceeded", wait=True) is True
    (url, payload), = hook.calls
    assert url == "http://hook.test/x"
    assert payload["event"] == "task_failed"
    assert payload["task_id"] == "task-ab12"
    assert payload["title"] == "fix the gate"
    assert payload["reason"] == "pass budget exceeded"
    assert payload["url"].endswith("/tasks/task-ab12")
    assert payload["ts"]


def test_a_failed_delivery_never_raises(hook):
    hook.error = notify.NotifyError("HTTP 500")
    # The whole contract: reporting an incident must not create one.
    assert notify.send("task_failed", task_id="t-1", wait=True) is True


def test_fire_and_forget_delivers_from_a_thread(hook, monkeypatch):
    import threading
    done = threading.Event()
    real = Recorder()

    def deliver_and_signal(url, payload, timeout=notify.HTTP_TIMEOUT):
        real(url, payload, timeout)
        done.set()

    monkeypatch.setattr(notify, "http_post", deliver_and_signal)
    notify.send("task_parked", task_id="t-2")
    assert done.wait(5), "background delivery never happened"
    assert real.calls and real.calls[0][1]["event"] == "task_parked"


# ── the choke points ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_every_failed_termination_reports_outward(db_path, hook):
    t = store.create_task("goal", repo="r")
    await orchestrator._terminate(t["id"], "failed", reason="circuit breaker tripped")
    (url, payload), = hook.calls
    assert payload["event"] == "task_failed"
    assert payload["task_id"] == t["id"]
    assert payload["reason"] == "circuit breaker tripped"


@pytest.mark.asyncio
async def test_a_done_termination_stays_quiet(db_path, hook):
    t = store.create_task("goal", repo="r")
    await orchestrator._terminate(t["id"], "done")
    assert hook.calls == []


def test_parking_on_an_internal_error_reports_outward(db_path, hook):
    t = store.create_task("goal", repo="r")
    orchestrator._park_on_error(t["id"])
    (url, payload), = hook.calls
    assert payload["event"] == "task_parked"
    assert "internal error" in payload["reason"]


def test_a_terminal_task_parks_nothing_and_says_nothing(db_path, hook):
    t = store.create_task("goal", repo="r", status="done")
    orchestrator._park_on_error(t["id"])
    assert hook.calls == []


@pytest.mark.asyncio
async def test_budget_park_reports_the_cap_that_tripped(db_path, hook, monkeypatch):
    t = store.create_task("goal", repo="r")
    usage.record_run(t["id"], "engineer", "m", {
        "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
        "cache_read": 0, "cache_write": 0, "total_tokens": 0,
        "cost": 6.0, "steps": 1,
    })
    monkeypatch.setattr("core.config.get_config",
                        lambda k, d=None: {"task_budget_usd": 5.0}.get(k, d))
    decision, bg = await orchestrator.handle_event(t["id"], "worker_finished")
    assert decision.action == "ask_human"
    (url, payload), = hook.calls
    assert payload["event"] == "task_parked"
    assert "task budget" in payload["reason"]
