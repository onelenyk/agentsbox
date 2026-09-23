"""Ask each forge what happened to the pull requests Sisyphus opened.

Sisyphus already knows how to *react* to a forge event. ``web.api.receive_webhook``
does three things with one: map the payload's PR/MR URL back to the task that
opened it, post an ``[external]`` message on that task's thread, re-open the task
and ``emit`` so the orchestrator picks it up. That path is complete and tested —
and unreachable, because a webhook requires the forge to open an *inbound*
connection and this daemon listens on 127.0.0.1 on a laptop. So an agent opens a
PR and never learns that a colleague approved it, requested changes, or merged it.

This module dials out instead, on a timer, and hands what it finds to those same
three lines. Nothing here re-implements the reaction; ``_deliver`` is deliberately
a transcription of ``receive_webhook``'s MR-lifecycle block, so a review that
arrives by polling lands in a thread indistinguishably from one that arrives by
webhook — including the ``[external]`` prefix, which core.webhooks puts on every
summary it produces.

Two invariants this file exists to hold:

* **Read-only.** Every request is a GET. Nothing is created, updated, approved,
  commented or merged on the forge, ever.
* **A failed read is not "unchanged".** A timeout, a 500 or a rate-limit leaves
  the stored baseline untouched and is reported as an error, so the next tick
  re-reads and the merge we could not see is announced late rather than never.
"""
import asyncio
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL = 300
# A floor, enforced over config. Polling is the whole cost of this feature: each
# open PR costs one to three API calls per tick, against a budget of 5000/hour
# (GitHub, authenticated). At 60s with ten open PRs that is already 1800/hour;
# anything faster is a way to lose the token's quota for the rest of the hour and
# get *nothing* — not even the webhook-shaped events this exists to deliver.
MIN_INTERVAL = 60
HTTP_TIMEOUT = 20


class ForgeError(RuntimeError):
    """The forge did not give us a state we can trust.

    Carries ``retry_after`` when the forge told us how long to stay away
    (Retry-After, or X-RateLimit-Reset on GitHub), so the loop can honour it
    instead of hammering a limit it has already hit.
    """

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


# ── HTTP ────────────────────────────────────────────────────────────────
#
# One indirection, `http_get`, is the entire network surface of this module.
# Tests replace it; nothing else here knows urllib exists.


def _http_get(url: str, headers: dict, timeout: int = HTTP_TIMEOUT):
    """GET *url* and parse JSON (an object or an array), or raise ForgeError.
    Never raises anything else — every failure mode of a forge read has to arrive
    at the caller as the same, catchable thing."""
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as exc:
        raise ForgeError(f"HTTP {exc.code} {_body_snippet(exc)}",
                         retry_after=_retry_after(exc)) from exc
    except urllib.error.URLError as exc:
        raise ForgeError(f"unreachable: {exc.reason}") from exc
    except Exception as exc:  # noqa: BLE001 — a malformed body is a failed read too
        raise ForgeError(f"bad response: {exc}") from exc


http_get = _http_get


def _body_snippet(exc) -> str:
    try:
        return exc.read().decode("utf-8", "replace")[:160]
    except Exception:  # noqa: BLE001
        return ""


def _retry_after(exc) -> Optional[float]:
    """Seconds the forge asked us to wait, from whichever header it used.

    GitHub answers an exhausted quota with 403 and X-RateLimit-Remaining: 0 plus
    an absolute X-RateLimit-Reset; secondary limits and GitLab use Retry-After.
    Both are honoured because a 403 that we retry every tick is how a token gets
    the *next* hour blocked as well.
    """
    headers = getattr(exc, "headers", None) or {}
    try:
        ra = headers.get("Retry-After")
        if ra:
            return max(0.0, float(ra))
        if str(headers.get("X-RateLimit-Remaining", "")).strip() == "0":
            reset = float(headers.get("X-RateLimit-Reset") or 0)
            return max(0.0, reset - time.time()) if reset else 900.0
    except (TypeError, ValueError):
        return None
    return None


# Host → monotonic deadline. A rate limit is a property of the token against the
# host, not of one PR, so parking the host skips the other nine PRs that would
# each earn another 403.
_cooldown_until: Dict[str, float] = {}

# Repos we have already complained about. The credential is missing every tick
# until a human sets a variable, and a log line per repo per tick would bury the
# one line that says which variable.
_missing_cred_logged: set = set()


def reset_state() -> None:
    """Clear process-local cooldowns and log de-duplication (tests, and a
    deliberate 'try again now' from an operator)."""
    _cooldown_until.clear()
    _missing_cred_logged.clear()


# ── Config ──────────────────────────────────────────────────────────────


def poll_enabled() -> bool:
    from core.config import get_config
    return bool(get_config("forge_poll.enabled", True))


def poll_interval() -> int:
    from core.config import get_config
    try:
        secs = int(get_config("forge_poll.interval_seconds", DEFAULT_INTERVAL) or DEFAULT_INTERVAL)
    except (TypeError, ValueError):
        logger.warning("forge_poll: interval_seconds is not a number — using %ds", DEFAULT_INTERVAL)
        return DEFAULT_INTERVAL
    if secs < MIN_INTERVAL:
        logger.warning("forge_poll: interval_seconds=%d is below the %ds floor — using %ds "
                       "(faster polling only spends API quota)", secs, MIN_INTERVAL, MIN_INTERVAL)
        return MIN_INTERVAL
    return secs


def _api_base(platform: str) -> str:
    """The forge's API root, honouring a self-hosted base_url from config.

    Mirrors core.providers: an unresolved ``${VAR}`` placeholder means the
    variable is unset, not that the API lives at a literal '${...}' address.
    """
    from core.config import get_config
    default = "https://api.github.com" if platform == "github" else "https://gitlab.com"
    base = str((get_config(platform, {}) or {}).get("base_url") or "").strip()
    if not base or base.startswith("${"):
        base = default
    return base.rstrip("/")


# ── Which PR are we asking about ────────────────────────────────────────

_GITHUB_PR = re.compile(r"^https?://[^/]+/(?P<slug>[^\s?#]+?)/pull/(?P<number>\d+)")
_GITLAB_MR = re.compile(r"^https?://[^/]+/(?P<slug>[^\s?#]+?)/(?:-/)?merge_requests/(?P<number>\d+)")


def parse_forge_url(url: str) -> Optional[Tuple[str, str, int]]:
    """(platform, project slug, number) from a PR/MR web URL, or None.

    Derived from the URL rather than from the repo's clone source because the URL
    is what we actually recorded and what a webhook would have matched — a repo
    registered by filesystem path has no owner/name in its source at all.
    ``/-/`` is optional: GitLab links minted before 13.x omit it and still work.
    """
    u = (url or "").strip()
    for platform, pattern in (("github", _GITHUB_PR), ("gitlab", _GITLAB_MR)):
        m = pattern.match(u)
        if m:
            return platform, m.group("slug").strip("/"), int(m.group("number"))
    return None


def forge_url_for(mr: dict) -> Optional[str]:
    """This MR's PR/MR URL: the stored column, else recovered from the task's
    thread and stored.

    The thread is a real source, not a guess: ``open_mr`` posts the forge URL as
    the ``mr``-kind message, and ``store.find_task_by_mr`` maps that same text
    back to a task for every incoming webhook. Recovering from it means MRs
    opened before this column existed are still pollable.
    """
    from core import mergerequests, store

    stored = (mr.get("forge_url") or "").strip()
    if stored:
        return stored
    for msg in store.get_thread(mr.get("task_id") or ""):
        if msg.get("kind") != "mr":
            continue
        for word in re.split(r"\s+", msg.get("text") or ""):
            candidate = word.strip().rstrip(".,);]")
            if parse_forge_url(candidate):
                mergerequests.set_forge_url(mr["id"], candidate)
                return candidate
    return None


# ── Reading state off a forge ───────────────────────────────────────────
#
# Both readers return the same shape, so diffing is platform-independent:
#   state      open | closed | merged | locked
#   merge      clean | dirty | blocked | ...  (forge vocabulary, compared as text)
#   updated_at the forge's own change stamp
#   comments   total comment count
#   approvals  number of approvals
#   review     the most recent review verdict, when the forge reports one


def _empty_state() -> dict:
    return {"state": "", "merge": "", "updated_at": "", "comments": 0,
            "approvals": 0, "review": ""}


def _github_state(slug: str, number: int, token: str, previous: Optional[dict]) -> dict:
    api = _api_base("github")
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "sisyphus-forge-poll"}
    pr = http_get(f"{api}/repos/{slug}/pulls/{number}", headers)

    state = _empty_state()
    # 'merged' is not a value of GitHub's `state` field — a merged PR is
    # state=closed, merged=true. Collapsing them here is what lets the diff say
    # "merged" instead of "closed", which is the difference between a task that
    # can stop and one that has to find out why it was rejected.
    state["state"] = "merged" if pr.get("merged") else str(pr.get("state") or "")
    state["merge"] = str(pr.get("mergeable_state") or "")
    state["updated_at"] = str(pr.get("updated_at") or "")
    state["comments"] = int(pr.get("comments") or 0) + int(pr.get("review_comments") or 0)

    # Reviews are a second request, and the only thing that answers "did anyone
    # approve?". Skipping it while updated_at is unchanged is safe *and* is what
    # keeps the steady-state cost at one call per PR per tick: GitHub bumps a
    # PR's updated_at when a review is submitted or a comment is added.
    if previous is not None and state["updated_at"] and state["updated_at"] == previous.get("updated_at"):
        state["approvals"] = int(previous.get("approvals") or 0)
        state["review"] = previous.get("review") or ""
        return state

    reviews = http_get(f"{api}/repos/{slug}/pulls/{number}/reviews?per_page=100", headers)
    if isinstance(reviews, list):
        # One reviewer may submit many times; only their latest verdict counts,
        # exactly as GitHub's own merge gate reads them.
        latest: Dict[str, str] = {}
        for rv in reviews:
            who = str(((rv or {}).get("user") or {}).get("login") or "")
            verdict = str((rv or {}).get("state") or "").upper()
            if verdict in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                latest[who] = verdict
        state["approvals"] = sum(1 for v in latest.values() if v == "APPROVED")
        if reviews:
            state["review"] = str((reviews[-1] or {}).get("state") or "").lower()
    return state


def _gitlab_state(slug: str, number: int, token: str, previous: Optional[dict]) -> dict:
    api = _api_base("gitlab")
    headers = {"PRIVATE-TOKEN": token, "User-Agent": "sisyphus-forge-poll"}
    project = urllib.parse.quote(slug, safe="")
    mr = http_get(f"{api}/api/v4/projects/{project}/merge_requests/{number}", headers)

    state = _empty_state()
    # GitLab says 'opened'; the rest of Sisyphus (and GitHub) says 'open'.
    raw = str(mr.get("state") or "")
    state["state"] = "open" if raw == "opened" else raw
    state["merge"] = str(mr.get("detailed_merge_status") or mr.get("merge_status") or "")
    state["updated_at"] = str(mr.get("updated_at") or "")
    state["comments"] = int(mr.get("user_notes_count") or 0)

    if previous is not None and state["updated_at"] and state["updated_at"] == previous.get("updated_at"):
        state["approvals"] = int(previous.get("approvals") or 0)
        state["review"] = previous.get("review") or ""
        return state

    # Approvals are a separate endpoint and a paid feature on some tiers: a 404
    # here means "this project has no approval rules", not "the poll failed", so
    # it must not discard the MR state we just read successfully.
    try:
        appr = http_get(f"{api}/api/v4/projects/{project}/merge_requests/{number}/approvals",
                        headers)
        approved_by = appr.get("approved_by") or []
        state["approvals"] = len(approved_by) if isinstance(approved_by, list) else 0
    except ForgeError as exc:
        logger.debug("forge_poll: no approvals for %s!%s (%s)", slug, number, exc)
    return state


def read_state(platform: str, slug: str, number: int, token: str,
               previous: Optional[dict]) -> dict:
    if platform == "github":
        return _github_state(slug, number, token, previous)
    if platform == "gitlab":
        return _gitlab_state(slug, number, token, previous)
    raise ForgeError(f"no reader for platform {platform!r}")


# ── What changed ────────────────────────────────────────────────────────

_CONFLICTED = ("dirty", "cannot_be_merged", "conflict", "broken_status")


def diff_state(platform: str, previous: Optional[dict], current: dict) -> List[str]:
    """The ``[external]`` lines a webhook would have produced for this change.

    Wording deliberately matches core.webhooks' summaries: a thread should not
    reveal which transport delivered the news, and a reader who learned to
    recognise "[external] GitHub PR merged" should not have to learn a second
    phrasing for the same event.

    A first observation (``previous is None``) reports only a PR that is already
    finished. Announcing the full current state of every open PR the first time
    we look would post a burst of stale non-news to every thread — an approval
    from three weeks ago is not an event. A PR that is already merged or closed
    is different: nobody ever told the task, and it is the one fact that lets the
    task stop waiting.
    """
    label = "GitHub" if platform == "github" else "GitLab"
    noun = "PR" if platform == "github" else "MR"
    out: List[str] = []

    if previous is None:
        if current.get("state") in ("merged", "closed", "locked"):
            out.append(f"[external] {label} {noun} {current['state']}")
        return out

    if current.get("state") and current["state"] != previous.get("state"):
        if current["state"] == "open" and previous.get("state") in ("closed", "locked"):
            out.append(f"[external] {label} {noun} reopened")
        else:
            out.append(f"[external] {label} {noun} {current['state']}")

    approvals, was = int(current.get("approvals") or 0), int(previous.get("approvals") or 0)
    if approvals > was:
        out.append(f"[external] {label} review: approved ({approvals} approval(s))")

    review = (current.get("review") or "").lower()
    if review == "changes_requested" and review != (previous.get("review") or "").lower():
        out.append(f"[external] {label} review: changes_requested")

    new_comments = int(current.get("comments") or 0) - int(previous.get("comments") or 0)
    if new_comments > 0:
        out.append(f"[external] {label} {noun}: {new_comments} new comment(s)")

    merge = (current.get("merge") or "").lower()
    if merge in _CONFLICTED and merge != (previous.get("merge") or "").lower():
        out.append(f"[external] {label} {noun} has merge conflicts with the target branch")

    return out


# ── Delivery — a transcription of web.api.receive_webhook ───────────────


def _deliver(mr: dict, url: str, summaries: List[str]) -> Optional[str]:
    """Post the summaries to the owning task's thread and wake it.

    Line for line what receive_webhook does with a parsed payload. It resolves
    the task from the URL via ``store.find_task_by_mr`` — the mapping a webhook
    uses — and falls back to the MR row's own task_id, which cannot be wrong but
    which a webhook never has.
    """
    from core import store
    from core.orchestrator import emit

    task = store.find_task_by_mr(url) or store.get_task(mr.get("task_id") or "")
    if not task:
        return None
    for text in summaries:
        store.post_message(task["id"], "system", kind="external", text=text)
    if task["status"] in ("done", "failed", "waiting", "paused"):
        store.set_status(task["id"], "active")
    emit(task["id"], "external")
    return task["id"]


def _close_locally(mr: dict, state: dict) -> None:
    """Mirror a terminal forge state onto our own row, so a merged PR stops being
    polled forever. Local bookkeeping only — nothing is sent to the forge."""
    from core import mergerequests

    if state.get("state") == "merged":
        mergerequests.set_status(mr["id"], "merged")
    elif state.get("state") == "closed":
        mergerequests.set_status(mr["id"], "closed")


# ── One pass ────────────────────────────────────────────────────────────


def poll_once() -> dict:
    """Ask every forge about every open MR we opened there, once.

    Returns {checked, changed, events, skipped, errors}. ``skipped`` and
    ``errors`` are populated rather than merely logged so a caller — a status
    page, a test, an operator — can tell "we looked and nothing had changed"
    apart from "we never managed to look".
    """
    from core import mergerequests
    from core.repos import repo_credential

    summary: dict = {"checked": 0, "changed": 0, "events": [], "skipped": [], "errors": []}

    for mr in mergerequests.list_mrs(status="open"):
        repo = mr.get("repo") or ""
        try:
            cred = repo_credential(repo)
        except Exception as exc:  # noqa: BLE001 — a broken repo entry must not stop the pass
            summary["errors"].append({"mr": mr["id"], "error": f"credential lookup failed: {exc}"})
            continue

        if (cred.get("platform") or "") not in ("github", "gitlab"):
            continue                       # local repo — Sisyphus is its forge

        url = forge_url_for(mr)
        if not url:
            summary["skipped"].append({"mr": mr["id"], "repo": repo,
                                       "reason": "no forge URL recorded for this MR"})
            continue
        parsed = parse_forge_url(url)
        if not parsed:
            summary["skipped"].append({"mr": mr["id"], "repo": repo,
                                       "reason": f"unrecognised forge URL {url}"})
            continue
        platform, slug, number = parsed

        if not cred.get("token"):
            hint = cred.get("env_hint") or ""
            reason = (f"no {platform} token for repo {repo!r}"
                      + (f" — set {hint}, or a token: on the repo in config.yaml" if hint else ""))
            summary["skipped"].append({"mr": mr["id"], "repo": repo, "reason": reason,
                                       "env_hint": hint})
            if repo not in _missing_cred_logged:
                _missing_cred_logged.add(repo)
                logger.warning("forge_poll: %s — %s is not being polled", reason, url)
            continue
        _missing_cred_logged.discard(repo)

        api = _api_base(platform)
        cooldown = _cooldown_until.get(api, 0.0) - time.monotonic()
        if cooldown > 0:
            summary["errors"].append({"mr": mr["id"], "url": url,
                                      "error": f"{platform} rate-limited; retrying in {int(cooldown)}s"})
            continue

        previous = mergerequests.get_forge_state(mr["id"])
        try:
            current = read_state(platform, slug, number, cred["token"], previous)
        except ForgeError as exc:
            if exc.retry_after:
                _cooldown_until[api] = time.monotonic() + exc.retry_after
            # The baseline is deliberately left untouched: next tick re-reads and
            # reports whatever we could not see now.
            summary["errors"].append({"mr": mr["id"], "url": url, "error": str(exc)})
            logger.warning("forge_poll: %s: %s", url, exc)
            continue
        except Exception as exc:  # noqa: BLE001 — one bad MR must not end the pass
            summary["errors"].append({"mr": mr["id"], "url": url, "error": f"unexpected: {exc}"})
            logger.exception("forge_poll: unexpected failure polling %s", url)
            continue

        summary["checked"] += 1
        changes = diff_state(platform, previous, current)
        # Persist before delivering. A crash between the two costs one missed
        # notification; the other order would replay the same [external] lines
        # into the thread on every tick until delivery finally succeeded.
        mergerequests.save_forge_state(mr["id"], current)
        if not changes:
            continue

        summary["changed"] += 1
        task_id = _deliver(mr, url, changes)
        summary["events"].append({"mr": mr["id"], "url": url, "task_id": task_id,
                                  "summaries": changes})
        logger.info("forge_poll: %s → task %s: %s", url, task_id, "; ".join(changes))
        _close_locally(mr, current)

    return summary


async def poll_forever() -> None:
    """Daemon background loop: one pass every forge_poll.interval_seconds.

    Started with ``asyncio.create_task`` at app startup, next to
    ``autopilot.run_forever``. The pass itself runs in a worker thread: it makes
    blocking HTTP calls with a 20s timeout, and the loop it would otherwise block
    is the same one running ``orchestrator.dispatch_forever`` — an unreachable
    forge would stall task dispatch for the whole timeout.

    No exception escapes. A loop that dies on one bad response is worse than
    useless: the daemon keeps running and silently stops noticing anything.
    """
    from core import heartbeat
    heartbeat.register("forge poll", every=poll_interval(), note="tracking PRs")
    if not poll_enabled():
        heartbeat.mark_off("forge poll", "forge_poll.enabled is false")
        logger.info("forge_poll: disabled (forge_poll.enabled) — PRs will not be tracked")
        return
    interval = poll_interval()
    logger.info("forge_poll: polling forges every %ds", interval)
    while True:
        try:
            summary = await asyncio.to_thread(poll_once)
            heartbeat.beat("forge poll",
                           note=(f"checked {summary['checked']}"
                                 + (f" · {len(summary['errors'])} errors"
                                    if summary["errors"] else "")))
            if summary["changed"] or summary["errors"]:
                logger.info("forge_poll: checked=%d changed=%d skipped=%d errors=%d",
                            summary["checked"], summary["changed"],
                            len(summary["skipped"]), len(summary["errors"]))
        except Exception:  # noqa: BLE001
            logger.exception("forge_poll: pass failed")
        await asyncio.sleep(interval)
