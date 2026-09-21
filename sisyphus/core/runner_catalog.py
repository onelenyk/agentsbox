"""What a runner can run on, and whether it is signed in — for the runner form.

Both answers come from the CLI itself rather than from a list kept here, so the
form offers what the installed runner will actually accept:

  opencode    `opencode models` lists every model of every provider it holds
              credentials for; opencode's own models.dev snapshot
              (~/.cache/opencode/models.json) adds names, context, price and
              whether the model can call tools. Sign-in is the provider list in
              opencode's auth.json (names only — the secrets are never read).
  claude-cli  Claude Code takes an alias (fable, opus, sonnet, haiku) or a full
              model name; the same snapshot's anthropic entry supplies the
              names. Sign-in is `claude auth status`.

Everything degrades to "nothing known" rather than raising: a form that cannot
list models still lets you type one.
"""
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CATALOG_PATH = Path("~/.cache/opencode/models.json").expanduser()
OPENCODE_AUTH_PATH = Path("~/.local/share/opencode/auth.json").expanduser()

# Claude Code resolves these to the newest model of the family.
CLAUDE_ALIASES = ("fable", "opus", "sonnet", "haiku")

_TTL = 300.0
_cache: dict = {}


def _cached(key, fn):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _TTL:
        return hit[1]
    val = fn()
    _cache[key] = (time.monotonic(), val)
    return val


def clear_cache() -> None:
    _cache.clear()


def catalog() -> dict:
    """opencode's models.dev snapshot: {provider_id: {name, env, models}}, or {}."""
    try:
        return json.loads(CATALOG_PATH.read_text())
    except Exception:  # noqa: BLE001 — no snapshot just means no metadata
        return {}


def _ctx(n) -> str:
    if not n:
        return ""
    return f"{n // 1_000_000}M" if n >= 1_000_000 else f"{n // 1000}K"


def _price(cost: dict) -> str:
    if not cost or cost.get("input") is None:
        return ""
    i, o = cost.get("input") or 0, cost.get("output") or 0
    if not i and not o:
        return "free"
    return f"${i:g} / ${o:g}"


def _entry(model_id: str, meta: Optional[dict]) -> dict:
    meta = meta or {}
    return {
        "id": model_id,
        "name": meta.get("name") or model_id.split("/", 1)[-1],
        "ctx": _ctx((meta.get("limit") or {}).get("context")),
        "price": _price(meta.get("cost") or {}),
        "tools": meta.get("tool_call", True),
        "released": meta.get("release_date") or "",
    }


def _run(argv: list, timeout: int = 20) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except Exception:  # noqa: BLE001
        logger.debug("runner probe failed: %s", argv, exc_info=True)
        return None


def provider_of(model_id: str) -> str:
    return model_id.split("/", 1)[0] if "/" in (model_id or "") else ""


def provider_env(rtype: str, model_id: str) -> str:
    """The variable an API key for *model_id* must arrive in."""
    if rtype == "claude-cli":
        return "ANTHROPIC_API_KEY"
    env = (catalog().get(provider_of(model_id)) or {}).get("env") or []
    return env[0] if env else "OPENROUTER_API_KEY"


# ── models ──────────────────────────────────────────────────────────────

def _opencode_models(binary: str) -> list:
    proc = _run([binary, "models"])
    ids = [ln.strip() for ln in (proc.stdout if proc and proc.returncode == 0 else "").splitlines()
           if re.match(r"^[\w.-]+/\S+$", ln.strip())]
    cat = catalog()
    groups: dict = {}
    for mid in ids:
        pid, rest = mid.split("/", 1)
        prov = cat.get(pid) or {}
        e = _entry(mid, (prov.get("models") or {}).get(rest))
        e["name"] = e["name"] if (prov.get("models") or {}).get(rest) else rest
        if not e["tools"]:
            continue  # an agent that cannot call a tool cannot report back
        g = groups.setdefault(pid, {"id": pid, "label": prov.get("name") or pid, "models": []})
        g["models"].append(e)
    for g in groups.values():
        g["models"].sort(key=lambda m: m["released"], reverse=True)
    return list(groups.values())


def _claude_models() -> list:
    anth = (catalog().get("anthropic") or {}).get("models") or {}
    newest = sorted(((k, v) for k, v in anth.items() if not re.search(r"-\d{8}$", k)),
                    key=lambda kv: kv[1].get("release_date") or "", reverse=True)
    aliases = []
    for a in CLAUDE_ALIASES:
        hit = next((kv for kv in newest if kv[0].startswith(f"claude-{a}-")), None)
        e = _entry(a, hit[1] if hit else None)
        e["name"] = f"{a} → {hit[1]['name']}" if hit else a
        aliases.append(e)
    groups = [{"id": "alias", "label": "Latest of each family", "models": aliases}]
    if newest:
        groups.append({"id": "anthropic", "label": "Pinned versions",
                       "models": [_entry(k, v) for k, v in newest]})
    return groups


def models(rtype: str, binary: str) -> list:
    """[{id, label, models: [{id, name, ctx, price}]}] the runner accepts."""
    if rtype == "claude-cli":
        return _cached(("models", rtype), _claude_models)
    return _cached(("models", rtype, binary), lambda: _opencode_models(binary))


def find(groups: list, model_id: str) -> Optional[dict]:
    for g in groups:
        for m in g["models"]:
            if m["id"] == model_id:
                return m
    return None


# ── sign-in ─────────────────────────────────────────────────────────────

def _claude_login(binary: str) -> dict:
    proc = _run([binary, "auth", "status"], timeout=15)
    try:
        st = json.loads(proc.stdout) if proc else {}
    except ValueError:
        st = {}
    if st.get("loggedIn"):
        who = st.get("email") or st.get("orgName") or "signed in"
        return {"ok": True, "summary": f"Signed in as {who}",
                "detail": f"via {st.get('authMethod') or 'login'}"}
    return {"ok": False, "summary": "Not signed in",
            "detail": f"run `{os.path.basename(binary)} auth login` in a terminal"}


def _opencode_login() -> dict:
    try:
        creds = json.loads(OPENCODE_AUTH_PATH.read_text())
    except Exception:  # noqa: BLE001
        creds = {}
    cat = catalog()
    names = [(cat.get(p) or {}).get("name") or p for p in creds]
    if names:
        return {"ok": True, "summary": f"{len(names)} provider{'s' if len(names) != 1 else ''} signed in",
                "detail": ", ".join(names), "providers": list(creds)}
    return {"ok": False, "summary": "No providers signed in",
            "detail": "run `opencode auth login` in a terminal", "providers": []}


def login_status(rtype: str, binary: str) -> dict:
    """{ok, summary, detail} for the runner's own sign-in."""
    if rtype == "claude-cli":
        return _cached(("login", rtype, binary), lambda: _claude_login(binary))
    return _cached(("login", rtype), _opencode_login)


def version(binary: str) -> str:
    from .runner_profiles import parse_version

    def probe():
        proc = _run([binary, "--version"], timeout=10)
        return parse_version((proc.stdout or proc.stderr) if proc else "")
    return _cached(("version", binary), probe)
