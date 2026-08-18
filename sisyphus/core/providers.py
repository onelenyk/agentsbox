"""Change publishers — turn a committed task branch into a reviewable change.

One entry point, ``publish_change``, dispatches on the repo's platform:
  - local  → compute a diff patch, save it, describe it on the thread
  - gitlab → push the branch, open an MR via REST, return its URL
  - github → push the branch, open a PR via REST, return its URL

Credentials never reach the worker: the worker only calls the open_mr tool; the
daemon (this code) does the git/API work with server-side creds from config.
"""
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from . import attachments, git_ops


def detect_platform(source: str) -> str:
    """Which forge (if any) a repo lives on, inferred from its clone source.
    A per-repo 'platform' override in config wins over this when present."""
    s = (source or "").lower()
    if "://" in s or s.startswith("git@"):
        if "github" in s:
            return "github"
        if "gitlab" in s:
            return "gitlab"
        return "unknown"          # a remote we don't have a provider for
    return "local"                # a filesystem path — no forge


def _repo_slug(source: str) -> str:
    """owner/repo (GitHub) or the full project path (GitLab) from a clone URL."""
    s = source.strip()
    if s.startswith("git@"):
        s = s.split(":", 1)[1] if ":" in s else s
    elif "://" in s:
        s = s.split("://", 1)[1]
        s = s.split("/", 1)[1] if "/" in s else s
    if s.endswith(".git"):
        s = s[:-4]
    return s.strip("/")


def _bare_path(task_id: str, source: str) -> str:
    from .worktree import get_repo_name_from_url
    ws = os.environ.get("WORKSPACE_ROOT", "/workspace")
    return f"{ws}/repos/{task_id}_{get_repo_name_from_url(source)}"


def _token(cfg: dict) -> "str | None":
    """A configured token, or None. Guards against an unresolved ${VAR}
    placeholder (config leaves it literal when the env var is unset)."""
    tok = (cfg or {}).get("token") or ""
    return tok if tok and not tok.startswith("${") else None


def _resolve_forge_url(source: str) -> "str | None":
    """The forge URL a repo publishes to: the source itself when it is already a
    URL, or the 'origin' remote of a path-registered repo (None if it has none)."""
    s = (source or "").strip()
    if "://" in s or s.startswith("git@"):
        return s
    if not s:
        return None
    r = subprocess.run(["git", "-C", s, "remote", "get-url", "origin"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    url = r.stdout.strip()
    return url or None


def _push(bare: str, branch: str, forge_url: str) -> None:
    r = subprocess.run(["git", "-C", bare, "push", forge_url, f"{branch}:{branch}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"push failed: {r.stderr.strip()}")


def _post_json(url: str, headers: dict, payload: dict) -> "dict | Exception":
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc
    except urllib.error.URLError as exc:
        return exc


def publish_change(session: dict, title: str, description: str) -> dict:
    """Publish the task's committed branch. Returns {"error": ...} or
    {"ok": True, ...} with fields the caller posts to the thread."""
    from . import repos, store

    task = store.get_task(session["task_id"])
    repo = repos.get_repo(task["repo"]) if task and task.get("repo") else None
    if not repo:
        return {"error": "task has no repo — nothing to publish"}

    source = repo.get("source", "")
    platform = (repo.get("platform") or "").strip() or detect_platform(source)
    task_id = session["task_id"]
    # Open the MR against the branch this task was cut from. For a plan step that
    # is the plan's integration branch, so the MR's diff is just this step's delta
    # on the shared base (not everything since main) and it merges into the branch
    # its siblings share — several agents contributing to one codebase. Falls back
    # to the repo default for a plain (non-plan) task.
    base = task.get("base_branch") or repo.get("branch", "main")
    bare = _bare_path(task_id, source)
    branch = git_ops.task_branch(task_id)

    if platform == "local":
        return _publish_local(task_id, bare, base, branch, title, task["repo"], source)
    if platform == "gitlab":
        return _publish_gitlab(bare, source, branch, base, title, description, task["repo"], task_id)
    if platform == "github":
        return _publish_github(bare, source, branch, base, title, description, task["repo"], task_id)
    return {"error": f"no publisher for platform {platform!r} — change is committed on {branch}"}


def _publish_local(task_id: str, bare: str, base: str, branch: str, title: str,
                   repo_name: str = "", source: str = "") -> dict:
    """Local repo, no forge: the reviewable change IS the diff. Save the full
    patch durably, register a local Merge Request, and hand back a files-changed
    summary for the thread. Read-only to the user's repo — nothing is pushed back
    (the MR's Land/Merge actions do that explicitly, on demand)."""
    patch = git_ops.diff_patch(bare, base, branch)
    if not patch.strip():
        return {"error": "no changes between the task branch and the base — nothing to open"}
    stat = git_ops.diff_stat(bare, base, branch).strip()
    patch_path = attachments.save_text(task_id, "change.patch", patch)

    from . import mergerequests
    existing = mergerequests.find_open_mr(task_id, branch, base)
    if existing:
        mr = mergerequests.update_mr(existing["id"], title=title)
    else:
        mr = mergerequests.create_mr(
            task_id=task_id, repo=repo_name, source_branch=branch, target_branch=base,
            title=title, bare_path=bare, base_branch=base, source_path=source,
        )
    return {"ok": True, "platform": "local", "branch": branch, "stat": stat,
            "patch_file": "change.patch", "patch_path": patch_path, "mr_id": mr["id"]}



def _record_forge_mr(task_id, repo_name, branch, base_branch, title, bare, source, url):
    """Record a forge-hosted merge request in our own ledger.

    Only _publish_local did this, so a GitHub or GitLab change existed on the
    forge and nowhere in Sisyphus: list_mrs() never returned it, which left
    core.forge_poll — the whole point of which is to follow what happens to
    these — with nothing to poll. Best-effort: the change is already published,
    and losing our bookkeeping must not turn a successful publish into an error.
    """
    if not task_id:
        return
    try:
        from . import mergerequests
        existing = mergerequests.find_open_mr(task_id, branch, base_branch)
        if existing:
            mergerequests.set_forge_url(existing["id"], url)
        else:
            mergerequests.create_mr(
                task_id=task_id, repo=repo_name, source_branch=branch,
                target_branch=base_branch, title=title, bare_path=bare,
                base_branch=base_branch, source_path=source, forge_url=url,
            )
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception(
            "published %s but could not record the merge request locally", url)


def _publish_gitlab(bare, source, branch, base_branch, title, description, repo_name="", task_id="") -> dict:
    from .config import get_config
    from .repos import repo_credential
    cfg = get_config("gitlab", {}) or {}
    # Prefer the repo's own token; the global one is the fallback. Naming the
    # variable that was expected turns "not configured" into an instruction.
    cred = repo_credential(repo_name) if repo_name else {"token": None, "env_hint": "GITLAB_TOKEN"}
    token = cred["token"] or _token(cfg)
    if not token:
        return {"error": f"GitLab token not configured for {repo_name or 'this repo'} "
                         f"(set {cred.get('env_hint') or 'GITLAB_TOKEN'}, or a token: on the repo)"}
    api = (cfg.get("base_url") or "https://gitlab.com")
    if api.startswith("${"):
        api = "https://gitlab.com"
    api = api.rstrip("/")
    forge_url = _resolve_forge_url(source)
    if not forge_url:
        return {"error": "path-registered repo has no origin remote — cannot publish to a forge"}
    try:
        _push(bare, branch, forge_url)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    project = urllib.parse.quote(_repo_slug(forge_url), safe="")
    res = _post_json(
        f"{api}/api/v4/projects/{project}/merge_requests",
        {"PRIVATE-TOKEN": token},
        {"source_branch": branch, "target_branch": base_branch,
         "title": title, "description": description},
    )
    if isinstance(res, urllib.error.HTTPError):
        return {"error": f"GitLab MR failed: HTTP {res.code} {res.read().decode()[:200]}"}
    if isinstance(res, urllib.error.URLError):
        return {"error": f"GitLab unreachable: {res.reason}"}
    url = res.get("web_url", "")
    _record_forge_mr(task_id, repo_name, branch, base_branch, title, bare, source, url)
    return {"ok": True, "platform": "gitlab", "branch": branch, "url": url}


def _publish_github(bare, source, branch, base_branch, title, description, repo_name="", task_id="") -> dict:
    from .config import get_config
    from .repos import repo_credential
    cfg = get_config("github", {}) or {}
    # Prefer the repo's own token; the global one is the fallback. Naming the
    # variable that was expected turns "not configured" into an instruction.
    cred = repo_credential(repo_name) if repo_name else {"token": None, "env_hint": "GITHUB_TOKEN"}
    token = cred["token"] or _token(cfg)
    if not token:
        return {"error": f"GitHub token not configured for {repo_name or 'this repo'} "
                         f"(set {cred.get('env_hint') or 'GITHUB_TOKEN'}, or a token: on the repo)"}
    api = (cfg.get("base_url") or "https://api.github.com")
    if api.startswith("${"):
        api = "https://api.github.com"
    api = api.rstrip("/")
    forge_url = _resolve_forge_url(source)
    if not forge_url:
        return {"error": "path-registered repo has no origin remote — cannot publish to a forge"}
    try:
        _push(bare, branch, forge_url)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    res = _post_json(
        f"{api}/repos/{_repo_slug(forge_url)}/pulls",
        {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        {"head": branch, "base": base_branch, "title": title, "body": description},
    )
    if isinstance(res, urllib.error.HTTPError):
        return {"error": f"GitHub PR failed: HTTP {res.code} {res.read().decode()[:200]}"}
    if isinstance(res, urllib.error.URLError):
        return {"error": f"GitHub unreachable: {res.reason}"}
    url = res.get("html_url", "")
    _record_forge_mr(task_id, repo_name, branch, base_branch, title, bare, source, url)
    return {"ok": True, "platform": "github", "branch": branch, "url": url}
