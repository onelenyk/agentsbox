"""How much of each subscription is left — the budget that actually binds.

A runner signed in to a subscription (Claude Code on a claude.ai plan, opencode
on OpenCode Go) is not billed per call: it spends a share of a window — five
hours, a week, a month — and stops cold when a window fills. The dollar figure
a run reports is notional there; the window is the real limit. So this keeps,
per provider and window, the last known share used and when it resets, and
answers the one question a spawn needs: is this provider usable right now?

Two sources, because the providers differ:

  claude        every Claude Code run emits a ``rate_limit_event`` with its
                five-hour and seven-day utilisation. Read from the run's own
                output after it ends (observe_run) — no extra calls, and a
                "rejected" status marks the window full the moment it happens.
  opencode-go   OpenCode Go answers GET /zen/go/v1/usage with rolling, weekly
                and monthly percentages, using the key opencode already keeps.
                Polled by poll_forever.

Anything else (an API key billed per token) has no window, and is never
reported exhausted here — the dollar budget guard covers it.
"""
import asyncio
import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from .db import get_conn

logger = logging.getLogger(__name__)

# Providers whose runs spend a subscription window rather than dollars.
SUBSCRIPTION_PROVIDERS = ("claude", "opencode-go")

# Display order and names for the windows, whichever provider reports them.
# "cooldown" is ours, not the provider's: set aside after it failed a run
# (provider_fault), so the next spawn goes to the next option in its class.
WINDOWS = {"5h": "5 hours", "week": "week", "month": "month", "cooldown": "set aside"}

DEFAULT_COOLDOWN_SECONDS = 900

# A window this full is treated as spent: the next run would likely be cut off
# part-way, which wastes the run and leaves the task half-done.
DEFAULT_THRESHOLD = 95

OPENCODE_GO_USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
DEFAULT_POLL_SECONDS = 300


def _now() -> datetime:
    return datetime.now(timezone.utc)


def init_limits_table() -> None:
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS provider_limits (
            provider    TEXT NOT NULL,
            span        TEXT NOT NULL,
            percent     REAL NOT NULL DEFAULT 0,
            resets_at   TEXT,
            status      TEXT NOT NULL DEFAULT 'ok',
            checked_at  TEXT NOT NULL,
            PRIMARY KEY (provider, span)
        )
    """)
    # Tasks parked because every runner their agent may use was at its limit.
    # The poll loop resumes them once one is usable again.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS limit_waits (
            task_id     TEXT PRIMARY KEY,
            agent       TEXT NOT NULL,
            since       TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


# ── which provider a run spends ─────────────────────────────────────────

def provider_of(runner: dict, model: str = "") -> str:
    """The provider whose limits a run on *runner* with *model* spends.

    Claude Code signed in to claude.ai spends the "claude" subscription; with
    an API key it bills per token ("anthropic"). opencode names the provider as
    the model's prefix — "opencode-go/deepseek-v4-flash" spends OpenCode Go."""
    from .runners import auth_mode
    rtype = ((runner or {}).get("type") or "opencode").strip()
    if rtype == "claude-cli":
        return "claude" if auth_mode(runner) == "login" else "anthropic"
    model = (model or (runner or {}).get("model") or "").strip()
    return model.split("/", 1)[0] if "/" in model else (model or "opencode")


def is_subscription(provider: str) -> bool:
    return provider in SUBSCRIPTION_PROVIDERS


# ── reading and writing windows ─────────────────────────────────────────

def record(provider: str, window: str, percent: float, resets_at: Optional[str],
           status: str = "ok") -> None:
    conn = get_conn()
    conn.execute(
        """INSERT INTO provider_limits (provider, span, percent, resets_at, status, checked_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(provider, span) DO UPDATE SET
             percent = excluded.percent, resets_at = excluded.resets_at,
             status = excluded.status, checked_at = excluded.checked_at""",
        (provider, window, max(0.0, min(100.0, float(percent))), resets_at, status,
         _now().isoformat()),
    )
    conn.commit()
    conn.close()


def windows(provider: str) -> list:
    """The provider's known windows, in display order."""
    conn = get_conn()
    rows = conn.execute("SELECT * FROM provider_limits WHERE provider = ?",
                        (provider,)).fetchall()
    conn.close()
    order = list(WINDOWS)
    out = [dict(r) for r in rows]
    out.sort(key=lambda w: order.index(w["span"]) if w["span"] in order else len(order))
    for w in out:
        w["label"] = WINDOWS.get(w["span"], w["span"])
    return out


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def threshold() -> float:
    from .config import get_config
    return float(get_config("limits.threshold_percent", DEFAULT_THRESHOLD) or DEFAULT_THRESHOLD)


def blocked_until(provider: str) -> Optional[datetime]:
    """When *provider* becomes usable again, or None if it is usable now.

    A window counts against it while it is at or past the threshold (or was
    rejected outright) AND its reset is still ahead — a full window whose reset
    has passed is stale, not spent. Several full windows: the latest reset is
    the one that frees it."""
    now, limit, until = _now(), threshold(), None
    for w in windows(provider):
        # A key-billed provider has no windows of its own; only a cool-down
        # after a fault can set it aside.
        if not is_subscription(provider) and w["span"] != "cooldown":
            continue
        full = w["status"] == "rejected" or w["percent"] >= limit
        resets = _parse_ts(w["resets_at"])
        if full and resets and resets > now:
            until = resets if until is None or resets > until else until
    return until


def cooldown_until(provider: str) -> Optional[datetime]:
    for w in windows(provider):
        resets = _parse_ts(w["resets_at"])
        if w["span"] == "cooldown" and resets and resets > _now():
            return resets
    return None


# ── faults: a run that never reached the model ──────────────────────────

def provider_fault(result) -> Optional[str]:
    """Why a finished run counts against its PROVIDER rather than the agent, or
    None.

    The line is "no model step was billed". Then the run never reached the
    model: a 402 from an empty balance, a lost login, a provider that hung
    before its first answer. Re-running on the same provider fails the same
    way — five tasks sat 21 hours on one DeepSeek 402 — so the next run should
    go to the next option in the class instead. A run that took real steps and
    then went quiet is left alone: from the runner's output a model stuck
    mid-answer cannot be told from a long silent command.
    """
    status = getattr(result, "status", "") or ""
    if status == "cancelled":
        return None
    steps = ((getattr(result, "usage", None) or {}).get("steps") or 0)
    if steps:
        return None
    error = (getattr(result, "error", "") or "").strip()
    if status == "timeout":
        return f"no answer before the timeout ({error or 'timed out'})"
    stdout = (getattr(result, "raw_stdout", "") or "").strip()
    stderr = (getattr(result, "raw_stderr", "") or "").strip()
    return f"no model step billed ({(error or stderr or stdout)[:160] or 'no output'})"


def cooldown(provider: str, reason: str) -> Optional[datetime]:
    """Set *provider* aside for a while after a fault. Returns until when."""
    from datetime import timedelta
    from .config import get_config
    secs = int(get_config("limits.fault_cooldown_seconds", DEFAULT_COOLDOWN_SECONDS) or 0)
    if not provider or secs <= 0:
        return None
    until = _now() + timedelta(seconds=secs)
    record(provider, "cooldown", 100, until.isoformat(), "rejected")
    logger.warning("limits: %s set aside until %s — %s", provider, until.isoformat(), reason)
    return until


# ── claude: from each run's own output ──────────────────────────────────

_CLAUDE_WINDOWS = {"five_hour": "5h", "seven_day": "week"}


def _epoch_iso(value) -> Optional[str]:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def observe_run(provider: str, stdout: str) -> bool:
    """Record the windows a Claude Code run reported. Returns whether any were
    found. The last event wins: a long run reports more than once, and the
    latest is the truest."""
    if provider != "claude":
        return False
    info = None
    for line in (stdout or "").splitlines():
        line = line.strip()
        if '"rate_limit_event"' not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "rate_limit_event":
            info = obj.get("rate_limit_info") or info
    if not isinstance(info, dict):
        return False
    rejected = info.get("status") == "rejected"
    hit = _CLAUDE_WINDOWS.get(info.get("rateLimitType") or "")
    seen = False
    for key, window in _CLAUDE_WINDOWS.items():
        w = (info.get("unifiedWindows") or {}).get(key)
        if not isinstance(w, dict):
            continue
        # Utilisation arrives as a fraction (0.48 is 48%).
        pct = float(w.get("utilization") or 0) * 100
        status = "rejected" if rejected and window == hit else "ok"
        record("claude", window, 100 if status == "rejected" else pct,
               _epoch_iso(w.get("resetsAt")), status)
        seen = True
    if rejected and hit and not seen:
        record("claude", hit, 100, _epoch_iso(info.get("resetsAt")), "rejected")
        seen = True
    return seen


# ── opencode-go: polled ─────────────────────────────────────────────────

_GO_WINDOWS = {"rolling": "5h", "weekly": "week", "monthly": "month"}


def _opencode_go_key() -> str:
    from .runner_catalog import OPENCODE_AUTH_PATH
    try:
        creds = json.loads(OPENCODE_AUTH_PATH.read_text())
    except Exception:  # noqa: BLE001
        return ""
    entry = creds.get("opencode-go") or {}
    return (entry.get("key") or entry.get("access") or "") if isinstance(entry, dict) else ""


def poll_opencode_go() -> Optional[str]:
    """Fetch and record OpenCode Go's windows. Returns an error, or None."""
    key = _opencode_go_key()
    if not key:
        return "not signed in to OpenCode Go"
    # Python's default User-Agent is refused (403) by the site's edge; curl's
    # identical request is not.
    req = urllib.request.Request(OPENCODE_GO_USAGE_URL,
                                 headers={"Authorization": f"Bearer {key}",
                                          "User-Agent": "sisyphus-limits/1",
                                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
    except (urllib.error.URLError, ValueError, OSError) as exc:
        return f"usage request failed: {exc}"
    usage = (body or {}).get("usage") or {}
    for key_name, window in _GO_WINDOWS.items():
        w = usage.get(key_name)
        if isinstance(w, dict):
            status = "ok" if (w.get("status") or "ok") == "ok" else "rejected"
            record("opencode-go", window, float(w.get("percent") or 0),
                   w.get("resetsAt"), status)
    return None


def providers_in_use() -> list:
    """Providers to show: subscriptions some runner or class option spends,
    and any provider currently set aside after a fault."""
    from . import model_classes
    from .runners import list_runners
    runners = {r["id"]: r for r in list_runners() if r.get("enabled", 1)}
    found = set()
    for r in runners.values():
        found.add(provider_of(r))
    for cls in model_classes.list_classes():
        for opt in cls["options"]:
            r = runners.get(opt.get("runner_id"))
            if r:
                found.add(provider_of(r, opt.get("model", "")))
    shown = [p for p in SUBSCRIPTION_PROVIDERS if p in found]
    return shown + sorted(p for p in found if p not in shown and cooldown_until(p))


# ── parking and resuming ────────────────────────────────────────────────

def park(task_id: str, agent: str) -> None:
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO limit_waits (task_id, agent, since) VALUES (?, ?, ?)",
                 (task_id, agent, _now().isoformat()))
    conn.commit()
    conn.close()


def resume_ready() -> int:
    """Resume every task paused on limits whose agent has a usable runner
    again."""
    from . import model_classes, orchestrator, store
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("SELECT * FROM limit_waits").fetchall()]
    conn.close()
    resumed = 0
    for row in rows:
        task = store.get_task(row["task_id"])
        still_parked = bool(task and task["status"] == "paused"
                            and task.get("pause_kind") == "limits")
        if still_parked and model_classes.choose(row["agent"]).runner is None:
            continue
        conn = get_conn()
        conn.execute("DELETE FROM limit_waits WHERE task_id = ?", (row["task_id"],))
        conn.commit()
        conn.close()
        # Someone moved it on already (Resume, a reply, a stop) — just forget it.
        if still_parked and orchestrator.resume(row["task_id"], "a runner is available again"):
            resumed += 1
    return resumed


async def poll_forever() -> None:
    """Daemon loop: refresh polled providers, then resume tasks a freed window
    lets through."""
    from . import heartbeat
    from .config import get_config
    interval = int(get_config("limits.poll_seconds", DEFAULT_POLL_SECONDS) or 0)
    heartbeat.register("limits", every=interval, note="watching subscription limits")
    if interval <= 0:
        heartbeat.mark_off("limits", "limits.poll_seconds is 0 — polling off")
        return
    while True:
        try:
            notes = []
            if "opencode-go" in await asyncio.to_thread(providers_in_use):
                err = await asyncio.to_thread(poll_opencode_go)
                notes.append(f"opencode-go: {err}" if err else "opencode-go checked")
            resumed = await asyncio.to_thread(resume_ready)
            if resumed:
                notes.append(f"resumed {resumed} task(s)")
            heartbeat.beat("limits", note=", ".join(notes) or "nothing to poll")
        except Exception:  # noqa: BLE001
            logger.exception("limits: pass failed")
        await asyncio.sleep(interval)


# ── what the Runners page shows ─────────────────────────────────────────

PROVIDER_LABELS = {
    "claude": ("Claude subscription", "Read from each Claude run — updates after the next one"),
    "opencode-go": ("OpenCode Go", f"Asked every {DEFAULT_POLL_SECONDS // 60} minutes"),
}


def tone(percent: float, status: str = "ok") -> str:
    if status == "rejected" or percent >= threshold():
        return "bad"
    return "warn" if percent >= 70 else "good"


def view() -> list:
    """One entry per subscription in use: its windows, and whether it is
    usable now."""
    out = []
    for provider in providers_in_use():
        label, source = PROVIDER_LABELS.get(provider, (provider, ""))
        # A cool-down that has run out is history, not a window.
        spans = [w for w in windows(provider)
                 if w["span"] != "cooldown" or cooldown_until(provider)]
        for w in spans:
            w["tone"] = tone(w["percent"], w["status"])
        until = blocked_until(provider)
        out.append({
            "provider": provider, "label": label, "source": source,
            "windows": spans, "polled": provider == "opencode-go",
            "checked_at": max((w["checked_at"] for w in spans), default=""),
            "blocked_until": until.isoformat() if until else "",
        })
    return out
