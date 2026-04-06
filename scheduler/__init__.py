"""
Scheduler package – APScheduler-based task automation (Phase 6).

Public exports
--------------
:class:`~scheduler.scheduler.SchedulerManager`
    Start / stop / query the background scheduler.

:class:`~scheduler.notifications.NotificationService`
    Send console and optional email notifications.

:mod:`~scheduler.tasks`
    Individual task functions (scrape, analyze, generate, cleanup, report).
"""

from scheduler.logging_config import attach_scheduler_log_handler
from scheduler.notifications import NotificationService
from scheduler.scheduler import SchedulerManager

__all__ = ["SchedulerManager", "NotificationService", "attach_scheduler_log_handler"]
