"""Production readiness assessment for repositories.

Evaluates whether a repository is configured for safe, auditable production
use: proper isolation, forge-backed delivery, check gates, and credential
security. Each item is a block (fail-fast) or warn (non-fatal).

Every repo has a ``target``: ``local`` (the default) or ``production``. The
assessment always computes every item, but Home only lists the failing ones for
a ``production`` repo; a ``local`` repo reads one calm line. The consequence
sentences below are the plain wording shared by Home, the readiness page and
the ``sisyphus.py repos readiness`` CLI — change it here and every surface
speaks the same words.
"""

from core.config import get_config
from core.repos import get_repo
from core.providers import _token
from core.repos import repo_platform

LOCAL = "local"
PRODUCTION = "production"


def target_of(repo: dict) -> str:
    """The repo's readiness target: ``local`` (default) or ``production``."""
    target = (repo.get("target") or LOCAL).strip().lower()
    return target if target in (LOCAL, PRODUCTION) else LOCAL


def assess(repo_name: str) -> dict:
    """Assess production readiness of a repo.

    Returns:
        {
            "repo": repo_name,
            "target": "local" | "production",
            "ready": bool (true when no block items fail),
            "items": [
                {
                    "key": "check" | "delivery" | ... ,
                    "ok": bool,
                    "severity": "block" | "warn",
                    "text": "one plain sentence of consequence",
                    "fix": "where to change it, as a plain sentence" (if ok=false),
                    "fix_href": "/integrations" or "/repos/<name>" when the
                                fix lives in the UI, else ""
                }
            ]
        }
    """
    repo = get_repo(repo_name)
    if not repo:
        return {
            "repo": repo_name,
            "target": LOCAL,
            "ready": False,
            "items": [{
                "key": "repo",
                "ok": False,
                "severity": "block",
                "text": f"Repository {repo_name!r} not found.",
                "fix": "Add the repository first.",
                "fix_href": "",
            }]
        }

    target = target_of(repo)
    items = []

    # Tracker: Jira must be configured for issues to become tasks
    items.append(_assess_tracker())

    platform = repo_platform(repo)

    # Check: the repo must have a check command
    check_cmd = repo.get("check", "").strip()
    items.append(_assess_check(check_cmd))

    # Delivery: a production repo should use a forge
    items.append(_assess_delivery(repo_name, platform))

    # Auto-merge: must be off for a forge repo
    auto_merge = repo.get("auto_merge", False)
    items.append(_assess_auto_merge(repo_name, auto_merge, platform))

    # Read isolation: sandbox.read_isolation must be on
    items.append(_assess_read_isolation())

    # Egress: sandbox.egress must be on
    items.append(_assess_egress())

    # Forge token: for github/gitlab repos, a token must be configured
    items.append(_assess_forge_token(platform, repo))

    # Pilot mode: warn if not read-only
    mode = repo.get("mode", "write").strip().lower()
    items.append(_assess_pilot_mode(repo_name, mode))

    # A repo is "ready" when there are no failed "block" items
    ready = all(item["ok"] or item["severity"] == "warn" for item in items)

    return {
        "repo": repo_name,
        "target": target,
        "ready": ready,
        "items": items
    }


def _assess_tracker() -> dict:
    """Jira must be configured for issues to become tasks (optional but recommended)."""
    from core import jira
    ok = jira.configured()
    return {
        "key": "tracker",
        "ok": ok,
        "severity": "warn",
        "text": "Jira is configured; issues can become tasks on their own."
                if ok else "New Jira bugs won't become tasks on their own.",
        "fix": "Open Integrations → Connections and fill in the Jira card."
               if not ok else "",
        "fix_href": "/integrations" if not ok else "",
    }


def _assess_check(check_cmd: str) -> dict:
    """A repo must have a check command that validates every MR."""
    ok = bool(check_cmd)
    return {
        "key": "check",
        "ok": ok,
        "severity": "block",
        "text": "A check command runs against every MR to gate merges."
                if ok else "Nothing verifies a merge request before it lands.",
        "fix": "Add a check: command to this repo in config.yaml "
               "(e.g. check: python -m pytest -q)."
               if not ok else "",
        "fix_href": "",
    }


def _assess_delivery(repo_name: str, platform: str) -> dict:
    """A production repo should deliver via a forge so CI and branch protection apply."""
    ok = platform in ("github", "gitlab")
    return {
        "key": "delivery",
        "ok": ok,
        "severity": "warn",
        "text": "Delivery is through a forge (github/gitlab)."
                if ok else "Merges happen locally, with no CI or branch protection.",
        "fix": "Open this repo's Edit page and set Platform to GitHub or GitLab."
               if not ok else "",
        "fix_href": f"/repos/{repo_name}" if not ok else "",
    }


def _assess_auto_merge(repo_name: str, auto_merge: bool, platform: str) -> dict:
    """Auto-merge must be off for a forge repo (human review required)."""
    # Only a block if on a forge AND auto_merge is true
    ok = not (auto_merge and platform in ("github", "gitlab"))
    return {
        "key": "auto_merge",
        "ok": ok,
        "severity": "block",
        # Say what is true: auto-merge on a local repo is allowed, not "off".
        "text": ("Auto-merge is off; MRs require a human merge." if not auto_merge
                 else "Auto-merge is on for a local repo; the forge gate does not apply."
                 if ok else "Approved MRs merge themselves without a human review."),
        "fix": "Open this repo's Edit page and turn off Auto-merge approved MRs."
               if not ok else "",
        "fix_href": f"/repos/{repo_name}" if not ok else "",
    }


def _assess_read_isolation() -> dict:
    """Workers must not read the home directory outside an allowlist."""
    cfg = get_config("sandbox", {}) or {}
    ok = cfg.get("read_isolation", False)
    return {
        "key": "read_isolation",
        "ok": ok,
        "severity": "block",
        "text": "Read isolation is enabled; workers cannot read home outside an allowlist."
                if ok else "Agents can read your whole home folder.",
        "fix": "Set sandbox.read_isolation: true in config.yaml."
               if not ok else "",
        "fix_href": "",
    }


def _assess_egress() -> dict:
    """Workers must have network limited to an allowlisted proxy."""
    cfg = get_config("sandbox", {}) or {}
    ok = cfg.get("egress", False)
    return {
        "key": "egress",
        "ok": ok,
        "severity": "block",
        "text": "Egress is controlled; worker network limited to an allowlisted proxy."
                if ok else "Agents can reach any website.",
        "fix": "Set sandbox.egress: true in config.yaml."
               if not ok else "",
        "fix_href": "",
    }


def _assess_forge_token(platform: str, repo: dict) -> dict:
    """For GitHub/GitLab repos, a token must be configured."""
    if platform not in ("github", "gitlab"):
        # Local and unknown platforms don't need a token
        return {
            "key": "forge_token",
            "ok": True,
            "severity": "block",
            "text": "No forge token needed (local or unknown platform).",
            "fix": "",
            "fix_href": "",
        }

    # Check for a per-repo token first, then fall back to global config
    repo_token = repo.get("token", "").strip() if repo else ""
    if repo_token and not repo_token.startswith("${"):
        return {
            "key": "forge_token",
            "ok": True,
            "severity": "block",
            "text": f"A {platform} token is configured (per-repo).",
            "fix": "",
            "fix_href": "",
        }

    # Fall back to global config
    global_cfg = get_config(platform, {}) or {}
    global_token = _token(global_cfg)
    if global_token:
        return {
            "key": "forge_token",
            "ok": True,
            "severity": "block",
            "text": f"A {platform} token is configured (global).",
            "fix": "",
            "fix_href": "",
        }

    return {
        "key": "forge_token",
        "ok": False,
        "severity": "block",
        "text": f"MRs to {platform} will fail until a token is set.",
        "fix": f"Open Integrations → Connections and set the {platform} token.",
        "fix_href": "/integrations",
    }


def _assess_pilot_mode(repo_name: str, mode: str) -> dict:
    """A repo should start in read-only mode for a safe pilot."""
    ok = mode == "read-only"
    return {
        "key": "pilot_mode",
        "ok": ok,
        "severity": "warn",
        "text": "Repo is in read-only mode; a safe pilot before allowing writes."
                if ok else "Agents can push changes straight to the repo.",
        "fix": "Open this repo's Edit page and tick Read-only."
               if not ok else "",
        "fix_href": f"/repos/{repo_name}" if not ok else "",
    }
