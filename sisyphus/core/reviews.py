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
        f"\n"
        f"Use get_mr_diff to read the diff. Check freshness vs target and "
        f"diff quality. Then call approve(summary=...) or "
        f"request_changes(reason=...). Read-only — never commit/merge.\n"
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

