"""Unit tests for scheduler.py: start_scheduler, get_scheduler, and job registration."""

import core.scheduler
from core.scheduler import start_scheduler, get_scheduler


def _reset_scheduler():
    """Reset the global _scheduler singleton between tests."""
    core.scheduler._scheduler = None


def _shutdown(scheduler):
    """Safely shutdown scheduler, ignoring SchedulerNotRunningError."""
    try:
        scheduler.shutdown(wait=False)
    except Exception:
        pass


class TestStartScheduler:
    def test_first_call_returns_scheduler_with_two_jobs(self):
        _reset_scheduler()
        scheduler = None
        try:
            scheduler = start_scheduler()
            assert scheduler is not None

            jobs = scheduler.get_jobs()
            assert len(jobs) == 2

            job_ids = {job.id for job in jobs}
            assert job_ids == {"orphan-cleanup", "process-backlog"}

            assert scheduler.running is True
        finally:
            if scheduler is not None:
                _shutdown(scheduler)

    def test_second_call_returns_same_instance(self):
        _reset_scheduler()
        scheduler = None
        try:
            s1 = start_scheduler()
            s2 = start_scheduler()
            assert s1 is s2
            scheduler = s1
        finally:
            if scheduler is not None:
                _shutdown(scheduler)

    def test_orphan_cleanup_has_interval_trigger_60_minutes(self):
        _reset_scheduler()
        scheduler = None
        try:
            scheduler = start_scheduler()
            job = scheduler.get_job("orphan-cleanup")
            assert job is not None
            assert job.trigger.__class__.__name__ == "IntervalTrigger"
            assert job.trigger.interval_length == 3600.0  # 60 minutes in seconds
        finally:
            if scheduler is not None:
                _shutdown(scheduler)

    def test_process_backlog_has_interval_trigger_10_seconds(self):
        _reset_scheduler()
        scheduler = None
        try:
            scheduler = start_scheduler()
            job = scheduler.get_job("process-backlog")
            assert job is not None
            assert job.trigger.__class__.__name__ == "IntervalTrigger"
            assert job.trigger.interval_length == 10.0  # 10 seconds
        finally:
            if scheduler is not None:
                _shutdown(scheduler)


class TestGetScheduler:
    def test_before_start_returns_none(self):
        _reset_scheduler()
        assert get_scheduler() is None

    def test_after_start_returns_scheduler_instance(self):
        _reset_scheduler()
        scheduler = None
        try:
            started = start_scheduler()
            result = get_scheduler()
            assert result is started
            assert result is not None
            scheduler = started
        finally:
            if scheduler is not None:
                _shutdown(scheduler)
