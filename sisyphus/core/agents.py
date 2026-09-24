"""Agents — a worker's persona + tool allowlist + which CLI runtime runs it.

Promotes the old hardcoded roles (core/roles.py) into a configurable, DB-backed
entity, mirroring core/runners.py. An Agent is:

    { name, persona, tools[], runner_id?, model, kind, enabled, is_builtin }

  - persona   : system-prompt text describing the agent.
  - tools     : allowlist — a subset of the agent-MCP tool catalog
                (web/agent_mcp.py TOOLS). The token-scoped server enforces it.
  - runner_id : which CLI runtime (runners.id) executes it; NULL = default runner.
  - model     : optional per-agent model override.
  - kind      : "worker" (does the task) or "orchestrator" (decides — Phase 2).

The built-in worker agents (explorer/engineer/reviewer) are seeded from the
legacy roles dict so behaviour is unchanged during the migration.
"""
import json
import logging
from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4

from .db import get_conn

logger = logging.getLogger(__name__)

AGENT_KINDS = ("worker", "orchestrator")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_agents_table() -> None:
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agents (
            id         TEXT PRIMARY KEY,
            name       TEXT NOT NULL UNIQUE,
            persona    TEXT NOT NULL DEFAULT '',
            tools      TEXT NOT NULL DEFAULT '[]',   -- json array of tool names
            runner_id  TEXT,                          -- NULL = default runner
            model      TEXT NOT NULL DEFAULT '',
            kind       TEXT NOT NULL DEFAULT 'worker',
            enabled    INTEGER NOT NULL DEFAULT 1,
            is_builtin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    # Which model class drives the agent (core.model_classes); blank = its kind.
    if "model_class" not in {r[1] for r in conn.execute("PRAGMA table_info(agents)")}:
        conn.execute("ALTER TABLE agents ADD COLUMN model_class TEXT NOT NULL DEFAULT ''")
    conn.commit()
    if conn.execute("SELECT COUNT(*) FROM agents WHERE kind = 'worker'").fetchone()[0] == 0:
        _seed_workers(conn)
    # Ensure the orchestrator agent exists even on a DB seeded before it did
    # (Phase 1 → Phase 2 upgrade) — without it, _decide has no agent to run.
    if conn.execute("SELECT COUNT(*) FROM agents WHERE kind = 'orchestrator'").fetchone()[0] == 0:
        _seed_orchestrator(conn)
    else:
        _refresh_orchestrator_persona(conn)
    _ensure_pinned_workers(conn)
    _append_builtin_agent_tools(conn)
    conn.close()


# Agents the CODE pins by name, as opposed to ones a decider may choose. Missing
# one is not a matter of taste: core.reviews creates a task pinned to
# 'mr-reviewer' for every opened MR, and on a database where nobody had created
# that agent by hand the task could never run — the review simply never
# happened. Same reasoning as _seed_orchestrator, which is why this is a
# create-if-absent and never an update: a persona someone has tuned is theirs.
PINNED_BY_CODE = ("mr-reviewer",)


def _ensure_pinned_workers(conn) -> None:
    from .roles import ROLES

    now = _now()
    for name in PINNED_BY_CODE:
        spec = ROLES.get(name)
        if not spec:
            continue
        if conn.execute("SELECT 1 FROM agents WHERE name = ?", (name,)).fetchone():
            continue
        conn.execute(
            """INSERT INTO agents (id, name, persona, tools, runner_id, model,
               kind, enabled, is_builtin, created_at, updated_at)
               VALUES (?, ?, ?, ?, NULL, '', 'worker', 1, 1, ?, ?)""",
            (f"agent-{uuid4().hex[:8]}", name, spec["persona"],
             json.dumps(spec["tools"]), now, now),
        )
    conn.commit()


def _append_builtin_agent_tools(conn) -> None:
    """Append new tools defined in ROLES to built-in agents, never removing
    existing ones. This carries tool improvements from code changes onto
    existing databases — a persona tune is a user's; a tool-list upgrade is
    a code improvement, like _refresh_orchestrator_persona."""
    from .roles import ROLES

    for agent in conn.execute("SELECT id, name, tools FROM agents WHERE is_builtin = 1").fetchall():
        role_spec = ROLES.get(agent["name"])
        if not role_spec:
            continue
        current = json.loads(agent["tools"] or "[]")
        target = role_spec["tools"]
        # Only append tools that are in ROLES but missing from the agent
        missing = [t for t in target if t not in current]
        if missing:
            new_tools = current + missing
            conn.execute(
                "UPDATE agents SET tools = ?, updated_at = ? WHERE id = ?",
                (json.dumps(new_tools), _now(), agent["id"])
            )
    conn.commit()


def _seed_workers(conn) -> None:
    """Seed the built-in worker agents from the legacy roles dict — same
    personas and tool allowlists, so existing behaviour is preserved."""
    from .roles import ROLES

    now = _now()
    for name, spec in ROLES.items():
        conn.execute(
            """INSERT INTO agents (id, name, persona, tools, runner_id, model,
               kind, enabled, is_builtin, created_at, updated_at)
               VALUES (?, ?, ?, ?, NULL, '', 'worker', 1, 1, ?, ?)""",
            (f"agent-{uuid4().hex[:8]}", name, spec["persona"],
             json.dumps(spec["tools"]), now, now),
        )
    conn.commit()


def _refresh_orchestrator_persona(conn) -> None:
    """Carry a new built-in orchestrator persona onto an existing database.

    Personas live in rows, seeded once, so a persona improvement in the code
    never reached a database that already had one — the decider kept following
    last release's instructions (the "answer instead of shipping a document"
    rule would have been dead on arrival). Only a persona that still matches
    one this project shipped is replaced; a tuned one is its owner's.
    """
    import hashlib

    from .orchestrator import ORCHESTRATOR_PERSONA, SHIPPED_PERSONAS

    row = conn.execute(
        "SELECT id, persona FROM agents WHERE name = 'orchestrator' AND is_builtin = 1").fetchone()
    if not row:
        return
    stored = row["persona"] or ""
    if stored == ORCHESTRATOR_PERSONA:
        return
    if hashlib.sha256(stored.encode()).hexdigest() not in SHIPPED_PERSONAS:
        logger.info("orchestrator persona was edited locally — leaving it alone")
        return
    conn.execute("UPDATE agents SET persona = ?, updated_at = ? WHERE id = ?",
                 (ORCHESTRATOR_PERSONA, _now(), row["id"]))
    conn.commit()
    logger.info("orchestrator persona updated to the current built-in")


def _seed_orchestrator(conn) -> None:
    """Seed the orchestrator agent — its decisions are tool calls, so no direct
    model HTTP. Persona + tool list come from core.orchestrator."""
    from .orchestrator import ORCHESTRATOR_PERSONA, ORCHESTRATOR_TOOLS

    now = _now()
    conn.execute(
        """INSERT INTO agents (id, name, persona, tools, runner_id, model,
           kind, enabled, is_builtin, created_at, updated_at)
           VALUES (?, 'orchestrator', ?, ?, NULL, '', 'orchestrator', 1, 1, ?, ?)""",
        (f"agent-{uuid4().hex[:8]}", ORCHESTRATOR_PERSONA,
         json.dumps(ORCHESTRATOR_TOOLS), now, now),
    )
    conn.commit()


def _row(row) -> Optional[dict]:
    if row is None:
        return None
    d = dict(row)
    d["tools"] = json.loads(d.get("tools") or "[]")
    return d


def list_agents(kind: Optional[str] = None) -> List[dict]:
    conn = get_conn()
    if kind:
        rows = conn.execute(
            "SELECT * FROM agents WHERE kind = ? ORDER BY is_builtin DESC, name ASC", (kind,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM agents ORDER BY is_builtin DESC, name ASC"
        ).fetchall()
    conn.close()
    return [_row(r) for r in rows]


def get_agent(name: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM agents WHERE name = ?", (name,)).fetchone()
    conn.close()
    return _row(row)


def agent_tools(name: str) -> List[str]:
    a = get_agent(name)
    return list(a["tools"]) if a else []


def add_agent(name: str, persona: str = "", tools: Optional[list] = None,
              runner_id: Optional[str] = None, model: str = "",
              kind: str = "worker") -> dict:
    if kind not in AGENT_KINDS:
        raise ValueError(f"invalid kind {kind!r}; must be one of {AGENT_KINDS}")
    now = _now()
    aid = f"agent-{uuid4().hex[:8]}"
    conn = get_conn()
    conn.execute(
        """INSERT INTO agents (id, name, persona, tools, runner_id, model,
           kind, enabled, is_builtin, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)""",
        (aid, name, persona, json.dumps(tools or []), runner_id, model, kind, now, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM agents WHERE id = ?", (aid,)).fetchone()
    conn.close()
    return _row(row)


def update_agent(name: str, **fields) -> Optional[dict]:
    allowed = {"persona", "tools", "runner_id", "model", "kind", "enabled", "model_class"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if "tools" in updates and isinstance(updates["tools"], list):
        updates["tools"] = json.dumps(updates["tools"])
    if "kind" in updates and updates["kind"] not in AGENT_KINDS:
        raise ValueError(f"invalid kind {updates['kind']!r}")
    if not updates:
        return get_agent(name)
    updates["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [name]
    conn = get_conn()
    conn.execute(f"UPDATE agents SET {set_clause} WHERE name = ?", values)
    conn.commit()
    conn.close()
    return get_agent(name)


def delete_agent(name: str) -> bool:
    """Delete a non-builtin agent. Built-ins are protected (they are the
    orchestrator's known roster and the migration's source of truth)."""
    conn = get_conn()
    row = conn.execute("SELECT is_builtin FROM agents WHERE name = ?", (name,)).fetchone()
    if row is None:
        conn.close()
        return False
    if row["is_builtin"]:
        conn.close()
        raise ValueError(f"cannot delete built-in agent {name!r}")
    conn.execute("DELETE FROM agents WHERE name = ?", (name,))
    conn.commit()
    conn.close()
    return True
