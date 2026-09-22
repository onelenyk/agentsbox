"""Auto-review hook — spawn a dedicated mr-reviewer task for every opened MR,
and surface the verdict on the MR itself. Read-only to the codebase — this
module only writes to the task+MR store.
"""
import re
import logging
from typing import Optional

from . import store

logger = logging.getLogger(__name__)

_MARKER_PAT = re.compile(r"^Review-of-MR:\s*(\S+)\s*$", re.MULTILINE)
_ORIGIN_PAT = re.compile(r"^Origin-task:\s*(\S+)\s*$", re.MULTILINE)


def ensure_review_task(mr: dict) -> Optional[dict]:
    """Create a review task for *mr* if one isn't already pending. Returns the
    created task dict, or None when a duplicate is found."""
    mr_id = mr["id"]

    # Dedup: a non-terminal review task for this MR already exists?
    for t in store.list_tasks():
        if t["status"] not in ("active", "waiting"):
            continue
        if t.get("agent") != "mr-reviewer":
            continue
        if _MARKER_PAT.search(t.get("goal", "")):
            # The marker line must reference THIS mr_id.
            if f"Review-of-MR: {mr_id}" in t["goal"]:
                return None

    # Run the repo's own check before the reviewer is asked anything, so the
    # verdict it must account for is already on the record and was produced by
    # the daemon rather than typed by an agent.
    check_line = ""
    try:
        from core import verify
        res = verify.run_and_record(mr)
        check_line = verify.summarise(res)
    except Exception:  # noqa: BLE001 — a broken check must not block review
        logger.exception("check failed to run for %s", mr_id)
        check_line = "The automated check could not be run — treat it as absent, not as a pass."

    # The baseline is computed here rather than described, so the reviewer has
    # the SHA instead of a rule it has to apply correctly.
    try:
        from core import mergerequests
        rng = mergerequests.diff_range(mr)
    except Exception:  # noqa: BLE001
        rng = {"base": "", "head": ""}
    base_line = (
        f"Baseline commit: {rng['base']}\n" if rng.get("base")
        else "Baseline commit: could not be resolved — say so rather than guessing one.\n"
    )

    mr_title = mr.get("title", "") or "Untitled"
    max_title_len = 120
    if len(mr_title) > max_title_len:
        mr_title = mr_title[:max_title_len - 1] + "…"

    goal = (
        f"Review-of-MR: {mr_id}\n"
        f"Origin-task: {mr.get('task_id', '')}\n"
        f"\n"
        f"Review THIS one merge request only.\n"
        f"Source branch: {mr.get('source_branch', '?')}\n"
        f"Target branch: {mr.get('target_branch', '?')}\n"
        f"{base_line}"
        f"Any git command you run yourself must compare against that baseline "
        f"commit, never HEAD~1 — HEAD~1 is only the previous commit on this "
        f"branch and hides anything changed earlier on it.\n"
        f"\n"
        f"Use get_mr_diff to read the diff. Check freshness vs target and "
        f"diff quality. Then call approve(summary=...) or "
        f"request_changes(reason=...). Read-only — never commit/merge.\n"
        f"\n"
        f"Automated check (run by the daemon, not by you): {check_line}\n"
        f"get_mr_diff returns that verdict in full — status, the exact command, "
        f"the commit it ran against and the tail of its output. This is the "
        f"authoritative result for this branch: do not substitute your own test "
        f"run for it, and do not approve over a failing check. Your own worktree "
        f"is sandboxed and fails some tests for environmental reasons, so where "
        f"your run and the record disagree ABOUT THE SAME COMMIT, the record "
        f"wins — report the discrepancy in your verdict instead of asking a "
        f"human to adjudicate it. If no check ran, or it ran against an older "
        f"commit, say so rather than implying the branch was verified.\n"
    )

    return store.create_task(
        goal=goal,
        repo=mr.get("repo", ""),
        agent="mr-reviewer",
        title=f"Review {mr_id}: {mr_title}",
    )


def parse_review_marker(goal: str) -> Optional[dict]:
    """Parse the review-marker header block from *goal*. Returns
    {mr_id, origin_task} or None if not a review task."""
    m = _MARKER_PAT.search(goal)
    if not m:
        return None
    o = _ORIGIN_PAT.search(goal)
    return {"mr_id": m.group(1).strip(), "origin_task": o.group(1).strip() if o else ""}


def record_review_death(goal: str, reason: str) -> None:
    """A review-of-MR task failed without ever delivering a verdict. Record the
    death on the MR row (so the gate and the badge read it instead of a forever
    'pending') and post a note on the origin task's thread, which is what the
    rest of this module does for an approve/request_changes verdict."""
    marker = parse_review_marker(goal)
    if not marker:
        return
    if marker["mr_id"]:
        try:
            from . import mergerequests
            mergerequests.set_review_status(marker["mr_id"], "died")
        except Exception:  # noqa: BLE001 — the origin note must still be sent
            logger.exception("could not record review death for %s", marker["mr_id"])
    if marker["origin_task"]:
        store.post_message(
            marker["origin_task"], author="system", kind="note",
            text=(f"the review of {marker['mr_id']} died — its reviewer "
                  f"ended without a verdict: {reason}"),
        )
        # …and wake it: a death is a verdict too, and the origin task may be
        # waiting on nothing else.
        try:
            from .orchestrator import emit
            emit(marker["origin_task"], "external")
        except Exception:  # noqa: BLE001
            logger.exception("could not wake origin task %s after a review death", marker["origin_task"])

