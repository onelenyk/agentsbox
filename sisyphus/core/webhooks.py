"""Parse forge webhook payloads into (mr_url, summary) — the external-event
ingestion for MR lifecycle (roadmap #6).

Best-effort and defensive: an event we can't map to a merge request (or a shape
we don't recognise) returns None and is ignored. The web layer maps the URL to
a task and posts the summary as an [external] message.
"""
from typing import Optional, Tuple

Parsed = Optional[Tuple[str, str]]


def parse(platform: str, payload: dict) -> Parsed:
    if platform == "github":
        return _github(payload or {})
    if platform == "gitlab":
        return _gitlab(payload or {})
    return None


def _github(p: dict) -> Parsed:
    pr = p.get("pull_request") or {}
    url = pr.get("html_url")

    # a review was submitted
    if "review" in p and url:
        state = (p.get("review") or {}).get("state", "submitted")
        return url, f"[external] GitHub review: {state}"

    # a PR was merged
    if p.get("action") == "closed" and pr.get("merged") and url:
        return url, "[external] GitHub PR merged"

    # CI concluded (workflow_run / check_suite)
    for key in ("workflow_run", "check_suite"):
        run = p.get(key) or {}
        if run.get("conclusion") == "failure":
            prs = run.get("pull_requests") or []
            u = (prs[0].get("html_url") or prs[0].get("url")) if prs else None
            if u:
                return u, f"[external] GitHub CI failed: {run.get('name', key)}"

    # any other PR action (opened/reopened/synchronize/…)
    if p.get("action") and url:
        return url, f"[external] GitHub PR {p['action']}"
    return None


def _gitlab(p: dict) -> Parsed:
    kind = p.get("object_kind")
    oa = p.get("object_attributes") or {}
    mr = p.get("merge_request") or {}

    if kind == "merge_request" and oa.get("url"):
        return oa["url"], f"[external] GitLab MR {oa.get('action', 'updated')}"
    if kind == "pipeline":
        url = mr.get("url") or oa.get("url")
        status = oa.get("status", "")
        if url and status:
            return url, f"[external] GitLab pipeline {status}"
    if kind == "note":
        url = mr.get("url") or oa.get("url")
        if url:
            return url, "[external] GitLab comment on MR"
    return None
