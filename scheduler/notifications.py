"""
Notification service for the scheduler pipeline.

Supports two channels:

1. **Console** – always active; writes a Rich-formatted summary to stdout via
   ``logging`` so the output appears in both the terminal and any log files.

2. **Email** – optional; activated by setting the environment variable
   ``EMAIL_NOTIFICATIONS_ENABLED=true`` plus the SMTP credentials below.

Environment variables
---------------------
``EMAIL_NOTIFICATIONS_ENABLED``
    Set to ``true`` to send real emails (default: ``false``).
``SMTP_SERVER``
    SMTP host (default: ``smtp.gmail.com``).
``SMTP_PORT``
    SMTP port (default: ``587``).
``SENDER_EMAIL``
    From-address used for outgoing messages.
``SENDER_PASSWORD``
    Password / app-password for SMTP authentication.
``RECIPIENT_EMAIL``
    Destination address for all notifications.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class NotificationService:
    """Send notifications about job pipeline activity.

    All public methods are safe to call even if email is disabled or
    credentials are missing – they fall back to console-only logging.

    Args:
        email_enabled: Override the ``EMAIL_NOTIFICATIONS_ENABLED`` env var.
    """

    def __init__(self, email_enabled: Optional[bool] = None) -> None:
        if email_enabled is not None:
            self._email_enabled = email_enabled
        else:
            self._email_enabled = (
                os.getenv("EMAIL_NOTIFICATIONS_ENABLED", "false").lower() == "true"
            )

        self._smtp_server: str = os.getenv("SMTP_SERVER", "smtp.gmail.com")
        self._smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
        self._sender_email: Optional[str] = os.getenv("SENDER_EMAIL")
        self._sender_password: Optional[str] = os.getenv("SENDER_PASSWORD")
        self._recipient_email: Optional[str] = os.getenv("RECIPIENT_EMAIL")

    # ------------------------------------------------------------------
    # Public notification methods
    # ------------------------------------------------------------------

    def send_new_jobs_notification(
        self, jobs: List[Dict[str, Any]], threshold: float = 0.0
    ) -> None:
        """Notify about newly discovered jobs (optionally filtered by score).

        Args:
            jobs: List of job dicts with at least ``title``, ``company``,
                  ``location``, and optionally ``match_score``.
            threshold: Only include jobs with ``match_score >= threshold``.
        """
        filtered = [
            j for j in jobs
            if float(j.get("match_score", 0)) >= threshold
        ] if threshold > 0 else jobs

        if not filtered:
            return

        subject = f"[JobScraper] {len(filtered)} new job match(es) found"
        lines = [f"Found {len(filtered)} new job(s):\n"]
        for j in filtered[:20]:
            score_str = (
                f"  Score: {j['match_score']:.1f}%"
                if "match_score" in j else ""
            )
            lines.append(
                f"- {j.get('title', '?')} at {j.get('company', '?')}"
                f"  ({j.get('location', '-')}){score_str}"
            )
        body = "\n".join(lines)

        logger.info("[notifications] New jobs: %s", body.replace("\n", " | "))
        self._send_email(subject, body)

    def send_error_notification(self, task_name: str, error_message: str) -> None:
        """Notify about a task failure.

        Args:
            task_name:     Name of the failing task.
            error_message: Error detail string.
        """
        subject = f"[JobScraper] Task failed: {task_name}"
        body = (
            f"The scheduled task '{task_name}' encountered an error:\n\n"
            f"{error_message}"
        )
        logger.error("[notifications] Task error in %s: %s", task_name, error_message)
        self._send_email(subject, body)

    def send_daily_report(self, report: Dict[str, Any]) -> None:
        """Send the daily summary report.

        Args:
            report: Dict produced by :func:`~scheduler.tasks.daily_report_task`.
                    Expected keys: ``date``, ``new_jobs``, ``analyzed_today``,
                    ``resumes_generated_today``, ``top_jobs``.
        """
        date_str = report.get("date", "today")
        new_jobs = report.get("new_jobs", 0)
        analyzed = report.get("analyzed_today", 0)
        resumes = report.get("resumes_generated_today", 0)
        top_jobs: List[Dict] = report.get("top_jobs", [])

        subject = f"[Gideon] Daily report – {date_str}"
        lines = [
            f"Gideon – Daily Report ({date_str})",
            "=" * 45,
            f"  New jobs scraped today : {new_jobs}",
            f"  Jobs analysed today    : {analyzed}",
            f"  Tailored resumes made  : {resumes}",
        ]
        if top_jobs:
            lines.append("\nTop new jobs:")
            for j in top_jobs:
                lines.append(
                    f"  - {j.get('title', '?')} @ {j.get('company', '?')}"
                    f"  [{j.get('status', '-')}]"
                )
        body = "\n".join(lines)

        logger.info(
            "[notifications] Daily report: %d new jobs, %d analysed, %d resumes.",
            new_jobs, analyzed, resumes,
        )
        self._send_email(subject, body)

    def send_task_result_summary(self, results: List[Any]) -> None:
        """Log a one-line summary for each completed :class:`~scheduler.tasks.TaskResult`.

        Useful for the end-of-run console summary without sending emails.

        Args:
            results: List of :class:`~scheduler.tasks.TaskResult` objects.
        """
        for r in results:
            status = "OK" if r.success else "FAIL"
            errors = f"  {len(r.errors)} error(s)" if r.errors else ""
            logger.info(
                "[notifications] [%s] %s  (%.1fs)%s",
                status,
                r.task_name,
                r.duration_seconds,
                errors,
            )
            for err in r.errors:
                logger.error("[notifications]   Error: %s", err)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _send_email(self, subject: str, body: str) -> None:
        """Send an email notification if email is enabled and configured.

        Falls back to a console log message when email is disabled or
        credentials are missing.

        Args:
            subject: Email subject line.
            body:    Plain-text email body.
        """
        if not self._email_enabled:
            logger.debug(
                "[notifications] Email disabled – would send: %s", subject
            )
            return

        missing = [
            name
            for name, val in [
                ("SENDER_EMAIL", self._sender_email),
                ("SENDER_PASSWORD", self._sender_password),
                ("RECIPIENT_EMAIL", self._recipient_email),
            ]
            if not val
        ]
        if missing:
            logger.warning(
                "[notifications] Email enabled but missing env vars: %s",
                ", ".join(missing),
            )
            return

        try:
            msg = MIMEMultipart()
            msg["From"] = self._sender_email  # type: ignore[assignment]
            msg["To"] = self._recipient_email  # type: ignore[assignment]
            msg["Subject"] = subject
            msg.attach(MIMEText(body, "plain"))

            with smtplib.SMTP(self._smtp_server, self._smtp_port) as server:
                server.ehlo()
                server.starttls()
                server.login(self._sender_email, self._sender_password)  # type: ignore[arg-type]
                server.send_message(msg)

            logger.info("[notifications] Email sent: %s", subject)

        except smtplib.SMTPException as exc:
            logger.error("[notifications] SMTP error sending '%s': %s", subject, exc)
        except OSError as exc:
            logger.error(
                "[notifications] Network error sending '%s': %s", subject, exc
            )
