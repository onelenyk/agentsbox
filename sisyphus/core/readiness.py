"""Production readiness assessment for repositories.

Evaluates whether a repository is configured for safe, auditable production
use: proper isolation, forge-backed delivery, check gates, and credential
security. Each item is a block (fail-fast) or warn (non-fatal).
"""

from core.config import get_config
from core.repos import get_repo
from core.providers import detect_platform, _token


def assess(repo_name: str) -> dict:
    """Assess production readiness of a repo.

    Returns:
        {
            "repo": repo_name,
            "ready": bool (true when no block items fail),
            "items": [
                {
                    "key": "check" | "delivery" | ... ,
                    "ok": bool,
                    "severity": "block" | "warn",
                    "text": "one plain sentence",
                    "fix": "one plain sentence" (if ok=false)
                }
            ]
        }
    """
    repo = get_repo(repo_name)
    if not repo:
        return {
            "repo": repo_name,
            "ready": False,
            "items": [{
                "key": "repo",
                "ok": False,
                "severity": "block",
                "text": f"Repository {repo_name!r} not found.",
                "fix": "Add the repository first."
            }]
        }

    items = []

    # Determine platform (explicit or inferred)
    source = repo.get("source", "")
    platform = (repo.get("platform") or "").strip() or detect_platform(source)

    # Check: the repo must have a check command
    check_cmd = repo.get("check", "").strip()
    items.append(_assess_check(check_cmd))

    # Delivery: a production repo should use a forge
    items.append(_assess_delivery(platform))

    # Auto-merge: must be off for a forge repo
    auto_merge = repo.get("auto_merge", False)
    items.append(_assess_auto_merge(auto_merge, platform))

    # Read isolation: sandbox.read_isolation must be on
    items.append(_assess_read_isolation())

    # Egress: sandbox.egress must be on
    items.append(_assess_egress())

    # Forge token: for github/gitlab repos, a token must be configured
    items.append(_assess_forge_token(platform, repo))

    # Pilot mode: warn if not read-only
    mode = repo.get("mode", "write").strip().lower()
    items.append(_assess_pilot_mode(mode))

    # A repo is "ready" when there are no failed "block" items
    ready = all(item["ok"] or item["severity"] == "warn" for item in items)

    return {
        "repo": repo_name,
        "ready": ready,
        "items": items
    }


def _assess_check(check_cmd: str) -> dict:
    """A repo must have a check command that validates every MR."""
    ok = bool(check_cmd)
    return {
        "key": "check",
        "ok": ok,
        "severity": "block",
        "text": "A check command runs against every MR to gate merges."
                if ok else "No check command configured.",
        "fix": "Add a check: command to the repo (e.g., check: python -m pytest -q)."
               if not ok else ""
    }


def _assess_delivery(platform: str) -> dict:
    """A production repo should deliver via a forge so CI and branch protection apply."""
    ok = platform in ("github", "gitlab")
    return {
        "key": "delivery",
        "ok": ok,
        "severity": "warn",
        "text": "Delivery is through a forge (github/gitlab)."
                if ok else f"Delivery is local (platform: {platform!r}); forge provides CI and branch protection.",
        "fix": "Set platform: github or platform: gitlab in the repo config."
               if not ok else ""
    }


def _assess_auto_merge(auto_merge: bool, platform: str) -> dict:
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
                 if ok else "Auto-merge is on; turn it off for a forge repo."),
        "fix": "Set auto_merge: false in the repo config."
               if not ok else ""
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
                if ok else "Read isolation is disabled; workers can read the entire home directory.",
        "fix": "Set sandbox.read_isolation: true in the config."
               if not ok else ""
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
                if ok else "Egress is open; workers have unrestricted network access.",
        "fix": "Set sandbox.egress: true in the config."
               if not ok else ""
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
            "fix": ""
        }

    # Check for a per-repo token first, then fall back to global config
    repo_token = repo.get("token", "").strip() if repo else ""
    if repo_token and not repo_token.startswith("${"):
        return {
            "key": "forge_token",
            "ok": True,
            "severity": "block",
            "text": f"A {platform} token is configured (per-repo).",
            "fix": ""
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
            "fix": ""
        }

    return {
        "key": "forge_token",
        "ok": False,
        "severity": "block",
        "text": f"No {platform} token configured.",
        "fix": f"Set {platform}.token in the config or set a per-repo token: for this repo."
    }


def _assess_pilot_mode(mode: str) -> dict:
    """A repo should start in read-only mode for a safe pilot."""
    ok = mode == "read-only"
    return {
        "key": "pilot_mode",
        "ok": ok,
        "severity": "warn",
        "text": "Repo is in read-only mode; a safe pilot before allowing writes."
                if ok else f"Repo mode is {mode!r}; start with mode: read-only for a safe pilot.",
        "fix": "Set mode: read-only in the repo config."
               if not ok else ""
    }
