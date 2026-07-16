import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)
_scheduler = None

def _cleanup_job():
    try:
        from core.worktree import cleanup_orphans, REPOS_DIR
        if REPOS_DIR.exists():
            for repo_path in REPOS_DIR.iterdir():
                if repo_path.is_dir() and (repo_path / "HEAD").exists():
                    cleanup_orphans(str(repo_path))
        logger.info("Scheduler: orphan cleanup complete")
    except Exception as e:
        logger.warning(f"Scheduler cleanup failed: {e}")

def _process_backlog():
    """Scan for pending cards and auto-run them."""
    import asyncio
    from core.state import list_cards
    from core.operations import run_card

    try:
        cards = list_cards()
        pending = [c for c in cards if c["status"] in ("backlog", "created")]
        for card in pending:
            try:
                asyncio.run(run_card(card["id"]))
                logger.info("Scheduler: auto-ran card %s", card["id"])
            except Exception as exc:
                logger.warning("Scheduler: failed to run card %s: %s", card["id"], exc)
    except Exception as exc:
        logger.warning("Scheduler: backlog scan failed: %s", exc)


def start_scheduler():
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(_cleanup_job, IntervalTrigger(minutes=60), id="orphan-cleanup", replace_existing=True)
    _scheduler.add_job(_process_backlog, IntervalTrigger(seconds=10), id="process-backlog", replace_existing=True)
    _scheduler.start()
    logger.info("APScheduler started with %d jobs", len(_scheduler.get_jobs()))
    return _scheduler

def get_scheduler():
    return _scheduler
