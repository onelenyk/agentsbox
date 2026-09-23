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
# 2 = the Mission Planner's tables retired with the planner (2026-09-23).
# 3 = the v1 pipeline's and the copilot's tables, unread since v2 (2026-09-23).
# 4 = subscription limits, model classes, usage.provider, agents.model_class
#     (2026-09-23).
CURRENT_VERSION = 4

# Tables of features that are gone. Dropped on upgrade so a database does not
# keep carrying — and a reader keep wondering about — data nothing reads.
RETIRED_TABLES = (
    "plans", "plan_items", "plan_messages",                        # Mission Planner
    "cards", "runs", "steps", "resources", "activities",           # v1 pipeline
    "copilot_sessions", "copilot_messages",                        # v1 copilot chat
)


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
    from core.limits import init_limits_table
    from core.model_classes import init_classes_table

    before = db_version()
    init_db()
    init_runners_table()
    init_agents_table()
    init_usage_table()
    init_mr_table()
    init_autopilot_table()
    init_limits_table()
    init_classes_table()
    conn = get_conn()
    try:
        for table in RETIRED_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute(f"PRAGMA user_version = {int(CURRENT_VERSION)}")
        conn.commit()
    finally:
        conn.close()
    if before != CURRENT_VERSION:
        logger.info("migrations: schema %d -> %d", before, CURRENT_VERSION)
    return CURRENT_VERSION
