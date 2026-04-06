"""Logging configuration for the scheduler package.

Attaches a dedicated :class:`~logging.handlers.RotatingFileHandler` to the
``scheduler`` logger so all scheduler activity is captured in
``logs/scheduler.log`` (separate from the root ``logs/app.log``).

The handler is added only once (guarded by a flag on the logger) so repeated
imports or CLI invocations do not register duplicate handlers.

Usage::

    from scheduler.logging_config import attach_scheduler_log_handler
    attach_scheduler_log_handler()          # call once at startup
"""

import logging
import logging.handlers
import os
from pathlib import Path

_SCHEDULER_LOG = Path("logs") / "scheduler.log"
_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_BACKUP_COUNT = 5
_FMT = logging.Formatter(
    fmt="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Sentinel attribute name to detect whether we already attached the handler
_ATTACHED_ATTR = "_scheduler_log_handler_attached"


def attach_scheduler_log_handler(
    log_path: str | Path = _SCHEDULER_LOG,
    max_bytes: int = _MAX_BYTES,
    backup_count: int = _BACKUP_COUNT,
) -> None:
    """Attach a rotating file handler to the ``scheduler`` logger.

    Safe to call multiple times — the handler is only added once per process.

    Args:
        log_path: Path to the scheduler log file.
        max_bytes: Maximum file size before rotation (default 10 MB).
        backup_count: Number of backup files to keep (default 5).
    """
    scheduler_logger = logging.getLogger("scheduler")

    if getattr(scheduler_logger, _ATTACHED_ATTR, False):
        return  # already configured in this process

    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    handler = logging.handlers.RotatingFileHandler(
        filename=log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(_FMT)

    scheduler_logger.addHandler(handler)
    # Keep propagate=True so messages still reach the root logger (app.log +
    # console). The dedicated handler just writes an additional copy to
    # scheduler.log for easy per-subsystem monitoring.
    setattr(scheduler_logger, _ATTACHED_ATTR, True)

    scheduler_logger.debug(
        "Scheduler log handler attached → %s  (%.0f MB / %d backups)",
        log_path.resolve(),
        max_bytes / 1_048_576,
        backup_count,
    )
