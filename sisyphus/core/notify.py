"""Outbound notifications — a webhook POST for the states nobody is watching.

Of the 9 failed tasks in this daemon's history, 8 failed silently: the loop
runs at 03:00, the browser poll (/api/notifications/poll) only reaches a tab
that is open, and a parked task looks exactly like a queued one until someone
goes looking. This module is the daemon's way of saying so at the moment it
happens: one POST to a configured URL per notable event.

Config (config.yaml):

    notify:
      webhook_url: ${SISYPHUS_NOTIFY_URL}   # empty/unset → notifications off

The payload is plain JSON — {event, task_id, title, reason, url, ts} — so the
receiver can be anything that accepts a POST: a Slack/Discord webhook behind a
relay, ntfy.sh, a phone push bridge, or curl in a pipe. No per-service
formatting lives here on purpose; one shape, the receiver adapts.

Delivery is fire-and-forget from a daemon thread: a notification must never
block or fail the thing it is reporting on. A failed POST is logged and
dropped — this is a signal channel, not a ledger; the thread and the logs
remain the durable record.

One indirection, ``http_post``, is the entire network surface. Tests replace
it; nothing else here knows urllib exists (the forge_poll pattern).
"""
import json
import logging
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 10

# Where the payload's task link points. Same env override the workers use for
# reaching the daemon (core/worker.py) — on the host itself the default is right.
DAEMON_URL = os.environ.get("SISYPHUS_DAEMON_URL", "http://127.0.0.1:7677")


class NotifyError(RuntimeError):
    """The webhook did not accept the notification."""


def webhook_url() -> Optional[str]:
    """The configured webhook URL, or None when notifications are off. An
    unresolved ``${VAR}`` placeholder (env var not set) counts as off."""
    from core.config import get_config
    url = get_config("notify.webhook_url")
    if not url or str(url).startswith("${"):
        return None
    return str(url)


def enabled() -> bool:
    return webhook_url() is not None


def _http_post(url: str, payload: dict, timeout: int = HTTP_TIMEOUT) -> None:
    """POST *payload* as JSON, or raise NotifyError. Never raises anything
    else — every failure mode of a webhook delivery arrives at the caller as
    the same, catchable thing."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        raise NotifyError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise NotifyError(f"unreachable: {exc.reason}") from exc
    except Exception as exc:  # noqa: BLE001 — a bad socket is a failed delivery too
        raise NotifyError(f"delivery failed: {exc}") from exc


http_post = _http_post


def _deliver(url: str, payload: dict) -> None:
    """One delivery attempt, errors logged and swallowed. Runs on the notify
    thread — nothing downstream of it may raise into the caller."""
    try:
        http_post(url, payload)
    except NotifyError as exc:
        logger.warning("notify: %s not delivered to %s — %s",
                       payload.get("event"), url, exc)


def send(event: str, task_id: str = "", title: str = "", reason: str = "",
         wait: bool = False) -> bool:
    """Report *event*. Returns whether a delivery was attempted (False =
    notifications are off). Fire-and-forget by default; ``wait=True`` delivers
    inline (startup code and tests, where the thread would outlive the check).

    Never raises: a notification failure must not become a second incident on
    top of the one it reports.
    """
    url = webhook_url()
    if not url:
        return False
    payload = {
        "event": event,
        "task_id": task_id,
        "title": title,
        "reason": reason,
        "url": f"{DAEMON_URL}/tasks/{task_id}" if task_id else DAEMON_URL,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    try:
        if wait:
            _deliver(url, payload)
        else:
            threading.Thread(target=_deliver, args=(url, payload),
                             daemon=True, name="notify").start()
    except Exception:  # noqa: BLE001 — even thread creation must not propagate
        logger.exception("notify: could not dispatch %s", event)
    return True
