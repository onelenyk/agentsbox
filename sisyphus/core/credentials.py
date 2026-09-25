"""Tokens, keys and the addresses that go with them — entered in the UI, kept
in Sisyphus's vault (core.keychain), and nowhere else.

config.yaml names every credential as a ``${VAR}`` placeholder (jira.token:
${JIRA_API_TOKEN}); core.config fills those from the vault and from nothing
else. There is no file fallback on purpose: not .env, not the shell, not a
value typed into config.yaml. A credential in a file is a credential every
worker can read, and one with two possible sources is one nobody can be sure
about. What an older setup left in files is shown on the page, with a button
that moves it into the vault and takes it out of the file.

A repo's own forge token is ``REPO_<NAME>_TOKEN`` (see ``repo_token_name``),
set on the same page; a repo needs nothing in config.yaml for it.

Values resolved here are never put into ``os.environ`` — a worker inherits the
daemon's environment — and ``worker_strip`` names what the launcher removes
in case the shell put one there.
"""
import logging
import os
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

SERVICE = "sisyphus-secret"

# The credentials Sisyphus itself reads. `secret` decides whether the page may
# show the value back; an address is not a secret, a token is. `path` is where
# config.yaml keeps its placeholder, so a plaintext value there can be found.
KNOWN: List[dict] = [
    {"name": "JIRA_BASE_URL", "group": "Jira", "label": "Site URL", "secret": False,
     "path": ("jira", "base_url"), "hint": "https://yourteam.atlassian.net"},
    {"name": "JIRA_EMAIL", "group": "Jira", "label": "Account email", "secret": False,
     "path": ("jira", "email"), "hint": "the Atlassian account the token belongs to"},
    {"name": "JIRA_API_TOKEN", "group": "Jira", "label": "API token", "secret": True,
     "path": ("jira", "token"), "hint": "id.atlassian.com → Security → Create and manage API tokens"},
    {"name": "GITLAB_BASE_URL", "group": "GitLab", "label": "Instance URL", "secret": False,
     "path": ("gitlab", "base_url"), "hint": "https://gitlab.com, or your own instance"},
    {"name": "GITLAB_TOKEN", "group": "GitLab", "label": "Personal access token", "secret": True,
     "path": ("gitlab", "token"), "hint": "scope: api — used by every GitLab repo without its own"},
    {"name": "GITHUB_BASE_URL", "group": "GitHub", "label": "API URL", "secret": False,
     "path": ("github", "base_url"), "hint": "https://api.github.com, or your Enterprise API"},
    {"name": "GITHUB_TOKEN", "group": "GitHub", "label": "Personal access token", "secret": True,
     "path": ("github", "token"), "hint": "scope: repo — used by every GitHub repo without its own"},
    {"name": "OPENROUTER_API_KEY", "group": "Models", "label": "OpenRouter API key", "secret": True,
     "path": ("openrouter", "api_key"), "hint": "only for runners that authenticate through OpenRouter"},
    {"name": "SISYPHUS_API_TOKEN", "group": "Daemon", "label": "API token", "secret": True,
     "path": ("api", "token"),
     "hint": "set = every caller off this machine must present it; empty = local use only"},
    {"name": "SISYPHUS_WEBHOOK_SECRET", "group": "Daemon", "label": "Webhook secret", "secret": True,
     "path": ("webhooks", "secret"), "hint": "validates forge webhooks that start autopilots"},
    {"name": "SISYPHUS_NOTIFY_URL", "group": "Daemon", "label": "Notification webhook URL", "secret": True,
     "path": ("notify", "webhook_url"), "hint": "failed or parked tasks are posted here"},
]
_KNOWN_BY_NAME = {k["name"]: k for k in KNOWN}

# Changing where a token is sent must not keep sending it: an address edit
# clears the token that goes with it, so the next one is typed by a person
# who saw the new address — not reused by whatever changed it.
_ADDRESS_GUARDS = {"JIRA_BASE_URL": ("JIRA_API_TOKEN",), "JIRA_EMAIL": ("JIRA_API_TOKEN",),
                   "GITLAB_BASE_URL": ("GITLAB_TOKEN",), "GITHUB_BASE_URL": ("GITHUB_TOKEN",)}

# A config line that names a credential by placeholder — a repo's own
# `token: ${MY_TOKEN}` included. Placeholders elsewhere (worker_env's ${PATH})
# are not credentials and never resolve from the vault.
_CRED_LINE = re.compile(
    r"^\s*(?:token|secret|api_key|password|email|base_url|webhook_url)\s*:\s*[\"']?\$\{(\w+)\}",
    re.MULTILINE)
_SECRET_KEYS = ("token", "secret", "api_key", "password")
_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")

_lock = threading.Lock()
_cache: Dict[str, Optional[str]] = {}


class CredentialError(ValueError):
    pass


# ── the vault, behind two functions tests replace ───────────────────────

def _kc_read(name: str) -> Optional[str]:
    from . import keychain
    return keychain.read(name, service=SERVICE)


def _kc_write(name: str, value: Optional[str]) -> None:
    from . import keychain
    if not keychain.available():
        raise CredentialError("the macOS keychain is not available to this process")
    if value is None:
        keychain.delete(name, service=SERVICE)
    else:
        try:
            keychain.store(name, value, service=SERVICE)
        except keychain.KeychainError as e:
            raise CredentialError(str(e)) from None


# ── names ───────────────────────────────────────────────────────────────

def repo_token_name(repo: str) -> str:
    """The vault name of *repo*'s own forge token: kmp-app → REPO_KMP_APP_TOKEN."""
    return "REPO_" + re.sub(r"[^A-Z0-9]+", "_", (repo or "").upper()).strip("_") + "_TOKEN"


def _raw_config() -> str:
    from .config import config_file
    try:
        return config_file().read_text()
    except OSError:
        return ""


def _repo_names(raw: str) -> List[str]:
    import yaml
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        return []
    return [r["name"] for r in (data.get("repos") or []) if isinstance(r, dict) and r.get("name")]


def managed_names(raw_config: Optional[str] = None) -> List[str]:
    """Every credential name: the known ones, one per repo, then any a config
    line refers to, in that order."""
    raw = _raw_config() if raw_config is None else raw_config
    names = [k["name"] for k in KNOWN]
    for n in [repo_token_name(r) for r in _repo_names(raw)] + _CRED_LINE.findall(raw or ""):
        if n not in names:
            names.append(n)
    return names


def is_managed(name: str) -> bool:
    return name in managed_names()


# ── lookup ──────────────────────────────────────────────────────────────

def stored(name: str) -> Optional[str]:
    """The vault's value for *name*, read once per process."""
    with _lock:
        if name in _cache:
            return _cache[name]
    value = _kc_read(name) or None
    with _lock:
        _cache[name] = value
    return value


def get(name: str) -> str:
    """The value for a credential, from the vault only. A name that is not a
    credential (a runner reading ANTHROPIC_API_KEY from launchd) still comes
    from the environment."""
    if name in managed_names():
        return stored(name) or ""
    return os.environ.get(name, "") or ""


def resolve_placeholder(name: str, managed: List[str]) -> Optional[str]:
    """What core.config substitutes for ``${name}``: the vault's value for a
    credential, the environment's for anything else, None to leave it."""
    if name in managed:
        return stored(name)
    return os.environ.get(name)


def worker_strip(raw_config: Optional[str] = None) -> List[str]:
    """Credential names a worker must not inherit from the daemon."""
    return managed_names(raw_config)


# ── writes ──────────────────────────────────────────────────────────────

def set_value(name: str, value: str) -> List[str]:
    """Keep *value* for *name* in the vault and make config see it now.
    Returns the names cleared because an address they go to changed."""
    name = (name or "").strip()
    value = (value or "").strip()
    if not _NAME_RE.match(name):
        raise CredentialError("a name is capitals, digits and underscores, like GITLAB_TOKEN")
    if not value:
        raise CredentialError("the value is empty — use Clear to remove one")
    before = stored(name)
    _kc_write(name, value)
    with _lock:
        _cache[name] = value
    cleared = []
    if before and before != value:
        for dep in _ADDRESS_GUARDS.get(name, ()):
            if stored(dep):
                _kc_write(dep, None)
                with _lock:
                    _cache[dep] = None
                cleared.append(dep)
    _reload()
    logger.info("credential %s saved to the vault", name)
    return cleared


def clear(name: str) -> None:
    _kc_write(name, None)
    with _lock:
        _cache[name] = None
    _reload()
    logger.info("credential %s removed from the vault", name)


def _reload() -> None:
    from .config import reload_config
    reload_config()


# ── what older setups left in files ─────────────────────────────────────

def _env_path() -> Path:
    from .env import _ROOT
    return _ROOT / ".env"


def _env_file_values() -> Dict[str, str]:
    p = _env_path()
    if not p.exists():
        return {}
    out = {}
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        if v:
            out[k.strip()] = v
    return out


def _config_plaintext(data: dict) -> List[dict]:
    """Credentials typed into config.yaml as values rather than placeholders:
    [{name, where, value}]."""
    found = []
    for k in KNOWN:
        block, key = k["path"]
        v = (data.get(block) or {}).get(key) if isinstance(data.get(block), dict) else None
        if isinstance(v, str) and v.strip() and not v.strip().startswith("${"):
            found.append({"name": k["name"], "where": f"{block}.{key}", "value": v.strip(),
                          "set": (block, key)})
    for i, r in enumerate(data.get("repos") or []):
        if not isinstance(r, dict):
            continue
        v = r.get("token")
        if isinstance(v, str) and v.strip() and not v.strip().startswith("${"):
            found.append({"name": repo_token_name(r.get("name", "")),
                          "where": f"repos[{r.get('name', i)}].token", "value": v.strip(),
                          "repo_index": i})
    return found


def leftovers() -> List[dict]:
    """Every credential still sitting in a file: [{name, where}], values
    withheld. Nothing reads these any more — they are only shown so they can
    be moved."""
    import yaml
    out = [{"name": n, "where": ".env"} for n in managed_names() if n in _env_file_values()]
    try:
        data = yaml.safe_load(_raw_config()) or {}
    except yaml.YAMLError:
        data = {}
    out += [{"name": f["name"], "where": "config.yaml " + f["where"]} for f in _config_plaintext(data)]
    return out


def move_leftovers() -> dict:
    """Move every credential found in .env or typed into config.yaml into the
    vault, and take it out of the file. Returns {moved: [...], failed: {name: why}}."""
    moved, failed = [], {}

    def keep(name, value):
        try:
            set_value(name, value)
            moved.append(name)
            return True
        except CredentialError as e:
            failed[name] = str(e)
            return False

    # config.yaml: replace the value with the placeholder that reads the vault.
    from .repos import _read_config_file, _write_config_file
    data = _read_config_file()
    changed = False
    for f in _config_plaintext(data):
        if not keep(f["name"], f["value"]):
            continue
        if "set" in f:
            block, key = f["set"]
            data[block][key] = "${" + f["name"] + "}"
        else:
            data["repos"][f["repo_index"]].pop("token", None)
        changed = True
    if changed:
        _write_config_file(data)

    # .env: take the line out.
    values = _env_file_values()
    env_moved = [n for n in managed_names() if n in values and keep(n, values[n])]
    if env_moved:
        p = _env_path()
        kept = [raw for raw in p.read_text().splitlines()
                if not ("=" in raw and not raw.lstrip().startswith("#")
                        and raw.split("=", 1)[0].strip() in env_moved)]
        p.write_text("\n".join(kept) + "\n")
        for name in env_moved:
            os.environ.pop(name, None)
        _reload()
    return {"moved": moved, "failed": failed}


# ── the page ────────────────────────────────────────────────────────────

def _masked(value: str) -> str:
    return "••••" + value[-4:] if len(value) > 8 else "••••"


# One card per category on the page. `needs` are the names that make the
# service usable (the status); an `optional` category is extra, not a gap.
GROUPS: List[dict] = [
    {"group": "Jira", "icon": "tracker", "kind": "Tracker",
     "about": "Import bugs as tasks, and let agents read, comment on and move cards.",
     "needs": ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"), "check": "jira"},
    {"group": "GitLab", "icon": "forge", "kind": "Forge",
     "about": "Publish merge requests and follow their pipelines. The URL defaults to gitlab.com.",
     "needs": ("GITLAB_TOKEN",)},
    {"group": "GitHub", "icon": "forge", "kind": "Forge",
     "about": "Publish pull requests and follow their checks. The URL defaults to api.github.com.",
     "needs": ("GITHUB_TOKEN",)},
    {"group": "Repos", "icon": "repos", "kind": "Per repo", "optional": True,
     "about": "A repo's own forge token, when it belongs to another account than the shared one."},
    {"group": "Models", "icon": "model", "kind": "Model provider", "optional": True,
     "about": "Only for runners that authenticate with a pasted OpenRouter key; sign-in runners need nothing."},
    {"group": "Daemon", "icon": "shield", "kind": "Access & alerts", "optional": True,
     "about": "Remote access to this daemon, forge webhooks, and where failure alerts go."},
]


def view() -> List[dict]:
    """One card per category: its fields and a status the page can colour."""
    repos = {repo_token_name(r): r for r in _repo_names(_raw_config())}
    rows: Dict[str, List[dict]] = {}
    for name in managed_names():
        meta = _KNOWN_BY_NAME.get(name)
        if not meta:
            repo = repos.get(name)
            meta = {"name": name, "group": "Repos", "secret": True,
                    "label": repo or name,
                    "hint": "without one, this repo uses the shared GitLab/GitHub token"
                    if repo else "named by a token: line in config.yaml"}
        value = stored(name) or ""
        rows.setdefault(meta["group"], []).append(
            {**meta, "set": bool(value),
             "shown": (value if not meta["secret"] else _masked(value)) if value else ""})
    cards = []
    for g in GROUPS:
        fields = rows.get(g["group"], [])
        if not fields:
            continue
        needs = [f for f in fields if f["name"] in g.get("needs", ())]
        have = sum(1 for f in fields if f["set"])
        if g.get("optional"):
            status, tone = (f"{have} set", "ok") if have else ("Not used", "idle")
        else:
            ready = sum(1 for f in needs if f["set"])
            if ready == len(needs):
                status, tone = "Connected", "ok"
            elif ready or have:
                status, tone = f"Incomplete · {ready}/{len(needs)}", "warn"
            else:
                status, tone = "Not set up", "idle"
        for f in fields:
            f["required"] = f["name"] in g.get("needs", ())
        cards.append({**g, "fields": fields, "status": status, "tone": tone,
                      "open": tone == "warn" or g["group"] == "Jira"})
    return cards
