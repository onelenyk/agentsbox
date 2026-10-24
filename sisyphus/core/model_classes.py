"""Model classes — which runner and model drive an agent, chosen at spawn.

An agent's role (engineer, reviewer, …) says what it does; its class says what
drives it. Two classes, named after the two kinds of agent:

  orchestrator  plans and decides — worth a strong model, used a little.
  worker        does the volume of the work — a fast, cheap model, used a lot.

Each class is an ordered list of options, each a runner plus a model. A spawn
takes the first option whose provider still has room in its subscription
windows (core.limits), so when one plan runs dry the work moves to the next
without anyone switching runners by hand. When every option is spent the
spawn gets no runner, and the caller parks the task until a window resets.

An agent reaches its class by its kind unless it names another one. An agent
pinned to a runner of its own keeps that pin — an explicit choice wins — and a
class with no options falls back to the default runner, which is how every
agent ran before classes existed.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .db import get_conn

CLASSES = ("orchestrator", "worker")


@dataclass
class Choice:
    runner: Optional[dict]          # None: nothing usable right now
    model: str = ""
    provider: str = ""
    source: str = ""                # "class" | "pin" | "default"
    skipped: list = field(default_factory=list)   # [(label, why)]
    until: Optional[datetime] = None               # when the first option frees up

    @property
    def runner_name(self) -> Optional[str]:
        return self.runner["name"] if self.runner else None

    def why_none(self, cls: str) -> str:
        parts = "; ".join(f"{label} {why}" for label, why in self.skipped) or "no options"
        when = f" — the first frees up at {self.until:%H:%M UTC %b %d}" if self.until else ""
        return f"no runner in class {cls!r} is usable ({parts}){when}"


class NoRunner(Exception):
    """Every option in an agent's class is at its limit (or unusable)."""

    def __init__(self, cls: str, choice: "Choice"):
        super().__init__(choice.why_none(cls))
        self.cls = cls
        self.choice = choice


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_classes_table() -> None:
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS model_classes (
            name        TEXT PRIMARY KEY,
            options     TEXT NOT NULL DEFAULT '[]',
            updated_at  TEXT NOT NULL
        )
    """)
    for name in CLASSES:
        conn.execute("INSERT OR IGNORE INTO model_classes (name, options, updated_at) "
                     "VALUES (?, '[]', ?)", (name, _now()))
    conn.commit()
    conn.close()


def _decode(row) -> dict:
    try:
        options = json.loads(row["options"] or "[]")
    except ValueError:
        options = []
    return {"name": row["name"], "updated_at": row["updated_at"],
            "options": [o for o in options if isinstance(o, dict) and o.get("runner_id")]}


def list_classes() -> list:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM model_classes").fetchall()
    conn.close()
    found = {r["name"]: _decode(r) for r in rows}
    return [found.get(n, {"name": n, "options": [], "updated_at": ""}) for n in CLASSES]


def get_class(name: str) -> dict:
    return next((c for c in list_classes() if c["name"] == name),
                {"name": name, "options": [], "updated_at": ""})


def set_options(name: str, options: list) -> dict:
    if name not in CLASSES:
        raise ValueError(f"unknown model class {name!r}; one of {CLASSES}")
    clean = [{"runner_id": str(o["runner_id"]), "model": str(o.get("model") or "").strip()}
             for o in options if isinstance(o, dict) and o.get("runner_id")]
    conn = get_conn()
    conn.execute("INSERT INTO model_classes (name, options, updated_at) VALUES (?, ?, ?) "
                 "ON CONFLICT(name) DO UPDATE SET options = excluded.options, "
                 "updated_at = excluded.updated_at",
                 (name, json.dumps(clean), _now()))
    conn.commit()
    conn.close()
    return get_class(name)


def class_of(agent: dict) -> str:
    cls = (agent.get("model_class") or "").strip()
    return cls if cls in CLASSES else (agent.get("kind") or "worker")


def _label(runner: dict, model: str) -> str:
    return f"{runner['name']}/{model or runner.get('model') or 'default model'}"


def choose(agent_or_name, skip: int = 0) -> Choice:
    """The runner and model *agent* should run on right now.

    *skip* >= 1 skips the first *skip* usable class options — the orchestrator's
    escalation ladder calls choose(role, skip=1) after a role's runs keep ending
    without a result, so the next attempt lands on a DIFFERENT runner than the
    one that failed. A pin or the default runner has no options to skip, so it
    is returned unchanged (an explicit choice wins)."""
    from . import agents, limits
    from .runners import get_default_runner, get_runner

    agent = agents.get_agent(agent_or_name) if isinstance(agent_or_name, str) else agent_or_name
    agent = agent or {}

    if agent.get("runner_id"):
        r = get_runner(agent["runner_id"])
        if r:
            model = agent.get("model") or ""
            return Choice(r, model, limits.provider_of(r, model), "pin")

    options = get_class(class_of(agent))["options"]
    if not options:
        r = get_default_runner()
        model = agent.get("model") or ""
        return Choice(r, model, limits.provider_of(r, model) if r else "", "default")

    skipped, until, used = [], None, 0
    for opt in options:
        r = get_runner(opt["runner_id"])
        if not r:
            skipped.append((opt["runner_id"], "is gone"))
            continue
        model = opt.get("model") or ""
        if not r.get("enabled", 1):
            skipped.append((_label(r, model), "is disabled"))
            continue
        provider = limits.provider_of(r, model)
        blocked = limits.blocked_until(provider)
        if blocked:
            why = ("was set aside after a failed run" if limits.cooldown_until(provider)
                   else f"is at its {provider} limit")
            skipped.append((_label(r, model), why))
            until = blocked if until is None or blocked < until else until
            continue
        if used < skip:
            used += 1
            skipped.append((_label(r, model),
                            "skipped — switching runners after repeated failures"))
            continue
        return Choice(r, model, provider, "class", skipped)
    return Choice(None, source="class", skipped=skipped, until=until)



CLASS_ROLES = {
    "orchestrator": "plans and decides — a strong model, used a little",
    "worker": "does the work — a fast model, used a lot",
}


def view() -> list:
    """Each class with its options in order, each option's state now, and
    which agents it drives."""
    from . import agents, limits
    from .runners import get_runner
    everyone = agents.list_agents()
    out = []
    for cls in list_classes():
        options, in_use = [], None
        for i, opt in enumerate(cls["options"]):
            r = get_runner(opt["runner_id"])
            model = opt.get("model") or ""
            row = {"index": i, "runner_id": opt["runner_id"], "model": model,
                   "runner": r["name"] if r else "(deleted runner)",
                   "shown_model": model or (r.get("model") if r else "") or "its default model",
                   "provider": "", "account": "", "state": "ready", "until": ""}
            if not r:
                row["state"] = "gone"
            elif not r.get("enabled", 1):
                row["state"] = "disabled"
            else:
                row["provider"] = limits.provider_of(r, model)
                row["account"] = _account_label(r, row["provider"])
                row["shown_model"] = (model.split("/", 1)[1] if "/" in model
                                      else row["shown_model"])
                blocked = limits.blocked_until(row["provider"])
                if blocked:
                    cooling = limits.cooldown_until(row["provider"])
                    row["state"] = "cooldown" if cooling and cooling >= blocked else "limit"
                    row["until"] = blocked.isoformat()
            if row["state"] == "ready" and in_use is None:
                in_use = i
            options.append(row)
        for row in options:
            row["in_use"] = row["index"] == in_use
        out.append({
            "name": cls["name"], "role": CLASS_ROLES.get(cls["name"], ""),
            "options": options, "exhausted": bool(options) and in_use is None,
            "agents": [a["name"] for a in everyone
                       if not a.get("runner_id") and class_of(a) == cls["name"]],
            "pinned": [a["name"] for a in everyone
                       if a.get("runner_id") and class_of(a) == cls["name"]],
        })
    return out


def move(name: str, index: int, delta: int) -> dict:
    options = get_class(name)["options"]
    j = index + delta
    if 0 <= index < len(options) and 0 <= j < len(options):
        options[index], options[j] = options[j], options[index]
    return set_options(name, options)


def remove(name: str, index: int) -> dict:
    options = get_class(name)["options"]
    if 0 <= index < len(options):
        options.pop(index)
    return set_options(name, options)


def add(name: str, runner_id: str, model: str = "") -> dict:
    return set_options(name, get_class(name)["options"] + [{"runner_id": runner_id, "model": model}])


# ── accounts: what a class option can be drawn from ─────────────────────
# A runner is a CLI; an account is what pays for a run. opencode holds keys for
# several providers at once (DeepSeek, OpenCode Go, OpenRouter, …), and its
# models already reach every one of them — but the form offered a flat list of
# ~450 model ids under one runner name, with nothing saying which account each
# spends or whether that account still has room. An account here is one
# (runner, provider) pair: Claude for the claude-cli runner, one per provider
# prefix for opencode.

def account_key(runner_id: str, provider: str) -> str:
    return f"{runner_id}|{provider}"


def _account_of(runner: dict, model: str) -> str:
    from . import limits
    return account_key(runner["id"], limits.provider_of(runner, model))


def _account_label(runner: dict, provider: str, group_label: str = "") -> str:
    from . import limits
    if (runner.get("type") or "") == "claude-cli":
        return {"claude": "Claude subscription", "anthropic": "Anthropic API"}.get(provider, "Claude")
    if group_label:
        return group_label
    from . import runner_catalog
    return (runner_catalog.catalog().get(provider) or {}).get("name") \
        or limits.PROVIDER_LABELS.get(provider, (provider, ""))[0] or provider


def accounts(runners: Optional[list] = None) -> list:
    """Every account an enabled runner can spend, with its models and state:
    [{key, runner_id, runner, provider, label, signed_in, models, n_models,
      state, until, used_by}]. Never raises: a runner whose catalog cannot be
    read shows up with no models, and its option can still be typed."""
    from . import limits, runner_catalog
    from .runners import list_runners
    runners = list_runners() if runners is None else runners
    used: dict = {}
    for cls in list_classes():
        for i, opt in enumerate(cls["options"]):
            r = next((x for x in runners if x["id"] == opt["runner_id"]), None)
            if r:
                used.setdefault(_account_of(r, opt.get("model") or ""), []).append(
                    f"{cls['name']} #{i + 1}")
    out = []
    for r in runners:
        if not r.get("enabled", 1):
            continue
        rtype = r.get("type") or "opencode"
        try:
            groups = runner_catalog.models(rtype, r.get("binary") or "")
        except Exception:  # noqa: BLE001
            groups = []
        try:
            login = runner_catalog.login_status(rtype, r.get("binary") or "")
        except Exception:  # noqa: BLE001
            login = {"ok": False}
        if rtype == "claude-cli":
            provider = limits.provider_of(r)
            buckets = [(provider, "", [m for g in groups for m in g["models"]])]
            signed = {provider} if login.get("ok") else set()
        else:
            buckets = [(g["id"], g["label"], g["models"]) for g in groups]
            signed = set(login.get("providers") or [])
        for provider, glabel, models in buckets:
            until = limits.blocked_until(provider)
            cooling = limits.cooldown_until(provider)
            key = account_key(r["id"], provider)
            out.append({
                "key": key, "runner_id": r["id"], "runner": r["name"],
                "provider": provider, "label": _account_label(r, provider, glabel),
                # No readable auth.json says nothing about opencode's keys —
                # only a list that leaves this provider out is a "no".
                "signed_in": provider in signed if signed else rtype != "claude-cli",
                "models": models, "n_models": len(models),
                "state": ("cooldown" if until and cooling and cooling >= until
                          else "limit" if until else "ready"),
                "until": until.isoformat() if until else "",
                "used_by": used.get(key, []),
            })
    # Accounts in use first, then the rest by name — the ones that matter now
    # should not sit under 370 OpenRouter models.
    out.sort(key=lambda a: (not a["used_by"], a["label"].lower()))
    return out


def split_account(value: str) -> tuple:
    """(runner_id, provider) from an account key; a bare runner id is accepted
    too, as the form used to send."""
    runner_id, _, provider = (value or "").partition("|")
    return runner_id, provider
