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

def repo_setup(name):
    """The repo's raw ``setup`` block (see core.setup_hook), or None when it has
    none — or when the repo is unknown, which reaches spawn_worker as its own
    refusal before any hook is consulted.

    Deliberately unvalidated: core.setup_hook decides what a malformed block
    means, because the one thing that must never happen is a mistyped hook
    arriving at a caller as "this repo configures no hook" and the worker
    starting without the files the human thought they had configured.
    """
    return (get_repo(name) or {}).get("setup")


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

def _read_config_file() -> dict:
    """The raw on-disk config, ${VAR} placeholders intact (this is what gets
    written back). On a fresh checkout that is config.example.yaml — see
    core.config.config_file — so the first write carries its keys over."""
    from core.config import config_file
    path = config_file()
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def _write_config_file(config: dict) -> None:
    # Always the real file: the example is a template, never written to.
    real = Path(__file__).parent.parent / "config.yaml"
    real.write_text(yaml.dump(config, default_flow_style=False, allow_unicode=True))
    from core.config import reload_config
    reload_config()


def _save_repo_config(name, source, platform, branch):
    config = _read_config_file()
    repos = config.get("repos", [])
    repos = [r for r in repos if r.get("name") != name]
    entry = {"name": name, "source": source, "branch": branch}
    if platform:
        entry["platform"] = platform
    repos.append(entry)
    config["repos"] = repos
    _write_config_file(config)


def delete_repo(name):
    """Delete a repo from config and remove bare clone from disk."""
    import shutil
    bare_path = REPOS_DIR / name
    if bare_path.exists():
        shutil.rmtree(str(bare_path), ignore_errors=True)
    # Remove from config
    config = _read_config_file()
    repos = config.get("repos", [])
    config["repos"] = [r for r in repos if r.get("name") != name]
    _write_config_file(config)


def is_read_only(repo_name: str) -> bool:
    """True if a repo is marked read-only: agents may investigate and review,
    never change it."""
    repo = get_repo(repo_name)
    if not repo:
        return False
    return (repo.get("mode") or "").strip() == "read-only"


def update_repo_config(name, source=None, platform=None, branch=None, auto_merge=None, mode=None):
    """Update repo configuration without re-cloning.

    In-place edits of the existing entry, so every other key the UI does not
    know about — ``check``, ``setup``, ``token``, … — survives the save.
    ``_save_repo_config`` cannot be used here: it rebuilds the entry from
    scratch and would drop those keys."""
    # The raw entries, placeholders intact: the loaded config has every
    # ${VAR} already filled from the vault, and writing that back would put
    # a token into config.yaml in plain text.
    config = _read_config_file()
    repos = config.get("repos") or []
    for r in repos:
        if r.get("name") == name:
            if source: r["source"] = source
            if branch: r["branch"] = branch
            if platform is not None:
                if platform:
                    r["platform"] = platform
                else:
                    r.pop("platform", None)
            if auto_merge is not None:
                r["auto_merge"] = bool(auto_merge)
            if mode is not None:
                if mode:
                    r["mode"] = mode
                else:
                    r.pop("mode", None)
            break
    config["repos"] = repos
    _write_config_file(config)


# ── Per-repo forge credentials ───────────────────────────────────────────

def _resolved(value) -> str:
    """A config string, or "" when it is an unresolved ${VAR} placeholder.

    core.config substitutes ${VAR} from the environment at load and leaves the
    literal in place when the variable is unset, so the placeholder itself must
    never be mistaken for a credential.
    """
    v = str(value or "").strip()
    return "" if (not v or v.startswith("${")) else v


def repo_credential(name: str) -> dict:
    """The token a repo publishes with: {token, source, platform, env_hint}.

    ``source`` is "repo" or "global" — which is the whole point of this
    function. A single global token per forge holds only until there are two
    organisations, or a work account and a personal one; the repository that
    needs a credential is the thing that should carry it. The global block stays
    as a fallback so an existing setup keeps working untouched.

    ``token`` is None when nothing usable is configured. It is never the
    unresolved placeholder, and ``env_hint`` names the variable that was
    expected, so a caller can say *which* variable to set instead of "not
    configured".
    """
    from core.config import get_config
    from core.providers import detect_platform

    from core.credentials import get as credential, repo_token_name

    repo = get_repo(name) or {}
    platform = (repo.get("platform") or "").strip() or detect_platform(repo.get("source", ""))

    own = credential(repo_token_name(name)) or _resolved(repo.get("token"))
    if own:
        return {"token": own, "source": "repo", "platform": platform, "env_hint": ""}

    cfg = get_config(platform, {}) if platform in ("github", "gitlab") else {}
    shared = _resolved((cfg or {}).get("token"))
    if shared:
        return {"token": shared, "source": "global", "platform": platform, "env_hint": ""}

    hint = {"github": "GITHUB_TOKEN", "gitlab": "GITLAB_TOKEN"}.get(platform, "")
    return {"token": None, "source": None, "platform": platform, "env_hint": hint}
