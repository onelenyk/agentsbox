"""Parse forge webhook payloads into (mr_url, summary) — the external-event
ingestion for MR lifecycle (roadmap #6).

Best-effort and defensive: an event we can't map to a merge request (or a shape
we don't recognise) returns None and is ignored. The web layer maps the URL to
a task and posts the summary as an [external] message.
"""
import hmac
import hashlib
import secrets
from typing import Optional, Tuple

from core.config import get_config

Parsed = Optional[Tuple[str, str]]


def get_webhook_secret() -> Optional[str]:
    """Get the webhook secret from config, or None if not configured."""
    secret = get_config("webhooks.secret")
    if not secret or str(secret).startswith("${"):
        return None
    return str(secret)


def verify_github_signature(body: bytes, signature_header: str) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature.

    Expected format: 'sha256=<hex>' in X-Hub-Signature-256 header.
    """
    secret = get_webhook_secret()
    if not secret:
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected_sig = hmac.new(
        secret.encode(),
        body,
        hashlib.sha256
    ).hexdigest()
    provided_sig = signature_header[7:]  # strip "sha256=" prefix
    return hmac.compare_digest(provided_sig, expected_sig)


def verify_gitlab_token(token_header: str) -> bool:
    """Verify GitLab X-Gitlab-Token header."""
    secret = get_webhook_secret()
    if not secret:
        return False
    return secrets.compare_digest(token_header or "", secret)


def verify_sisyphus_token(token_header: str) -> bool:
    """Verify generic/Jira X-Sisyphus-Token header."""
    secret = get_webhook_secret()
    if not secret:
        return False
    return secrets.compare_digest(token_header or "", secret)


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
