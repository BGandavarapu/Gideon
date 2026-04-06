"""
Database persistence layer for scraped job postings.

This module is the bridge between the scraper layer (:class:`~scraper.base_scraper.JobPosting`
dataclass) and the database layer (:class:`~database.models.Job` ORM model).

Key design decisions
--------------------
- **Upsert semantics**: ``application_url`` is the natural dedup key (UNIQUE
  constraint on the ``jobs`` table).  When a URL already exists the row is
  refreshed (``date_scraped`` updated, any newly available fields written),
  so re-running a scrape never produces duplicates.
- **Per-posting transactions**: each save is its own ``get_db()`` context so
  that one bad row does not roll back the whole batch.
- **Partial-batch resilience**: :func:`save_postings_to_db` always returns a
  :class:`BatchResult` summary so the caller can report saved / updated /
  failed counts without crashing.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from database.database import get_db
from database.models import Job
from scraper.base_scraper import JobPosting
from scraper.exceptions import DatabasePersistenceError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


@dataclass
class BatchResult:
    """Summary of a :func:`save_postings_to_db` operation.

    Attributes:
        saved: Number of brand-new rows inserted.
        updated: Number of existing rows refreshed.
        failed: Number of postings that could not be persisted.
        errors: List of ``(application_url, error_message)`` tuples.
    """

    saved: int = 0
    updated: int = 0
    failed: int = 0
    errors: List[tuple] = field(default_factory=list)

    @property
    def total_processed(self) -> int:
        """Total postings attempted (saved + updated + failed)."""
        return self.saved + self.updated + self.failed

    def __str__(self) -> str:
        return (
            f"BatchResult(saved={self.saved}, updated={self.updated}, "
            f"failed={self.failed})"
        )


# ---------------------------------------------------------------------------
# Single-posting helpers
# ---------------------------------------------------------------------------


def _posting_to_job_kwargs(posting: JobPosting) -> dict:
    """Convert a :class:`~scraper.base_scraper.JobPosting` to a dict for ``Job(**kwargs)``.

    Args:
        posting: Source dataclass instance.

    Returns:
        Dictionary of field values matching :class:`~database.models.Job` columns.
    """
    return {
        "job_title": posting.job_title,
        "company_name": posting.company_name,
        "location": posting.location,
        "job_description": posting.job_description,
        "required_skills": _split_skill_string(posting.required_skills),
        "preferred_skills": _split_skill_string(posting.preferred_skills),
        "salary_range": posting.salary_range,
        "application_url": posting.application_url,
        "date_posted": posting.date_posted,
        "date_scraped": datetime.now(timezone.utc),
        "source": posting.source,
        "status": "new",
    }


def save_job_to_db(posting: JobPosting) -> tuple[Job, bool]:
    """Persist a single :class:`~scraper.base_scraper.JobPosting` to the database.

    Uses an upsert strategy keyed on ``application_url``:
    - **Insert** if the URL has never been seen before.
    - **Update** ``date_scraped`` and any non-null fields if it already exists,
      while preserving the existing ``status`` so that jobs under review are
      not reset to ``"new"``.

    Args:
        posting: The job posting to persist.

    Returns:
        A ``(job_orm_object, is_new)`` tuple where ``is_new`` is ``True`` for
        an insert and ``False`` for an update.

    Raises:
        DatabasePersistenceError: If the database operation fails.
    """
    try:
        with get_db() as db:
            existing: Optional[Job] = (
                db.query(Job)
                .filter(Job.application_url == posting.application_url)
                .first()
            )

            if existing is None:
                job = Job(**_posting_to_job_kwargs(posting))
                db.add(job)
                logger.debug(
                    "Inserted new job: %r at %s", posting.job_title, posting.application_url
                )
                return job, True

            # Update mutable fields; preserve status so reviewed jobs keep their state
            existing.job_title = posting.job_title
            existing.company_name = posting.company_name
            existing.location = posting.location or existing.location
            existing.job_description = posting.job_description
            existing.salary_range = posting.salary_range or existing.salary_range
            existing.date_posted = posting.date_posted or existing.date_posted
            existing.date_scraped = datetime.now(timezone.utc)
            # Merge skills: add any new ones without losing manually tagged skills
            existing.required_skills = _merge_skills(
                existing.required_skills, posting.required_skills
            )
            existing.preferred_skills = _merge_skills(
                existing.preferred_skills, posting.preferred_skills
            )
            logger.debug(
                "Updated existing job id=%s: %r", existing.id, posting.job_title
            )
            return existing, False

    except SQLAlchemyError as exc:
        logger.error(
            "Database error saving %s: %s", posting.application_url, exc
        )
        raise DatabasePersistenceError(
            f"Failed to save job: {exc}", url=posting.application_url
        ) from exc


def _split_skill_string(skills: Optional[str | list]) -> Optional[list]:
    """Normalise a skills value to a list, splitting comma-separated strings.

    Args:
        skills: Either a comma-separated string, a list, or ``None``.

    Returns:
        A list of stripped skill strings, or ``None`` if input is empty.
    """
    if skills is None:
        return None
    if isinstance(skills, str):
        result = [s.strip() for s in skills.split(",") if s.strip()]
        return result or None
    result = [s for s in skills if s]
    return result or None


def _merge_skills(
    existing: Optional[list],
    incoming: Optional[str | list],
) -> Optional[list]:
    """Merge an existing skills list with newly scraped skills.

    Preserves all existing items and appends any newly seen ones
    (case-insensitive deduplication).

    Args:
        existing: Current list stored in the database (may be ``None``).
        incoming: Newly scraped skills, either a list or a raw string.

    Returns:
        Merged list, or ``None`` if both inputs are empty.
    """
    incoming_list = _split_skill_string(incoming) or []

    if not incoming_list:
        # Nothing new to add; return existing (normalised to None if empty)
        return existing if existing else None

    if not existing:
        return incoming_list

    existing_lower = {s.lower() for s in existing}
    merged = list(existing)
    for skill in incoming_list:
        if skill.lower() not in existing_lower:
            merged.append(skill)
            existing_lower.add(skill.lower())

    return merged or None


# ---------------------------------------------------------------------------
# Batch persistence
# ---------------------------------------------------------------------------


def save_postings_to_db(
    postings: List[JobPosting],
    *,
    on_progress: Optional[callable] = None,
) -> BatchResult:
    """Persist a list of postings, continuing past individual failures.

    Each posting is saved in its own transaction so that one bad row does not
    roll back the rest.  Progress is reported via an optional callback.

    Args:
        postings: List of :class:`~scraper.base_scraper.JobPosting` instances.
        on_progress: Optional callable invoked after each attempt with the
            signature ``(index: int, total: int, posting: JobPosting, is_new: bool | None)``.
            ``is_new`` is ``None`` on failure.

    Returns:
        :class:`BatchResult` with counts of inserts, updates, and failures.
    """
    result = BatchResult()
    total = len(postings)

    for idx, posting in enumerate(postings):
        try:
            _, is_new = save_job_to_db(posting)
            if is_new:
                result.saved += 1
            else:
                result.updated += 1
            if on_progress:
                on_progress(idx + 1, total, posting, is_new)
        except DatabasePersistenceError as exc:
            result.failed += 1
            result.errors.append((posting.application_url, str(exc)))
            logger.warning("Skipping %s due to error: %s", posting.application_url, exc)
            if on_progress:
                on_progress(idx + 1, total, posting, None)

    logger.info(
        "Batch complete – %d saved, %d updated, %d failed (of %d total).",
        result.saved,
        result.updated,
        result.failed,
        total,
    )
    return result


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def get_jobs_by_status(status: str, limit: int = 100) -> List[Job]:
    """Fetch jobs from the database filtered by status.

    Args:
        status: One of ``"new"``, ``"analyzed"``, ``"applied"``.
        limit: Maximum number of rows to return.

    Returns:
        List of :class:`~database.models.Job` ORM instances.
    """
    with get_db() as db:
        return (
            db.query(Job)
            .filter(Job.status == status)
            .order_by(Job.date_scraped.desc())
            .limit(limit)
            .all()
        )


def get_job_count() -> dict:
    """Return a status → count mapping for all jobs in the database.

    Returns:
        Dictionary e.g. ``{"new": 12, "analyzed": 3, "applied": 1}``.
    """
    with get_db() as db:
        from sqlalchemy import func

        rows = db.query(Job.status, func.count(Job.id)).group_by(Job.status).all()
        return {status: count for status, count in rows}
