"""Git repository manager — bare clones + platform mapping."""
import os, subprocess, yaml
from pathlib import Path

WORKSPACE_ROOT = os.environ.get("WORKSPACE_ROOT", "/workspace")
REPOS_DIR = Path(WORKSPACE_ROOT) / "repos"

def load_repos():
    from core.config import get_config
    return get_config("repos", [])

def add_repo(name, source, platform, branch="main"):
    bare_path = REPOS_DIR / name
    REPOS_DIR.mkdir(parents=True, exist_ok=True)
    if not bare_path.exists():
        result = subprocess.run(["git", "clone", "--bare", source, str(bare_path)], capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"Clone failed: {result.stderr}")
    _save_repo_config(name, source, platform, branch)
    return str(bare_path)

def get_repo(name):
    for r in load_repos():
        if r["name"] == name:
            return r
    return None

def list_repos():
    repos = load_repos()
    for r in repos:
        bare_path = REPOS_DIR / r["name"]
        r["cloned"] = bare_path.exists() and (bare_path / "HEAD").exists()
        r["reach"] = source_reach(r.get("source", ""))
    return repos


def source_reach(source: str) -> str:
    """Whether work can actually start against this repo: ``ok``, ``missing``
    or ``remote``.

    Not the same question as ``cloned``. Workers clone per task into
    ``repos/<task_id>_<name>`` and cleanup_task deletes it, so the persistent
    ``repos/<name>`` bare clone that ``cloned`` looks for exists only if
    add_repo made one — leaving every repo reading "pending" forever, including
    ones that have run dozens of tasks. What decides whether the next task can
    run is the source being reachable.
    """
    source = (source or "").strip()
    if not source:
        return "missing"
    if "://" in source or source.startswith("git@"):
        return "remote"        # can't verify without a network round-trip
    p = Path(source).expanduser()
    if not p.exists():
        return "missing"
    return "ok" if (p / ".git").exists() or (p / "HEAD").exists() else "missing"

def update_repo(name):
    bare_path = REPOS_DIR / name
    if bare_path.exists():
        subprocess.run(["git", "-C", str(bare_path), "fetch", "origin"], capture_output=True, timeout=30)
        return True
    return False

def _save_repo_config(name, source, platform, branch):
    config_path = Path(__file__).parent.parent / "config.yaml"
    config = yaml.safe_load(config_path.read_text()) or {}
    repos = config.get("repos", [])
    repos = [r for r in repos if r.get("name") != name]
    repos.append({"name": name, "source": source, "branch": branch})
    config["repos"] = repos
    config_path.write_text(yaml.dump(config, default_flow_style=False, allow_unicode=True))
    from core.config import reload_config
    reload_config()
def delete_repo(name):
    """Delete a repo from config and remove bare clone from disk."""
    import shutil
    bare_path = REPOS_DIR / name
    if bare_path.exists():
        shutil.rmtree(str(bare_path), ignore_errors=True)
    # Remove from config
    config_path = Path(__file__).parent.parent / "config.yaml"
    config = yaml.safe_load(config_path.read_text()) or {}
    repos = config.get("repos", [])
    config["repos"] = [r for r in repos if r.get("name") != name]
    config_path.write_text(yaml.dump(config, default_flow_style=False, allow_unicode=True))
    from core.config import reload_config
    reload_config()
def update_repo_config(name, source=None, platform=None, branch=None):
    """Update repo configuration without re-cloning."""
    repos = load_repos()
    for r in repos:
        if r["name"] == name:
            if source: r["source"] = source
            if branch: r["branch"] = branch
            break
    config_path = Path(__file__).parent.parent / "config.yaml"
    config = yaml.safe_load(config_path.read_text()) or {}
    config["repos"] = repos
    config_path.write_text(yaml.dump(config, default_flow_style=False, allow_unicode=True))
    from core.config import reload_config
    reload_config()
