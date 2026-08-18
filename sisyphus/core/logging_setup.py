"""Configure app logging to a rotating file under logs/, alongside the console.

Called once at daemon/CLI startup. Captures every ``logging.getLogger(__name__)``
across core/ so there is a persistent, size-bounded record instead of only
whatever scrolled past in a terminal.
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_configured = False


def setup_logging() -> Path:
    """Idempotent. Returns the log file path.

    The directory follows ``SISYPHUS_LOG_DIR`` when set, otherwise ``logs/``
    beside the source. The override exists because the path was derived from
    the module's own location, so a daemon started with an isolated
    ``SISYPHUS_DB`` and ``WORKSPACE_ROOT`` — which the browser end-to-end test
    is careful to do — still wrote into the real daemon's log. Its startup lines
    then read as unexplained restarts of the running daemon, and cost a real
    debugging detour chasing a database that had never split.
    """
    global _configured
    root = Path(__file__).resolve().parent.parent
    override = os.environ.get("SISYPHUS_LOG_DIR", "").strip()
    log_dir = Path(override) if override else root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "sisyphus.log"
    if _configured:
        return log_file
    _configured = True

    # Under pytest, importing web.app would otherwise pour every test's logging
    # into the real app log — keep it clean.
    if "pytest" in sys.modules:
        return log_file

    try:
        from core.config import get_config
        level_name = str(get_config("log_level", "info")).upper()
    except Exception:
        level_name = "INFO"
    level = getattr(logging, level_name, logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    if not any(isinstance(h, RotatingFileHandler) for h in root_logger.handlers):
        handler = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root_logger.addHandler(handler)
    return log_file
