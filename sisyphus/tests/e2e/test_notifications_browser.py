"""End-to-end browser test for task notifications (Playwright).

Skipped automatically if Playwright (or its browser) isn't installed:
    pip install playwright && python -m playwright install chromium

Spins up a real daemon on a free port with an isolated DB, opens the app in a
headless Chromium with notification permission granted, inserts a done task
straight into the DB (no orchestrator, no cost), then asserts the page fired a
browser Notification with the right title/body.
"""
import datetime
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]  # …/sisyphus


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_health(base: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/api/v1/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("pw")
    db = tmp / "sisyphus.db"
    ws = tmp / "workspace"
    ws.mkdir()
    port = _free_port()
    env = {
        **__import__("os").environ,
        "SISYPHUS_DB": str(db),
        "WORKSPACE_ROOT": str(ws),
        # Without this the isolated daemon still logs into the repo's real
        # logs/sisyphus.log, where its startup lines look like the running
        # daemon restarting.
        "SISYPHUS_LOG_DIR": str(tmp / "logs"),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "web.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(REPO_ROOT), env=env,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        if not _wait_health(base):
            pytest.skip("daemon did not come up")
        yield base, str(db)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def _insert_done_task(db_path: str, goal: str) -> str:
    tid = "task-" + uuid.uuid4().hex[:8]
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn = sqlite3.connect(db_path, timeout=15)
    conn.execute(
        "INSERT INTO tasks (id, goal, repo, status, parent_id, agent, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (tid, goal, "", "done", None, None, now, now),
    )
    conn.commit()
    conn.close()
    return tid


# Installed before any page script: replace Notification with a pure recorder
# (permission forced to "granted", returns a stub). This tests OUR client
# pipeline — poll -> detect a new event -> construct a Notification with the
# right content — without depending on Chromium's (flaky, headless) permission
# and notification services.
_SPY = """
(() => {
  window.__fired = [];
  const S = function (t, o) {
    window.__fired.push({ title: t, body: (o||{}).body, tag: (o||{}).tag });
    return { onclick: null, close() {} };
  };
  Object.defineProperty(S, 'permission', { get: () => 'granted' });
  S.requestPermission = () => Promise.resolve('granted');
  window.Notification = S;
})();
"""


def test_browser_notification_fires_for_done_task(live_server):
    base, db_path = live_server
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        context.add_init_script(_SPY)
        page = context.new_page()
        page.goto(base + "/dashboard")
        page.wait_for_function("typeof notifyPoll === 'function' && typeof showTaskNotification === 'function'")

        # Clean baseline, then create a notable event and poll for it.
        page.evaluate("window._notifSeen = new Set(); window.__fired = [];")
        tid = _insert_done_task(db_path, "Playwright notif test")
        page.evaluate("notifyPoll()")
        page.wait_for_function("window.__fired.length > 0", timeout=8000)

        fired = page.evaluate("window.__fired")
        browser.close()

    assert len(fired) == 1, fired
    assert fired[0]["body"] == "Playwright notif test"
    assert "completed" in fired[0]["title"]
    assert fired[0]["tag"] == tid
