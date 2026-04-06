"""
Scheduler manager for the Gideon pipeline.

:class:`SchedulerManager` wraps APScheduler's
:class:`~apscheduler.schedulers.background.BackgroundScheduler` and provides:

- Configuration loading from ``config.yaml`` (with safe defaults).
- Automatic job registration for the two scheduled tasks (cleanup + report).
  The three manual tasks (scrape/analyze/generate) are triggered via the
  web dashboard and do NOT run on a schedule.
- Test-mode support with short intervals (seconds/minutes) for rapid
  verification without waiting for daily or hourly windows.
- A listener that emits structured log lines for every job execution or error.
- ``start / stop / pause / resume`` lifecycle controls.
- A ``get_job_info()`` helper used by the CLI commands.

Schedule (production)
---------------------
+-----------------------------+----------------------------------+
| Task                        | Default trigger                  |
+=============================+==================================+
| scrape_jobs                 | cron at ``scrape_time`` (09:00)  |
+-----------------------------+----------------------------------+
| analyze_new_jobs            | interval every 2 hours           |
+-----------------------------+----------------------------------+
| generate_resumes            | cron daily at 10:00              |
+-----------------------------+----------------------------------+
| cleanup_old_jobs            | cron Sunday 00:00                |
+-----------------------------+----------------------------------+
| daily_report                | cron daily at 20:00              |
+-----------------------------+----------------------------------+

Schedule (test mode)
--------------------
+-----------------------------+----------------------------------+
| Task                        | Test trigger                     |
+=============================+==================================+
| scrape_jobs                 | interval every 5 minutes         |
+-----------------------------+----------------------------------+
| analyze_new_jobs            | interval every 2 minutes         |
+-----------------------------+----------------------------------+
| generate_resumes            | interval every 3 minutes         |
+-----------------------------+----------------------------------+
| cleanup_old_jobs            | interval every 10 minutes        |
+-----------------------------+----------------------------------+
| daily_report                | interval every 5 minutes         |
+-----------------------------+----------------------------------+
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults (used when config.yaml is missing or a key is absent)
# ---------------------------------------------------------------------------
_DEFAULTS: Dict[str, Any] = {
    "test_mode": False,
    "scrape_time": "09:00",
    "auto_generate_threshold": 35.0,
    "cleanup_days": 30,
    "search_configs": [
        {"keywords": "python developer", "location": "San Francisco", "max_results": 20}
    ],
}


def _load_config(config_path: str = "config.yaml") -> Dict[str, Any]:
    """Load the ``scheduler`` section from *config_path*.

    Returns safe defaults if the file is absent or the section is missing.
    """
    try:
        import yaml  # type: ignore[import-untyped]

        with open(config_path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        cfg = raw.get("scheduler", {})
        logger.debug("Scheduler config loaded from %s.", config_path)
        return cfg
    except FileNotFoundError:
        logger.warning(
            "Config file %r not found – using built-in defaults.", config_path
        )
        return {}
    except Exception as exc:
        logger.warning("Could not load %r (%s) – using defaults.", config_path, exc)
        return {}


class SchedulerManager:
    """Manage all scheduled pipeline tasks via APScheduler.

    Args:
        config_path: Path to ``config.yaml``.  Can be absolute or relative to
                     the current working directory.

    Example::

        manager = SchedulerManager()
        manager.start()

        # ... application runs ...

        manager.stop()
    """

    def __init__(self, config_path: str = "config.yaml") -> None:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR
        from scheduler.logging_config import attach_scheduler_log_handler

        attach_scheduler_log_handler()

        self._scheduler = BackgroundScheduler(
            job_defaults={
                "coalesce": True,       # Merge missed runs into one
                "max_instances": 1,     # Never run the same job twice concurrently
                "misfire_grace_time": 300,  # Allow up to 5-min late start
            }
        )
        self._config = {**_DEFAULTS, **_load_config(config_path)}
        self._is_running: bool = False

        self._scheduler.add_listener(
            self._on_job_event,
            EVENT_JOB_EXECUTED | EVENT_JOB_ERROR,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Register all jobs and start the background scheduler thread."""
        if self._is_running:
            logger.warning("[scheduler] Already running – ignoring start().")
            return
        self._register_jobs()
        self._scheduler.start()
        self._is_running = True
        logger.info("[scheduler] Started (test_mode=%s).", self._config["test_mode"])

    def stop(self, wait: bool = True) -> None:
        """Stop the scheduler and (optionally) wait for running jobs to finish.

        Args:
            wait: Block until all currently executing jobs complete.
        """
        if not self._is_running:
            logger.warning("[scheduler] Not running – ignoring stop().")
            return
        self._scheduler.shutdown(wait=wait)
        self._is_running = False
        logger.info("[scheduler] Stopped.")

    def pause(self) -> None:
        """Pause all jobs (they will not fire until :meth:`resume` is called)."""
        self._scheduler.pause()
        logger.info("[scheduler] Paused.")

    def resume(self) -> None:
        """Resume all paused jobs."""
        self._scheduler.resume()
        logger.info("[scheduler] Resumed.")

    @property
    def is_running(self) -> bool:
        """``True`` if the scheduler background thread is active."""
        return self._is_running

    # ------------------------------------------------------------------
    # Status query
    # ------------------------------------------------------------------

    def get_job_info(self) -> List[Dict[str, Any]]:
        """Return a list of dicts describing all registered jobs.

        Each dict contains: ``id``, ``name``, ``next_run``, ``trigger``.
        """
        info = []
        for job in self._scheduler.get_jobs():
            info.append(
                {
                    "id": job.id,
                    "name": job.name,
                    "next_run": job.next_run_time,
                    "trigger": str(job.trigger),
                }
            )
        return info

    # ------------------------------------------------------------------
    # Job registration
    # ------------------------------------------------------------------

    def _register_jobs(self) -> None:
        """Add pipeline tasks to APScheduler based on settings.json.

        Always scheduled:
          - cleanup_old_jobs  (weekly Sunday 00:00)
          - daily_report      (daily 20:00)

        Conditionally scheduled (based on data/settings.json):
          - scrape_jobs_task    — only when mode = 'automatic'
          - generate_resumes_task — only when mode = 'automatic'

        analyze_new_jobs is always manual (triggered after scrape).
        """
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        from scheduler.tasks import (
            cleanup_old_jobs_task,
            daily_report_task,
            scrape_jobs_task,
            generate_resumes_task,
        )

        test_mode: bool = bool(self._config.get("test_mode", False))
        cleanup_days: int = int(self._config.get("cleanup_days", 30))

        # Load user settings for conditional scheduling
        try:
            from web.settings_manager import SettingsManager
            sm = SettingsManager()
        except Exception as exc:
            logger.warning("[scheduler] Could not load SettingsManager (%s) — treating all as manual.", exc)
            sm = None

        if test_mode:
            logger.info("[scheduler] TEST MODE – using short intervals.")
            cleanup_trigger = IntervalTrigger(minutes=10)
            report_trigger  = IntervalTrigger(minutes=5)
        else:
            cleanup_trigger = CronTrigger(day_of_week="sun", hour=0, minute=0)
            report_trigger  = CronTrigger(hour=20, minute=0)

        self._scheduler.add_job(
            func=cleanup_old_jobs_task,
            args=[cleanup_days],
            trigger=cleanup_trigger,
            id="cleanup_old_jobs",
            name="Cleanup Old Jobs",
            replace_existing=True,
        )

        self._scheduler.add_job(
            func=daily_report_task,
            trigger=report_trigger,
            id="daily_report",
            name="Daily Report",
            replace_existing=True,
        )

        scheduled_count = 2

        # Conditionally schedule scrape
        scrape_mode = sm.get_mode("scrape") if sm else "manual"
        if scrape_mode == "automatic":
            scrape_time = sm.get_schedule("scrape") if sm else "09:00"
            if test_mode:
                scrape_trigger = IntervalTrigger(minutes=5)
            else:
                hour, minute = map(int, scrape_time.split(":"))
                scrape_trigger = CronTrigger(hour=hour, minute=minute)
            search_configs = self._config.get("search_configs", [
                {"keywords": "python developer", "location": "San Francisco", "max_results": 20}
            ])
            self._scheduler.add_job(
                func=scrape_jobs_task,
                args=[search_configs],
                trigger=scrape_trigger,
                id="scrape_jobs",
                name="Scrape Jobs",
                replace_existing=True,
            )
            scheduled_count += 1
            logger.info("[scheduler] scrape_jobs_task scheduled at %s", scrape_time)
        else:
            logger.info("[scheduler] scrape_jobs_task is in manual mode — not scheduled.")

        # Conditionally schedule generate
        generate_mode = sm.get_mode("generate") if sm else "manual"
        if generate_mode == "automatic":
            gen_time = sm.get_schedule("generate") if sm else "10:00"
            if test_mode:
                gen_trigger = IntervalTrigger(minutes=3)
            else:
                hour, minute = map(int, gen_time.split(":"))
                gen_trigger = CronTrigger(hour=hour, minute=minute)
            threshold = float(self._config.get("auto_generate_threshold", 35.0))
            self._scheduler.add_job(
                func=generate_resumes_task,
                args=[threshold],
                trigger=gen_trigger,
                id="generate_resumes",
                name="Generate Resumes",
                replace_existing=True,
            )
            scheduled_count += 1
            logger.info("[scheduler] generate_resumes_task scheduled at %s", gen_time)
        else:
            logger.info("[scheduler] generate_resumes_task is in manual mode — not scheduled.")

        logger.info(
            "[scheduler] Registered %d automatic task(s) (test_mode=%s). "
            "scrape/analyze/generate manual tasks can also be triggered via dashboard Pipeline Controls.",
            scheduled_count,
            test_mode,
        )

    def reschedule_task(self, task_name: str) -> None:
        """Re-evaluate and update APScheduler registration for *task_name*.

        Reads the current mode from settings.json:
        - If ``'automatic'``: adds/replaces the job in the scheduler.
        - If ``'manual'``: removes the job from the scheduler (if present).

        Safe to call when the scheduler is not running — logs a warning
        and returns without raising.

        Args:
            task_name: ``'scrape'`` or ``'generate'``.
        """
        if not self._is_running:
            logger.warning(
                "[scheduler] reschedule_task(%r) called but scheduler is not running — skipping.",
                task_name,
            )
            return

        from apscheduler.triggers.cron import CronTrigger

        try:
            from web.settings_manager import SettingsManager
            sm = SettingsManager()
        except Exception as exc:
            logger.warning("[scheduler] reschedule_task: SettingsManager unavailable (%s).", exc)
            return

        job_id_map = {"scrape": "scrape_jobs", "generate": "generate_resumes"}
        job_id = job_id_map.get(task_name)
        if job_id is None:
            logger.warning("[scheduler] reschedule_task: unknown task %r", task_name)
            return

        # Remove existing job (if any)
        try:
            self._scheduler.remove_job(job_id)
            logger.info("[scheduler] Removed job %r from scheduler.", job_id)
        except Exception:
            pass  # job wasn't scheduled — that's fine

        mode = sm.get_mode(task_name)
        if mode != "automatic":
            logger.info("[scheduler] %r is now manual — not re-adding to scheduler.", task_name)
            return

        # Re-add the job
        from scheduler.tasks import scrape_jobs_task, generate_resumes_task

        schedule = sm.get_schedule(task_name)
        hour, minute = map(int, schedule.split(":"))
        trigger = CronTrigger(hour=hour, minute=minute)

        if task_name == "scrape":
            search_configs = self._config.get("search_configs", [
                {"keywords": "python developer", "location": "San Francisco", "max_results": 20}
            ])
            self._scheduler.add_job(
                func=scrape_jobs_task,
                args=[search_configs],
                trigger=trigger,
                id=job_id,
                name="Scrape Jobs",
                replace_existing=True,
            )
        else:
            threshold = float(self._config.get("auto_generate_threshold", 35.0))
            self._scheduler.add_job(
                func=generate_resumes_task,
                args=[threshold],
                trigger=trigger,
                id=job_id,
                name="Generate Resumes",
                replace_existing=True,
            )

        logger.info(
            "[scheduler] %r rescheduled as automatic at %s.", task_name, schedule
        )

    # ------------------------------------------------------------------
    # APScheduler event listener
    # ------------------------------------------------------------------

    def _on_job_event(self, event: Any) -> None:
        """Handle APScheduler job-executed and job-error events."""
        if event.exception:
            logger.error(
                "[scheduler] Job %r FAILED: %s", event.job_id, event.exception
            )
            try:
                from scheduler.notifications import NotificationService
                NotificationService().send_error_notification(
                    event.job_id, str(event.exception)
                )
            except Exception:
                pass  # Never crash the scheduler thread over a notification error
        else:
            # The return value is the TaskResult from the task function
            result = event.retval
            if result is not None and hasattr(result, "to_dict"):
                logger.info(
                    "[scheduler] Job %r OK – duration=%.1fs  data=%s  errors=%d",
                    event.job_id,
                    result.duration_seconds,
                    result.data,
                    len(result.errors),
                )
                if result.errors:
                    for err in result.errors:
                        logger.warning("[scheduler]   Task warning: %s", err)
            else:
                logger.info("[scheduler] Job %r executed successfully.", event.job_id)
