"""Schema versioning — one stamp over the ad-hoc, idempotent migrations.

Every table and column change today is additive and self-guarding (CREATE
TABLE IF NOT EXISTS, ALTER TABLE behind a PRAGMA table_info check), which is
why the daemon has never needed a version number. The number exists for the
day that stops being true: PRAGMA user_version records which generation of
the schema last ran, so a future non-additive migration has something to
compare against, and a restored old snapshot can be upgraded on the spot
instead of waiting for the next daemon restart.

migrate_all() is the whole surface: run every init (each one carries its own
guarded ALTERs), then stamp. Idempotent by construction — calling it on a
current database is a no-op plus a stamp.
"""
import logging

logger = logging.getLogger(__name__)

# Bump when the set of known migrations grows. 1 = every schema change up to
# and including 2026-09 (16 guarded ALTERs across store/planner/mergerequests).
CURRENT_VERSION = 1


def db_version() -> int:
    """The stamped schema generation of the current database (0 = never
    stamped — a pre-versioning database)."""
    from core.db import get_conn
    conn = get_conn()
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def migrate_all() -> int:
    """Bring the database at core.db.DB_PATH up to CURRENT_VERSION: run every
    init function in startup order (each is idempotent and carries its own
    guarded ALTERs), then stamp. Returns the version stamped."""
    from core.db import init_db, get_conn
    from core.runners import init_runners_table
    from core.agents import init_agents_table
    from core.usage import init_usage_table
    from core.mergerequests import init_mr_table
    from core.autopilot import init_autopilot_table
    from core.copilot_store import init_copilot_tables
    from core.planner import init_planner_tables

    before = db_version()
    init_db()
    init_runners_table()
    init_agents_table()
    init_usage_table()
    init_mr_table()
    init_autopilot_table()
    init_copilot_tables()
    init_planner_tables()
    conn = get_conn()
    try:
        conn.execute(f"PRAGMA user_version = {int(CURRENT_VERSION)}")
        conn.commit()
    finally:
        conn.close()
    if before != CURRENT_VERSION:
        logger.info("migrations: schema %d -> %d", before, CURRENT_VERSION)
    return CURRENT_VERSION
