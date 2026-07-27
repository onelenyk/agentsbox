"""Change publishers — turn a committed task branch into a reviewable change.

One entry point, ``publish_change``, dispatches on the repo's platform:
  - local  → compute a diff patch, save it, describe it on the thread (built here)
  - github → open a PR via REST (Bucket B #1d — not yet implemented)
  - gitlab → open an MR via REST (Bucket B #1c — not yet implemented)

Credentials never reach the worker: the worker only calls the open_mr tool; the
daemon (this code) does the git/API work with server-side creds.
"""
import os

from . import git_ops


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


def _bare_path(task_id: str, source: str) -> str:
    from .worktree import get_repo_name_from_url
    ws = os.environ.get("WORKSPACE_ROOT", "/workspace")
    return f"{ws}/repos/{task_id}_{get_repo_name_from_url(source)}"


def publish_change(session: dict, title: str, description: str) -> dict:
    """Publish the task's committed branch. Returns a result dict — {"error": ...}
    or {"ok": True, ...} with fields the caller posts to the thread."""
    from . import repos, store

    task = store.get_task(session["task_id"])
    repo = repos.get_repo(task["repo"]) if task and task.get("repo") else None
    if not repo:
        return {"error": "task has no repo — nothing to publish"}

    platform = (repo.get("platform") or "").strip() or detect_platform(repo.get("source", ""))
    task_id = session["task_id"]
    base = repo.get("branch", "main")
    bare = _bare_path(task_id, repo.get("source", ""))
    branch = git_ops.task_branch(task_id)

    if platform == "local":
        return _publish_local(task_id, bare, base, branch, title)

    return {
        "error": (
            f"{platform!r} publishing isn't implemented yet — your change is "
            f"committed on branch {branch}."
        )
    }


def _publish_local(task_id: str, bare: str, base: str, branch: str, title: str) -> dict:
    """Local repo, no forge: the reviewable change IS the diff. Save the full
    patch durably (survives task-terminal worktree cleanup) and hand back a
    files-changed summary for the thread."""
    patch = git_ops.diff_patch(bare, base, branch)
    if not patch.strip():
        return {"error": "no changes between the task branch and the base — nothing to open"}

    from . import attachments

    stat = git_ops.diff_stat(bare, base, branch).strip()
    patch_path = attachments.save_text(task_id, "change.patch", patch)

    return {
        "ok": True,
        "platform": "local",
        "branch": branch,
        "stat": stat,
        "patch_file": "change.patch",
        "patch_path": patch_path,
    }
