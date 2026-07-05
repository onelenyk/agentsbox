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

def start_scheduler():
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(_cleanup_job, IntervalTrigger(minutes=60), id="orphan-cleanup", replace_existing=True)
    _scheduler.start()
    logger.info("APScheduler started with %d jobs", len(_scheduler.get_jobs()))
    return _scheduler

def get_scheduler():
    return _scheduler
