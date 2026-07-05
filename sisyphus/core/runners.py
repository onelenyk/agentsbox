"""AI Runner management — CRUD + health check.

Runners are AI backend configurations (opencode, Claude CLI, Ollama, etc.).
Each agent references a runner by name. The launcher reads runner config
to determine which binary to spawn and how to configure its environment.
"""
import asyncio
import json
import logging
import os
import shutil
import urllib.request
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from .db import get_conn

logger = logging.getLogger(__name__)

RUNNER_TYPES = ("opencode", "claude-cli", "ollama", "openai-api", "custom")


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
            updated_at TEXT NOT NULL
        )
    """)
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
) -> dict:
    rid = f"runner-{uuid4().hex[:8]}"
    now = _now()
    extra_json = json.dumps(extra_args or [])

    if is_default:
        _clear_default()

    conn = get_conn()
    conn.execute(
        """INSERT INTO runners (id, name, binary, type, model, api_key_env, base_url,
           is_default, enabled, extra_args, health_status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 'unknown', ?, ?)""",
        (rid, name, binary, runner_type, model, api_key_env, base_url,
         int(is_default), extra_json, now, now),
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
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM runners WHERE is_default = 1 AND enabled = 1 LIMIT 1"
    ).fetchone()
    conn.close()
    if row:
        return dict(row)
    row = conn.execute(
        "SELECT * FROM runners WHERE enabled = 1 ORDER BY created_at ASC LIMIT 1"
    ).fetchone()
    conn.close()
    return dict(row) if row else None


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
        "enabled", "extra_args",
    }
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}

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


async def health_check(runner_id: str) -> dict:
    runner = get_runner(runner_id)
    if not runner:
        return {"status": "error", "error": "Runner not found"}

    now = _now()
    issues = []

    binary = runner["binary"]
    if not shutil.which(binary):
        issues.append(f"Binary '{binary}' not found in PATH")
    else:
        pass

    api_env = runner.get("api_key_env", "")
    if api_env and not os.environ.get(api_env):
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
        except Exception as e:
            issues.append(f"Base URL '{base_url}' unreachable: {e}")

    status = "healthy" if not issues else "unhealthy"
    error = "; ".join(issues) if issues else None

    conn = get_conn()
    conn.execute(
        "UPDATE runners SET health_status = ?, health_checked_at = ?, health_error = ?, updated_at = ? WHERE id = ?",
        (status, now, error, now, runner_id),
    )
    conn.commit()
    conn.close()

    return {"status": status, "error": error, "checked_at": now}
