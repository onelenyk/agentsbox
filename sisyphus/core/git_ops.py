"""Real git operations on a task branch — the shared spine under commit / open_mr.

Every task gets its own branch, ``sisyphus/<task_id>``, cut from the repo's
default branch. The engineer commits onto it; open_mr publishes it (a real
PR/MR for a forge, a diff for a local repo). This module is platform-agnostic
plumbing — the publish step lives in core.providers.
"""
import subprocess

TASK_BRANCH_PREFIX = "sisyphus/"

# Files that must never end up in a task's commits, excluded via git pathspec
# magic at add time: the daemon's generated opencode config + session state, and
# common build/OS junk an agent can create by running the code (e.g. it runs the
# tests and __pycache__ appears). Keeps engineer commits clean without relying on
# the target repo having a .gitignore.
# Long-form :(exclude) pathspecs (the :! short form mis-parses a leading "_" as
# magic). git's default matching lets "*" span "/", so "*.pyc" / "*__pycache__*"
# catch nested paths too.
_COMMIT_EXCLUDES = [
    ":(exclude)opencode.json",
    ":(exclude).opencode", ":(exclude).opencode/**",
    ":(exclude).sisyphus", ":(exclude).sisyphus/**",
    ":(exclude)*.pyc",
    ":(exclude)*__pycache__*",
    ":(exclude)*.DS_Store",
    ":(exclude)*.pytest_cache*",
]


def task_branch(task_id: str) -> str:
    return f"{TASK_BRANCH_PREFIX}{task_id}"


def _run(args, cwd=None):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def is_git_repo(worktree: str) -> bool:
    """True if *worktree* is inside a git working tree — false for a repo-less
    task's scratch dir, where commit/open_mr have nothing to act on."""
    if not worktree:
        return False
    r = _run(["git", "-C", worktree, "rev-parse", "--is-inside-work-tree"])
    return r.returncode == 0 and r.stdout.strip() == "true"


def ensure_task_branch(bare_path: str, task_id: str, base_branch: str) -> str:
    """Create ``sisyphus/<task_id>`` off *base_branch* in the bare repo if it
    doesn't exist yet. Returns the branch name."""
    branch = task_branch(task_id)
    exists = _run(["git", "-C", bare_path, "rev-parse", "--verify", "--quiet",
                   f"refs/heads/{branch}"])
    if exists.returncode != 0:
        r = _run(["git", "-C", bare_path, "branch", branch, base_branch])
        if r.returncode != 0:
            raise RuntimeError(f"could not create task branch {branch}: {r.stderr.strip()}")
    return branch


def commit_all(worktree: str, message: str) -> "str | None":
    """Stage everything (minus daemon artifacts) and commit on the worktree's
    current branch. Returns the new commit SHA, or None if there was nothing to
    commit. The worktree is checked out ON the task branch for the engineer, so
    the commit advances that branch."""
    _run(["git", "-C", worktree, "add", "-A", "--", "."] + _COMMIT_EXCLUDES)
    staged = _run(["git", "-C", worktree, "diff", "--cached", "--quiet"])
    if staged.returncode == 0:
        return None  # nothing staged → nothing to commit
    c = _run([
        "git", "-C", worktree,
        "-c", "user.email=sisyphus@local", "-c", "user.name=Sisyphus",
        "commit", "-m", message,
    ])
    if c.returncode != 0:
        raise RuntimeError(f"commit failed: {c.stderr.strip()}")
    return _run(["git", "-C", worktree, "rev-parse", "HEAD"]).stdout.strip()


def diff_stat(bare_path: str, base_branch: str, branch: str) -> str:
    """`git diff --stat base...branch` from the bare repo — the files-changed
    summary of what the task branch introduces."""
    return _run(["git", "-C", bare_path, "diff", "--stat",
                 f"{base_branch}...{branch}"]).stdout


def diff_patch(bare_path: str, base_branch: str, branch: str) -> str:
    """`git diff base...branch` — the full patch the task branch introduces."""
    return _run(["git", "-C", bare_path, "diff",
                 f"{base_branch}...{branch}"]).stdout
