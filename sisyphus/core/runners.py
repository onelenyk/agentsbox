"""AI Runner management — CRUD + health check.

Runners are AI backend configurations (opencode, Claude Code CLI). Each agent
references a runner by name. The launcher reads runner config to determine
which binary to spawn; how to actually drive that binary — argv, credential
variables, MCP wiring, tool naming — lives in core.runner_profiles, one entry
per supported type.
"""
import asyncio
import json
import logging
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from .db import get_conn
# Types that actually have a launch profile. Sourced from runner_profiles
# rather than listed separately: the previous hardcoded list advertised
# ollama/openai-api/custom, which the UI happily let you pick and which were
# then fed opencode's command line and OpenRouter's environment — menu entries
# that could never run. One list means adding a profile is what makes a type
# selectable.
from .runner_profiles import SUPPORTED_TYPES as RUNNER_TYPES
from .credentials import get as _credential

logger = logging.getLogger(__name__)


def validate_type(runner_type: str) -> str:
    """Normalise and check a runner type, or raise ValueError."""
    rtype = (runner_type or "").strip() or "opencode"
    if rtype not in RUNNER_TYPES:
        raise ValueError(
            f"unsupported runner type {rtype!r}; supported: {', '.join(RUNNER_TYPES)}"
        )
    return rtype


AUTH_MODES = ("login", "key", "env")


def auth_mode(runner: dict) -> str:
    """How *runner* authenticates: its CLI's own sign-in ("login"), a key kept
    in the keychain ("key"), or a variable from the daemon's environment
    ("env"). Rows from before the column read as what they always meant: a
    named variable is "env", nothing is the CLI's own login."""
    mode = ((runner or {}).get("auth_mode") or "").strip()
    if mode in AUTH_MODES:
        return mode
    return "env" if ((runner or {}).get("api_key_env") or "").strip() else "login"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_runners_table():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runners (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            binary TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'opencode',
            model TEXT NOT NULL DEFAULT '',
            api_key_env TEXT DEFAULT '',
            base_url TEXT DEFAULT '',
            is_default INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1,
            extra_args TEXT DEFAULT '[]',
            health_status TEXT DEFAULT 'unknown',
            health_checked_at TEXT,
            health_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            idle_timeout INTEGER,
            timeout INTEGER
        )
    """)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(runners)")}
    if "auth_mode" not in cols:
        conn.execute("ALTER TABLE runners ADD COLUMN auth_mode TEXT DEFAULT ''")
    # Per-runner worker timeouts (seconds). NULL means "use the type default"
    # — see core.runner_profiles.effective_timeouts for how they resolve.
    if "idle_timeout" not in cols:
        conn.execute("ALTER TABLE runners ADD COLUMN idle_timeout INTEGER")
    if "timeout" not in cols:
        conn.execute("ALTER TABLE runners ADD COLUMN timeout INTEGER")
    conn.commit()

    existing = conn.execute("SELECT COUNT(*) FROM runners").fetchone()[0]
    if existing == 0:
        _seed_default_runner(conn)
    conn.close()


def _seed_default_runner(conn):
    from core.config import get_config

    rid = f"runner-{uuid4().hex[:8]}"
    now = _now()
    conn.execute(
        """INSERT INTO runners (id, name, binary, type, model, api_key_env, base_url,
           is_default, enabled, extra_args, health_status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, '[]', 'unknown', ?, ?)""",
        (
            rid, "opencode-default", "opencode", "opencode",
            get_config("default_model", "deepseek"),
            "OPENROUTER_API_KEY",
            get_config("openrouter.base_url", "https://openrouter.ai/api/v1"),
            now, now,
        ),
    )
    conn.commit()
    logger.info("Seeded default runner: opencode-default (id=%s)", rid)


def add_runner(
    name: str,
    binary: str,
    runner_type: str = "opencode",
    model: str = "",
    api_key_env: str = "",
    base_url: str = "",
    extra_args: list = None,
    is_default: bool = False,
    auth_mode: str = "",
    idle_timeout: Optional[int] = None,
    timeout: Optional[int] = None,
) -> dict:
    runner_type = validate_type(runner_type)
    if auth_mode and auth_mode not in AUTH_MODES:
        raise ValueError(f"unknown sign-in mode {auth_mode!r}")
    rid = f"runner-{uuid4().hex[:8]}"
    now = _now()
    extra_json = json.dumps(extra_args or [])

    if is_default:
        _clear_default()

    conn = get_conn()
    conn.execute(
        """INSERT INTO runners (id, name, binary, type, model, api_key_env, base_url,
           is_default, enabled, extra_args, health_status, created_at, updated_at,
           auth_mode, idle_timeout, timeout)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 'unknown', ?, ?, ?, ?, ?)""",
        (rid, name, binary, runner_type, model, api_key_env, base_url,
         int(is_default), extra_json, now, now, auth_mode, idle_timeout, timeout),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM runners WHERE id = ?", (rid,)).fetchone()
    conn.close()
    return dict(row)


def get_runner(runner_id: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM runners WHERE id = ?", (runner_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_runner_by_name(name: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM runners WHERE name = ?", (name,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_default_runner() -> Optional[dict]:
    """The runner flagged default, else the oldest enabled one.

    Both queries share one connection, closed once at the end: closing after
    the first and then reusing it made the fallback path raise
    ProgrammingError("Cannot operate on a closed database") — reachable
    whenever an enabled runner exists but none is flagged default (e.g. the
    default one was deleted).
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM runners WHERE is_default = 1 AND enabled = 1 LIMIT 1"
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT * FROM runners WHERE enabled = 1 ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def resolve_runner(name: str = "") -> dict:
    """The runner row a launch with *name* will actually use.

    Same fallback chain the launcher applies (named row → default row → a
    last-resort opencode stub), exported so callers that must know the runner's
    type BEFORE launching — core.worker, which writes CLI-specific MCP config
    and names MCP tools in the prompt — can't drift out of step with what
    core.launcher.launch_agent independently resolves.
    """
    runner = get_runner_by_name(name) if name else None
    if not runner:
        runner = get_default_runner()
    if not runner:
        return {"binary": "opencode", "type": "opencode", "model": "",
                "api_key_env": "", "base_url": "", "extra_args": "[]"}
    return runner


def list_runners() -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM runners ORDER BY is_default DESC, name ASC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_runner(runner_id: str, **fields) -> dict:
    allowed = {
        "name", "binary", "type", "model", "api_key_env", "base_url",
        "enabled", "extra_args", "auth_mode", "idle_timeout", "timeout",
    }
    # The timeout columns are nullable: a None is a real value that clears a
    # row override back to "use the type default", so it must reach SQL (the
    # other fields' None means "leave alone").
    nullable = {"idle_timeout", "timeout"}
    updates = {k: v for k, v in fields.items()
               if k in allowed and (v is not None or k in nullable)}

    if "type" in updates:
        updates["type"] = validate_type(updates["type"])
    if updates.get("auth_mode") and updates["auth_mode"] not in AUTH_MODES:
        raise ValueError(f"unknown sign-in mode {updates['auth_mode']!r}")
    if "extra_args" in updates and isinstance(updates["extra_args"], list):
        updates["extra_args"] = json.dumps(updates["extra_args"])

    if not updates:
        return get_runner(runner_id)

    updates["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [runner_id]

    conn = get_conn()
    conn.execute(f"UPDATE runners SET {set_clause} WHERE id = ?", values)
    conn.commit()
    row = conn.execute("SELECT * FROM runners WHERE id = ?", (runner_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_runner(runner_id: str) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM runners WHERE id = ?", (runner_id,))
    conn.commit()
    conn.close()
    from . import keychain
    keychain.delete(runner_id)


def set_default_runner(runner_id: str) -> Optional[dict]:
    conn = get_conn()
    conn.execute("UPDATE runners SET is_default = 0")
    conn.execute("UPDATE runners SET is_default = 1, updated_at = ? WHERE id = ?",
                 (_now(), runner_id))
    conn.commit()
    row = conn.execute("SELECT * FROM runners WHERE id = ?", (runner_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _clear_default():
    conn = get_conn()
    conn.execute("UPDATE runners SET is_default = 0")
    conn.commit()
    conn.close()


def _version_note(runner: dict, binary_path: str) -> Optional[str]:
    """Whether the installed CLI is the version this daemon's launch flags were
    verified against (core.runner_profiles.VERIFIED_VERSIONS). A drifted version
    is a note, never an issue: these CLIs update weekly and nearly every update
    is harmless — but when runs start failing in ways that look like the model
    misbehaving, this is the line that says where to look first."""
    from . import runner_profiles

    rtype = (runner.get("type") or "").strip() or runner_profiles.DEFAULT_TYPE
    try:
        proc = subprocess.run([binary_path, "--version"], capture_output=True,
                              text=True, timeout=10)
    except Exception:  # noqa: BLE001 — a CLI that won't report a version is not unhealthy
        logger.debug("could not read %s --version", binary_path, exc_info=True)
        return None
    return runner_profiles.version_note(rtype, runner_profiles.parse_version(proc.stdout or proc.stderr))


async def health_check(runner_id: str) -> dict:
    runner = get_runner(runner_id)
    if not runner:
        return {"status": "error", "error": "Runner not found"}

    now = _now()
    # issues make a runner unhealthy; notes are worth showing but do not,
    # on their own, mean the runner cannot run.
    issues, notes = [], []

    # Resolve exactly the way the launcher does (core.runner_detect.resolve:
    # PATH plus the common CLI install dirs), NOT via bare shutil.which —
    # which reports "not found" for a binary the launcher then starts fine
    # under a minimal daemon PATH, i.e. health lying about a working system.
    from .runner_detect import resolve as _resolve_binary
    from .runner_profiles import credential_store

    binary = runner["binary"]
    resolved = _resolve_binary(binary)
    if not resolved:
        issues.append(f"Binary '{binary}' not found in PATH or the usual install dirs")
    else:
        note = await asyncio.to_thread(_version_note, runner, resolved)
        if note:
            notes.append(note)

    mode = auth_mode(runner)
    api_env = runner.get("api_key_env", "")
    if mode == "key":
        from . import keychain
        if not await asyncio.to_thread(keychain.read, runner["id"]):
            issues.append("its API key is missing from the keychain — paste it again in Edit")
    elif mode == "login" and resolved:
        from .runner_catalog import login_status
        rtype = (runner.get("type") or "").strip() or "opencode"
        st = await asyncio.to_thread(login_status, rtype, resolved)
        if not st["ok"]:
            issues.append(f"{st['summary']} — {st['detail']}")
    elif api_env and not _credential(api_env):
        # An unset variable is only fatal for a CLI with nowhere else to look.
        # Both supported CLIs log in for themselves, and the daemon's own
        # environment is nearly empty under launchd — so the variable being
        # absent here says nothing about whether a worker can authenticate.
        # This check used to flip such runners to "unhealthy" while they ran
        # perfectly well.
        store = credential_store((runner.get("type") or "").strip() or "opencode")
        if store:
            notes.append(f"env var '{api_env}' is not set — falling back to {store}")
        else:
            issues.append(f"Env var '{api_env}' is not set")

    base_url = runner.get("base_url", "")
    if base_url:
        try:
            loop = asyncio.get_running_loop()
            req = urllib.request.Request(base_url, method="HEAD")
            await loop.run_in_executor(
                None,
                lambda: urllib.request.urlopen(req, timeout=5),
            )
        except urllib.error.HTTPError as e:
            # An HTTP status IS a reachable server: the host resolved, the TLS
            # handshake finished, something answered. OpenRouter replies 404 to
            # a HEAD on /api/v1 — which flipped a runner that worked perfectly
            # to "unhealthy", the one word this page exists to get right. Only a
            # transport failure below means the URL cannot be reached at all.
            logger.debug("base URL %s answered HTTP %s to HEAD", base_url, e.code)
        except Exception as e:
            issues.append(f"Base URL '{base_url}' unreachable: {e}")

    status = "healthy" if not issues else "unhealthy"
    # One column carries both; the UI colours it by status, so a note on a
    # healthy runner reads as information rather than as a failure.
    error = "; ".join(issues) if issues else ("; ".join(notes) or None)

    conn = get_conn()
    conn.execute(
        "UPDATE runners SET health_status = ?, health_checked_at = ?, health_error = ?, updated_at = ? WHERE id = ?",
        (status, now, error, now, runner_id),
    )
    conn.commit()
    conn.close()

    return {"status": status, "error": error, "checked_at": now}
