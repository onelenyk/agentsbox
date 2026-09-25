"""Real git operations on a task branch — the shared spine under commit / open_mr.

Every task gets its own branch, ``sisyphus/<task_id>``, cut from the repo's
default branch. The engineer commits onto it; open_mr publishes it (a real
PR/MR for a forge, a diff for a local repo). This module is platform-agnostic
plumbing — the publish step lives in core.providers.
"""
import os
import stat
import subprocess

TASK_BRANCH_PREFIX = "sisyphus/"

# Files that must never end up in a task's commits, excluded via git pathspec
# magic at add time: whatever the runner CLI generated (its config + session
# state), plus common build/OS junk an agent can create by running the code
# (e.g. it runs the tests and __pycache__ appears). Keeps engineer commits clean
# without relying on the target repo having a .gitignore.
# Long-form :(exclude) pathspecs (the :! short form mis-parses a leading "_" as
# magic). git's default matching lets "*" span "/", so "*.pyc" / "*__pycache__*"
# catch nested paths too.
#
# The runner-owned names come from core.runner_profiles rather than being listed
# here, so adding a runner cannot leave its scaffolding committable: this list
# previously knew only opencode's, which meant a Claude worker — whose cwd is
# the same worktree — could have committed its own state directory.
def _runner_excludes():
    from .runner_profiles import workspace_artifacts
    out = []
    for path in workspace_artifacts():
        # Both forms unconditionally: a directory needs the /** to exclude its
        # contents, and on a plain file that pattern simply matches nothing.
        out.append(f":(exclude){path}")
        out.append(f":(exclude){path}/**")
    return out


_COMMIT_EXCLUDES = _runner_excludes() + [
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


def ensure_branch(repo_path: str, branch: str, base: str) -> bool:
    """Create *branch* off *base* in a real (non-bare) repo if it doesn't already
    exist. Non-destructive — only adds a ref, never touches the working tree.
    Returns True if it was created, False if it already existed. Used to stand up
    a plan's integration branch in the source repo so dependent steps stack on it."""
    exists = _run(["git", "-C", repo_path, "rev-parse", "--verify", "--quiet",
                   f"refs/heads/{branch}"])
    if exists.returncode == 0:
        return False
    r = _run(["git", "-C", repo_path, "branch", branch, base])
    if r.returncode != 0:
        raise RuntimeError(f"could not create branch {branch} off {base}: {r.stderr.strip()}")
    return True


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


# A goal that explicitly asks for a mode change opts the commit guard out.
# Terms are intentionally loose (case-insensitive) — "executable bit", "chmod",
# "file mode", "mode change" — so a goal that wants e.g. build.sh non-executable
# isn't silently overruled by the guard restoring it.
_MODE_CHANGE_TERMS = ("executable bit", "chmod", "file mode", "mode change")


def goal_allows_mode_change(goal: str) -> bool:
    """True when the task goal explicitly asks for a mode change, which disables
    the commit-time exec-bit guard (see restore_dropped_exec_bits)."""
    g = (goal or "").lower()
    return any(t in g for t in _MODE_CHANGE_TERMS)


def restore_dropped_exec_bits(worktree: str, goal: str = "",
                              allow_mode_change: bool = False) -> list:
    """The agent commit-time guard.

    Stages the worktree exactly like commit_all, then inspects the staged diff
    (`git diff --cached --raw`) for entries whose ONLY change is a mode drop
    100755 -> 100644 with an identical blob (no content change) — the sandbox
    checkout artifact that kept stripping the exec bit on tools/goal_report.py
    and desktop/build.sh. Each such file gets its exec bit restored, both in
    the index (`git update-index --chmod=+x`) and on disk, so the subsequent
    `git add -A` in commit_all cannot re-stage the drop.

    Guarded by the task goal: when it explicitly asks for a mode change
    (goal_allows_mode_change) or the caller passes *allow_mode_change*, modes
    are left untouched. Returns the list of paths whose exec bit was restored,
    so the commit tool can append a note to its result text."""
    if allow_mode_change or goal_allows_mode_change(goal):
        return []
    _run(["git", "-C", worktree, "add", "-A", "--", "."] + _COMMIT_EXCLUDES)
    raw = _run(["git", "-C", worktree, "diff", "--cached", "--raw", "--abbrev=40"])
    restored = []
    if raw.returncode != 0:
        return restored
    for line in raw.stdout.splitlines():
        # format: :100755 100644 <old-blob> <new-blob> M<TAB>path
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) < 5:
            continue
        old_mode, new_mode, old_sha, new_sha = parts[0], parts[1], parts[2], parts[3]
        if old_mode == ":100755" and new_mode == "100644" and old_sha == new_sha:
            _run(["git", "-C", worktree, "update-index", "--chmod=+x", path])
            full = os.path.join(worktree, path)
            try:
                os.chmod(full, os.stat(full).st_mode
                         | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            except OSError:
                pass
            restored.append(path)
    return restored


def diff_stat(bare_path: str, base_branch: str, branch: str) -> str:
    """`git diff --stat base...branch` from the bare repo — the files-changed
    summary of what the task branch introduces."""
    return _run(["git", "-C", bare_path, "diff", "--stat",
                 f"{base_branch}...{branch}"]).stdout


def diff_patch(bare_path: str, base_branch: str, branch: str) -> str:
    """`git diff base...branch` — the full patch the task branch introduces."""
    return _run(["git", "-C", bare_path, "diff",
                 f"{base_branch}...{branch}"]).stdout
