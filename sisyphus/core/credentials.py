"""Tokens, keys and the addresses that go with them — entered in the UI, kept
in the macOS login keychain.

config.yaml names every credential as a ``${VAR}`` placeholder (jira.token:
${JIRA_API_TOKEN}). Those used to be filled from ``.env``, a plaintext file
next to the code that every worker can read. They are now filled from here
first: the Integrations page writes a value into the keychain, core.config
substitutes it on its next load, and nothing touches a file.

Resolution for one name, first hit wins:

  1. the keychain (service ``sisyphus-secret``, account = the variable name)
  2. the daemon's environment — the shell, launchd, or a leftover ``.env``

so an existing ``.env`` keeps working until its values are moved over
(``import_env_file``, a button on the page).

Values resolved here are deliberately NOT put into ``os.environ``: a worker
inherits the daemon's environment, and an agent has no business holding the
Jira token. ``worker_strip`` names what the launcher removes for the same
reason when a value did come from the environment.
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
# show the value back; an address is not a secret, a token is.
KNOWN: List[dict] = [
    {"name": "JIRA_BASE_URL", "group": "Jira", "label": "Site URL", "secret": False,
     "hint": "https://yourteam.atlassian.net"},
    {"name": "JIRA_EMAIL", "group": "Jira", "label": "Account email", "secret": False,
     "hint": "the Atlassian account the token belongs to"},
    {"name": "JIRA_API_TOKEN", "group": "Jira", "label": "API token", "secret": True,
     "hint": "id.atlassian.com → Security → Create and manage API tokens"},
    {"name": "GITLAB_BASE_URL", "group": "GitLab", "label": "Instance URL", "secret": False,
     "hint": "https://gitlab.com, or your own instance"},
    {"name": "GITLAB_TOKEN", "group": "GitLab", "label": "Personal access token", "secret": True,
     "hint": "scope: api"},
    {"name": "GITHUB_BASE_URL", "group": "GitHub", "label": "API URL", "secret": False,
     "hint": "https://api.github.com, or your Enterprise API"},
    {"name": "GITHUB_TOKEN", "group": "GitHub", "label": "Personal access token", "secret": True,
     "hint": "scope: repo"},
    {"name": "OPENROUTER_API_KEY", "group": "Models", "label": "OpenRouter API key", "secret": True,
     "hint": "only for runners that authenticate through OpenRouter"},
    {"name": "SISYPHUS_API_TOKEN", "group": "Daemon", "label": "API token", "secret": True,
     "hint": "set = every caller off this machine must present it; empty = local use only"},
    {"name": "SISYPHUS_WEBHOOK_SECRET", "group": "Daemon", "label": "Webhook secret", "secret": True,
     "hint": "validates forge webhooks that start autopilots"},
    {"name": "SISYPHUS_NOTIFY_URL", "group": "Daemon", "label": "Notification webhook URL", "secret": True,
     "hint": "failed or parked tasks are posted here"},
]
_KNOWN_BY_NAME = {k["name"]: k for k in KNOWN}

# A config line that carries a credential — a repo's own `token: ${MY_TOKEN}`
# included. Placeholders elsewhere (worker_env's ${PATH}) are not credentials
# and are never looked up in the keychain.
_CRED_LINE = re.compile(
    r"^\s*(?:token|secret|api_key|password|email|base_url|webhook_url)\s*:\s*[\"']?\$\{(\w+)\}",
    re.MULTILINE)
_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")

_lock = threading.Lock()
_cache: Dict[str, Optional[str]] = {}


class CredentialError(ValueError):
    pass


# ── the keychain, behind two functions tests replace ────────────────────

def _backend_enabled() -> bool:
    return os.environ.get("SISYPHUS_KEYCHAIN", "on") != "off"


def _kc_read(name: str) -> Optional[str]:
    if not _backend_enabled():
        return None
    from . import keychain
    return keychain.read(name, service=SERVICE)


def _kc_write(name: str, value: Optional[str]) -> None:
    from . import keychain
    if not _backend_enabled():
        raise CredentialError("the keychain is switched off for this process")
    if value is None:
        keychain.delete(name, service=SERVICE)
    else:
        try:
            keychain.store(name, value, service=SERVICE)
        except keychain.KeychainError as e:
            raise CredentialError(str(e)) from None


# ── lookup ──────────────────────────────────────────────────────────────

def managed_names(raw_config: Optional[str] = None) -> List[str]:
    """Every credential name: the known ones, then any a config line refers
    to (a repo's own token), in that order."""
    if raw_config is None:
        from .config import config_file
        try:
            raw_config = config_file().read_text()
        except OSError:
            raw_config = ""
    names = [k["name"] for k in KNOWN]
    for n in _CRED_LINE.findall(raw_config or ""):
        if n not in names:
            names.append(n)
    return names


def stored(name: str) -> Optional[str]:
    """The keychain's value for *name*, read once per process."""
    with _lock:
        if name in _cache:
            return _cache[name]
    value = _kc_read(name) or None
    with _lock:
        _cache[name] = value
    return value


def get(name: str) -> str:
    """The value for *name*: keychain, then environment, else ""."""
    return stored(name) or os.environ.get(name, "") or ""


def resolve_placeholder(name: str, managed: List[str]) -> Optional[str]:
    """What core.config substitutes for ``${name}``: the keychain value for a
    credential, else the environment, else None (left as the placeholder)."""
    if name in managed:
        v = stored(name)
        if v:
            return v
    return os.environ.get(name)


def source(name: str) -> str:
    """Where *name*'s value comes from: keychain | env_file | environment | ""."""
    if stored(name):
        return "keychain"
    if os.environ.get(name):
        return "env_file" if name in _env_file_values() else "environment"
    return ""


def worker_strip(raw_config: Optional[str] = None) -> List[str]:
    """Credential names a worker must not inherit from the daemon."""
    return managed_names(raw_config)


# ── writes ──────────────────────────────────────────────────────────────

def set_value(name: str, value: str) -> None:
    """Keep *value* for *name* in the keychain and make config see it now."""
    name = (name or "").strip()
    value = (value or "").strip()
    if not _NAME_RE.match(name):
        raise CredentialError("a name is capitals, digits and underscores, like GITLAB_TOKEN")
    if not value:
        raise CredentialError("the value is empty — use Clear to remove one")
    _kc_write(name, value)
    with _lock:
        _cache[name] = value
    _reload()
    logger.info("credential %s saved to the keychain", name)


def clear(name: str) -> None:
    """Remove *name* from the keychain. An environment value, if any, shows
    through again — the page says so."""
    _kc_write(name, None)
    with _lock:
        _cache[name] = None
    _reload()
    logger.info("credential %s removed from the keychain", name)


def _reload() -> None:
    from .config import reload_config
    reload_config()


# ── the .env file, on its way out ───────────────────────────────────────

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


def env_file_pending() -> List[str]:
    """Credential names that still have a value in .env."""
    managed = managed_names()
    return [n for n in managed if n in _env_file_values()]


def import_env_file() -> dict:
    """Move every credential in .env into the keychain and take its line out
    of the file. Returns {moved: [...], failed: {name: why}}."""
    managed = managed_names()
    values = _env_file_values()
    moved, failed = [], {}
    for name in managed:
        if name not in values:
            continue
        try:
            set_value(name, values[name])
            moved.append(name)
        except CredentialError as e:
            failed[name] = str(e)
    if moved:
        p = _env_path()
        lines = []
        for raw in p.read_text().splitlines():
            key = raw.split("=", 1)[0].strip() if "=" in raw and not raw.lstrip().startswith("#") else ""
            if key in moved:
                lines.append(f"# {key} — moved to the keychain; edit it on the Integrations page")
            else:
                lines.append(raw)
        p.write_text("\n".join(lines) + "\n")
        for name in moved:
            os.environ.pop(name, None)
        _reload()
    return {"moved": moved, "failed": failed}


# ── the page ────────────────────────────────────────────────────────────

def _masked(value: str) -> str:
    return "••••" + value[-4:] if len(value) > 8 else "••••"


def view() -> List[dict]:
    """Rows for the page, grouped in KNOWN's order; config-only names last."""
    rows = []
    for name in managed_names():
        meta = _KNOWN_BY_NAME.get(name) or {
            "name": name, "group": "From config.yaml", "label": name, "secret": True,
            "hint": "a repo's own credential"}
        value = get(name)
        rows.append({
            **meta,
            "set": bool(value),
            "source": source(name),
            "shown": (value if not meta["secret"] else _masked(value)) if value else "",
        })
    groups: Dict[str, List[dict]] = {}
    for r in rows:
        groups.setdefault(r["group"], []).append(r)
    return [{"group": g, "rows": rs} for g, rs in groups.items()]


def is_managed(name: str) -> bool:
    return name in managed_names()
