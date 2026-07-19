"""Git worktree manager for parallel agent isolation."""
import os
import shutil
import stat
import subprocess
from pathlib import Path

WORKSPACE_ROOT = os.environ.get("WORKSPACE_ROOT", "/workspace")
REPOS_DIR = Path(WORKSPACE_ROOT) / "repos"


def init_bare_clone(repo_url: str, bare_path: str) -> str:
    """Create a bare git clone. Returns the bare repo path."""
    bare = Path(bare_path)
    if not bare.exists():
        result = subprocess.run(
            ["git", "clone", "--bare", repo_url, str(bare)],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to clone {repo_url}: {result.stderr}")
    return str(bare)


def create_worktree(bare_path: str, run_id: str, step_id: str,
                    branch: str = "main") -> str:
    """Create a git worktree for a specific pipeline step.

    Worktrees are placed outside the bare clone at:
        {workspace}/repos/worktrees/{run_id}_{step_id}

    This avoids Git admin directory name truncation issues when
    worktrees are nested inside the bare clone.
    """
    worktree_dir = (REPOS_DIR / "worktrees").resolve()
    worktree_path = worktree_dir / f"{run_id}_{step_id}"

    if worktree_path.exists():
        return str(worktree_path)

    worktree_dir.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        ["git", "-C", bare_path, "worktree", "add", str(worktree_path),
         branch],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        if "already used by worktree" in result.stderr:
            detached_result = subprocess.run(
                ["git", "-C", bare_path, "worktree", "add",
                 "--detach", str(worktree_path), branch],
                capture_output=True, text=True
            )
            if detached_result.returncode != 0:
                raise RuntimeError(
                    f"Failed to create detached worktree: "
                    f"{detached_result.stderr}"
                )
            return str(worktree_path)

        if "submodule" in result.stderr.lower():
            raise RuntimeError(
                f"Submodule support not available: {result.stderr}"
            )
        if "lfs" in result.stderr.lower():
            raise RuntimeError(
                f"Git-LFS support not available: {result.stderr}"
            )
        raise RuntimeError(f"Failed to create worktree: {result.stderr}")

    return str(worktree_path)


def remove_worktree(bare_path: str, run_id: str, step_id: str,
                    force: bool = True) -> bool:
    """Remove a git worktree. Returns True if successful."""
    worktree_path = (REPOS_DIR / "worktrees" / f"{run_id}_{step_id}").resolve()

    if not worktree_path.exists():
        return True

    cmd = ["git", "-C", bare_path, "worktree", "remove",
           str(worktree_path)]
    if force:
        cmd.append("--force")

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0 and worktree_path.exists():
        shutil.rmtree(str(worktree_path), ignore_errors=True)

    return True


def set_access(worktree_path: str, level: str):
    """Set file access permissions on a worktree recursively.

    Modes for directories:
      read    → 555 (r-xr-xr-x)
      analyze → 755 (rwxr-xr-x)
      edit    → 755 (rwxr-xr-x)

    Files under a read-only directory get 444 (r--r--r--).
    Files under a writable directory get 644 (rw-r--r--).
    """
    mode_map = {
        "read": (stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP
                 | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH),
        "analyze": (stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP
                    | stat.S_IROTH | stat.S_IXOTH),
        "edit": (stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP
                 | stat.S_IROTH | stat.S_IXOTH),
    }
    mode = mode_map.get(level)
    if mode is None:
        return

    path = Path(worktree_path)
    if not path.exists():
        return

    # Apply recursively to all files and directories
    for entry in path.rglob("*"):
        try:
            if entry.is_dir():
                entry.chmod(mode)
            elif entry.is_file():
                file_mode = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
                if mode & stat.S_IWUSR:
                    file_mode |= stat.S_IWUSR
                entry.chmod(file_mode)
        except OSError:
            pass
    # Root directory
    path.chmod(mode)


def cleanup_orphans(bare_path: str) -> int:
    """Remove worktrees not associated with active runs in SQLite.
    Returns count of removed worktrees."""
    wt_dir = REPOS_DIR / "worktrees"

    if not wt_dir.exists():
        return 0

    removed = 0
    for wt_entry in wt_dir.iterdir():
        if not wt_entry.is_dir():
            continue
        entry_name = wt_entry.name
        if "_" not in entry_name:
            continue
        run_id = entry_name.rsplit("_", 1)[0]

        from core.db import get_conn
        conn = get_conn()
        cursor = conn.execute(
            "SELECT COUNT(*) as cnt FROM steps WHERE run_id = ? "
            "AND status IN ('pending', 'running')",
            (run_id,)
        )
        row = cursor.fetchone()
        conn.close()

        if row and row["cnt"] == 0:
            subprocess.run(
                ["git", "-C", bare_path, "worktree", "remove",
                 "--force", str(wt_entry)],
                capture_output=True
            )
            shutil.rmtree(str(wt_entry), ignore_errors=True)
            removed += 1

    return removed


def get_repo_name_from_url(repo_url: str) -> str:
    """Extract repo name from Git URL."""
    name = repo_url.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name
