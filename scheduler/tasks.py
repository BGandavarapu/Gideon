"""
Scheduled task implementations for the Gideon pipeline.

Each public function in this module is a *task* that the scheduler can run on
a cron/interval schedule.  All tasks follow the same contract:

- Accept only JSON-serialisable arguments (so APScheduler can persist them).
- Return a :class:`TaskResult` dataclass describing what happened.
- Never raise unhandled exceptions – errors are captured in the result.
- Log every significant step at INFO level and every error at ERROR level.

Available tasks
---------------
``scrape_jobs_task``
    Scrape LinkedIn for new job postings and persist them to the database.

``analyze_new_jobs_task``
    Run keyword/requirement extraction on every "new" job in the database.

``generate_resumes_task``
    Auto-generate tailored resumes for "analyzed" jobs whose pre-tailor match
    score meets a configurable threshold.

``cleanup_old_jobs_task``
    Archive jobs that are older than a configurable number of days.

``daily_report_task``
    Compile a summary of today's activity (new jobs, resumes generated, errors)
    and pass it to :class:`~scheduler.notifications.NotificationService`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Tasks that are triggered manually from the web dashboard (NOT scheduled).
MANUAL_TASKS: list = ["scrape_jobs_task", "analyze_new_jobs_task", "generate_resumes_task"]

# Tasks that run automatically on a cron/interval schedule.
AUTO_TASKS: list = ["cleanup_old_jobs_task", "daily_report_task"]

# Maximum number of *new* jobs accepted in a single scrape run.
SCRAPE_LIMIT = 50


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


@dataclass
class TaskResult:
    """Uniform result object returned by every task function.

    Attributes:
        task_name:   Human-readable name of the task.
        success:     ``True`` if the task completed without a critical failure.
        started_at:  UTC timestamp when the task began.
        finished_at: UTC timestamp when the task ended (set by the task itself).
        errors:      List of error message strings (non-fatal errors allowed).
        data:        Task-specific counters / details (e.g. ``jobs_found``).
    """

    task_name: str
    success: bool = True
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: Optional[datetime] = None
    errors: List[str] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def finish(self) -> "TaskResult":
        """Mark the task as finished and return self (for chaining)."""
        self.finished_at = datetime.now(timezone.utc)
        return self

    @property
    def duration_seconds(self) -> float:
        """Wall-clock seconds from start to finish (0 if not finished yet)."""
        if self.finished_at is None:
            return 0.0
        return (self.finished_at - self.started_at).total_seconds()

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "task_name": self.task_name,
            "success": self.success,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_seconds": round(self.duration_seconds, 2),
            "errors": self.errors,
            "data": self.data,
        }


# ---------------------------------------------------------------------------
# Helper: safe Unicode sanitiser (mirrors main.py's _safe())
# ---------------------------------------------------------------------------


def _safe_text(text: str) -> str:
    """Replace characters outside cp1252 with '?' so logs don't crash on Windows."""
    try:
        text.encode("cp1252")
        return text
    except (UnicodeEncodeError, AttributeError):
        return "".join(c if ord(c) < 256 else "?" for c in str(text))


# ---------------------------------------------------------------------------
# Task 1: scrape_jobs_task
# ---------------------------------------------------------------------------


def scrape_jobs_task(search_configs: Optional[List[Dict]] = None) -> TaskResult:
    """Scrape LinkedIn for new job postings and persist them to the database.

    Uses the existing :class:`~scraper.linkedin_scraper.LinkedInScraper` and
    :func:`~scraper.db_handler.save_postings_to_db` so dedup / upsert logic is
    handled automatically.

    Search configurations are now read from ``settings.json`` via
    :class:`~web.settings_manager.SettingsManager` so the user can manage them
    through the Resumes page UI.  The *search_configs* parameter is kept for
    backwards compatibility (e.g. direct CLI / test invocation); when omitted,
    the settings are loaded automatically.

    Args:
        search_configs: Optional list of search query dicts, each with keys:
            ``keywords`` (str), ``location`` (str), ``max_results`` (int),
            ``domain`` (str, optional).  If ``None``, configs are loaded from
            ``settings.json``.

    Returns:
        :class:`TaskResult` with ``data`` keys:
        ``jobs_found``, ``new_jobs``, ``updated_jobs``, ``configs_run``.
    """
    # Load configs from SettingsManager if not provided explicitly.
    # Priority: industry configs for active domain + user-added configs.
    if search_configs is None:
        try:
            from web.settings_manager import SettingsManager as _SM
            _sm = _SM()

            logger.info("[scrape_jobs] Resolving search configs...")
            active_domains = _sm.get_active_domains()
            active_domain = active_domains[0] if active_domains else None
            logger.info("[scrape_jobs] Active domains from DB: %r", active_domains)

            industry_cfgs_preview = (
                _sm.get_industry_search_configs_for_domains(active_domains)
                if active_domains else []
            )
            logger.info(
                "[scrape_jobs] Industry configs for domains %r -> %d configs",
                active_domains, len(industry_cfgs_preview),
            )

            user_cfgs_preview = _sm.get_search_configs(enabled_only=True)
            logger.info(
                "[scrape_jobs] Manual search_configs (enabled): %d -> %s",
                len(user_cfgs_preview), [c["keywords"] for c in user_cfgs_preview],
            )

            if active_domains:
                # Reuse the already-fetched industry configs (avoid second DB call)
                industry_cfgs = industry_cfgs_preview
                # User-added configs that match any of the active domains (enabled only)
                user_domain_cfgs = [
                    c for c in user_cfgs_preview
                    if c.get("domain") in active_domains
                ]
                # Merge, deduplicate by keywords+location
                seen = {(c["keywords"].lower(), c.get("location", "").lower()) for c in industry_cfgs}
                merged = list(industry_cfgs)
                for uc in user_domain_cfgs:
                    key = (uc["keywords"].lower(), uc.get("location", "").lower())
                    if key not in seen:
                        merged.append(uc)
                        seen.add(key)
                search_configs = merged
                from web.settings_manager import DOMAINS as _DOMAINS
                domain_display = ", ".join(_DOMAINS.get(d, d) for d in active_domains)
                logger.info(
                    "[scrape_jobs] Resume-driven mode: %d config(s) for domain(s) '%s'.",
                    len(search_configs), domain_display,
                )
            else:
                # No active domain — fall back to user's enabled manual configs only
                search_configs = user_cfgs_preview
                if not search_configs:
                    logger.warning(
                        "[scrape_jobs] No active domain and no manual configs. "
                        "Please select a resume on the Resumes page first."
                    )
                else:
                    logger.warning(
                        "[scrape_jobs] No active resume domain — using %d manual search config(s) only.",
                        len(search_configs),
                    )
        except Exception as _exc:
            logger.warning("[scrape_jobs] Could not load configs from settings: %s", _exc)
            search_configs = []

    result = TaskResult(task_name="scrape_jobs")

    if not search_configs:
        logger.warning("[scrape_jobs] No search configs available — nothing to scrape.")
        result.data = {"configs_run": 0, "jobs_found": 0, "new_jobs": 0, "updated_jobs": 0}
        return result.finish()

    logger.info("[scrape_jobs] Starting – %d search config(s)", len(search_configs))

    try:
        from database.database import get_db
        from database.models import Job as _Job
        from scraper.linkedin_scraper import LinkedInScraper
        from scraper.db_handler import save_postings_to_db

        jobs_found = 0
        new_jobs = 0
        updated_jobs = 0
        configs_run = 0

        # active_domain may have been resolved above; fall back to None safely
        _active_domain_fallback = locals().get("active_domain", None)

        for cfg in search_configs:
            if new_jobs >= SCRAPE_LIMIT:
                logger.info(
                    "[scrape_jobs] Scrape cap reached (%d/%d new jobs). Stopping early.",
                    new_jobs, SCRAPE_LIMIT,
                )
                break

            keywords = cfg.get("keywords", "")
            location = cfg.get("location", "")
            max_results = int(cfg.get("max_results", 20))
            # cfg['domain'] may be absent for industry configs (they don't carry it);
            # fall back to the resolved active_domain
            domain = cfg.get("domain") or _active_domain_fallback

            logger.info(
                "[scrape_jobs] Scraping LinkedIn: %r in %r (max %d, domain=%r)",
                keywords, location, max_results, domain,
            )

            try:
                with LinkedInScraper() as scraper:
                    postings = scraper.scrape(
                        keywords=keywords,
                        location=location,
                        max_results=max_results,
                    )

                jobs_found += len(postings)
                configs_run += 1

                batch = save_postings_to_db(postings)
                new_jobs += batch.saved
                updated_jobs += batch.updated

                # Tag scraped jobs with the config's domain where not yet set
                if domain and (batch.saved > 0 or batch.updated > 0):
                    try:
                        scraped_urls = {p.application_url for p in postings if p.application_url}
                        with get_db() as db:
                            jobs_to_tag = (
                                db.query(_Job)
                                .filter(
                                    _Job.application_url.in_(scraped_urls),
                                    _Job.domain.is_(None),
                                )
                                .all()
                            )
                            for job in jobs_to_tag:
                                job.domain = domain
                            db.commit()
                            logger.info(
                                "[scrape_jobs] Tagged %d job(s) with domain=%r.",
                                len(jobs_to_tag), domain,
                            )
                    except Exception as tag_exc:
                        logger.warning("[scrape_jobs] Domain tagging failed: %s", tag_exc)

                logger.info(
                    "[scrape_jobs] Config %r done – %d found, %d new, %d updated.",
                    keywords, len(postings), batch.saved, batch.updated,
                )

            except Exception as exc:
                msg = _safe_text(f"Config '{keywords}' failed: {exc}")
                logger.error("[scrape_jobs] %s", msg)
                result.errors.append(msg)

        result.data = {
            "configs_run": configs_run,
            "jobs_found": jobs_found,
            "new_jobs": new_jobs,
            "updated_jobs": updated_jobs,
            "capped": new_jobs >= SCRAPE_LIMIT,
        }
        logger.info(
            "[scrape_jobs] Done – %d new / %d updated across %d config(s).",
            new_jobs, updated_jobs, configs_run,
        )

    except Exception as exc:
        msg = _safe_text(f"Critical error: {exc}")
        logger.exception("[scrape_jobs] %s", msg)
        result.errors.append(msg)
        result.success = False

    return result.finish()


# ---------------------------------------------------------------------------
# Task 2: analyze_new_jobs_task
# ---------------------------------------------------------------------------


def analyze_new_jobs_task() -> TaskResult:
    """Analyse every "new" job in the database and extract structured skills.

    Runs :class:`~analyzer.keyword_extractor.KeywordExtractor` and
    :class:`~analyzer.requirement_parser.RequirementParser` on each job, then
    writes the extracted ``required_skills`` and ``preferred_skills`` back to the
    database and sets ``status = "analyzed"``.

    Returns:
        :class:`TaskResult` with ``data`` key ``jobs_analyzed``.
    """
    result = TaskResult(task_name="analyze_new_jobs")
    logger.info("[analyze_new_jobs] Starting")

    try:
        from analyzer.keyword_extractor import KeywordExtractor
        from database.database import get_db
        from database.models import Job

        extractor = KeywordExtractor()
        jobs_analyzed = 0
        jobs_skipped = 0

        with get_db() as db:
            new_jobs = db.query(Job).filter(Job.status == "new").all()
            logger.info("[analyze_new_jobs] Found %d job(s) to analyse.", len(new_jobs))

            # Stamp each analyzed job with the currently active resume id.
            active_resume_id: Optional[int] = None
            try:
                from database.models import MasterResume as _MR
                _ar = db.query(_MR).filter(_MR.is_active == True).first()
                if _ar:
                    active_resume_id = _ar.id
            except Exception as _rexc:
                logger.debug("[analyze_new_jobs] Could not resolve active resume: %s", _rexc)

            for job in new_jobs:
                if not job.job_description:
                    logger.warning(
                        "[analyze_new_jobs] Job #%d has no description – skipping.", job.id
                    )
                    jobs_skipped += 1
                    continue

                try:
                    extracted = extractor.extract(job.job_description)

                    job.required_skills = extracted["required_skills"] or None
                    job.preferred_skills = extracted["preferred_skills"] or None
                    job.status = "analyzed"
                    if active_resume_id is not None:
                        job.analyzed_with_resume_id = active_resume_id
                    jobs_analyzed += 1

                    # Confirm / refine domain using DomainDetector
                    try:
                        from analyzer.domain_detector import DomainDetector
                        detected = DomainDetector().detect_from_job(job)
                        job.domain = detected.get("domain", job.domain)
                    except Exception as _det_exc:
                        logger.debug(
                            "[analyze_new_jobs] Domain detection skipped for job #%d: %s",
                            job.id, _det_exc,
                        )

                    logger.info(
                        "[analyze_new_jobs] Job #%d (%s) – %d required, %d preferred, domain=%r.",
                        job.id,
                        _safe_text(job.job_title or ""),
                        len(extracted["required_skills"]),
                        len(extracted["preferred_skills"]),
                        job.domain,
                    )

                except Exception as exc:
                    msg = _safe_text(f"Job #{job.id} analysis failed: {exc}")
                    logger.error("[analyze_new_jobs] %s", msg)
                    result.errors.append(msg)

        result.data = {
            "jobs_found": len(new_jobs) if "new_jobs" in dir() else 0,
            "jobs_analyzed": jobs_analyzed,
            "jobs_skipped": jobs_skipped,
        }
        logger.info("[analyze_new_jobs] Done – %d job(s) analysed.", jobs_analyzed)

    except Exception as exc:
        msg = _safe_text(f"Critical error: {exc}")
        logger.exception("[analyze_new_jobs] %s", msg)
        result.errors.append(msg)
        result.success = False

    return result.finish()


# ---------------------------------------------------------------------------
# Task 3: generate_resumes_task
# ---------------------------------------------------------------------------


def _get_resume_for_job(job: Any, db: Any) -> Optional[Any]:
    """Return the best :class:`~database.models.MasterResume` for *job*.

    Lookup order:
    1. Domain-specific resume configured in ``settings.json``.
    2. Active master resume (fallback).
    3. ``None`` if neither exists.

    Args:
        job: A :class:`~database.models.Job` ORM instance.
        db: An open SQLAlchemy session.

    Returns:
        :class:`~database.models.MasterResume` or ``None``.
    """
    from database.models import MasterResume as _MR

    try:
        from web.settings_manager import SettingsManager as _SM
        domain = getattr(job, "domain", None) or "other"
        resume_id = _SM().get_domain_resume(domain)
        if resume_id is not None:
            mr = db.query(_MR).filter(_MR.id == resume_id).first()
            if mr:
                logger.info(
                    "[generate_resumes] Using domain resume #%d (%r) for job #%d (domain=%r).",
                    mr.id, mr.name, job.id, domain,
                )
                return mr
    except Exception as _exc:
        logger.debug("[generate_resumes] Domain resume lookup failed: %s", _exc)

    # Fallback: active resume
    return db.query(_MR).filter(_MR.is_active.is_(True)).first()


def generate_resumes_task(match_threshold: float = 35.0) -> TaskResult:
    """Auto-generate tailored resumes for high-match "analyzed" jobs.

    Only jobs that:
    1. Have ``status == "analyzed"``.
    2. Do not already have a :class:`~database.models.TailoredResume` row.
    3. Score at or above *match_threshold* (pre-tailor) against the matched
       master resume (domain-specific or active fallback).

    are processed.

    Args:
        match_threshold: Minimum pre-tailor match score (0–100) to trigger
            automated tailoring.  Defaults to 35.

    Returns:
        :class:`TaskResult` with ``data`` keys:
        ``jobs_checked``, ``resumes_generated``, ``skipped_low_score``.
    """
    result = TaskResult(task_name="generate_resumes")
    logger.info(
        "[generate_resumes] Starting – threshold=%.1f%%", match_threshold
    )

    try:
        from analyzer.scoring import ScoringEngine
        from database.database import get_db
        from database.models import Job, MasterResume, TailoredResume
        from resume_engine.modifier import ResumeModifier
        from resume_engine.rate_limiter import QuotaExceededError

        resumes_generated = 0
        skipped_low_score = 0
        jobs_checked = 0

        with get_db() as db:
            # Verify at least one master resume exists as a sanity check
            any_master = (
                db.query(MasterResume)
                .filter(MasterResume.is_active.is_(True))
                .first()
            )
            if not any_master:
                logger.warning(
                    "[generate_resumes] No active master resume found – skipping task."
                )
                result.data = {
                    "jobs_checked": 0,
                    "resumes_generated": 0,
                    "skipped_low_score": 0,
                }
                return result.finish()

            # Find analyzed jobs that don't already have a tailored resume
            already_tailored_job_ids = {
                row.job_id
                for row in db.query(TailoredResume.job_id).all()
            }
            candidate_jobs = (
                db.query(Job)
                .filter(Job.status == "analyzed")
                .all()
            )
            candidate_jobs = [
                j for j in candidate_jobs if j.id not in already_tailored_job_ids
            ]

            logger.info(
                "[generate_resumes] %d analysed job(s) without tailored resume.",
                len(candidate_jobs),
            )

            engine = ScoringEngine()
            modifier = ResumeModifier()

            for job in candidate_jobs:
                jobs_checked += 1
                try:
                    # Pick the right resume for this job's domain
                    master = _get_resume_for_job(job, db)
                    if not master:
                        logger.warning(
                            "[generate_resumes] No resume for job #%d – skipping.", job.id
                        )
                        continue

                    score_result = engine.score(job, master)
                    score = score_result.total_score

                    if score < match_threshold:
                        logger.info(
                            "[generate_resumes] Job #%d score %.1f < threshold %.1f – skipping.",
                            job.id, score, match_threshold,
                        )
                        skipped_low_score += 1
                        continue

                    logger.info(
                        "[generate_resumes] Job #%d (%s) score=%.1f – tailoring ...",
                        job.id, _safe_text(job.job_title or ""), score,
                    )

                    mod_result = modifier.modify_resume(
                        master, job,
                        style_fingerprint=getattr(master, "style_fingerprint", None),
                    )

                    # Upsert tailored resume
                    existing = (
                        db.query(TailoredResume)
                        .filter(
                            TailoredResume.job_id == job.id,
                            TailoredResume.master_resume_id == master.id,
                        )
                        .first()
                    )
                    if existing:
                        existing.tailored_content = mod_result.content
                        existing.match_score      = score
                        existing.score_breakdown  = score_result.score_breakdown
                        existing.generated_at     = datetime.now(timezone.utc)
                    else:
                        db.add(
                            TailoredResume(
                                job_id=job.id,
                                master_resume_id=master.id,
                                tailored_content=mod_result.content,
                                match_score=score,
                                score_breakdown=score_result.score_breakdown,
                            )
                        )

                    resumes_generated += 1
                    logger.info(
                        "[generate_resumes] Tailored resume saved for job #%d.", job.id
                    )

                except QuotaExceededError as exc:
                    msg = f"NVIDIA NIM quota exceeded after {resumes_generated} resume(s): {exc}"
                    logger.error("[generate_resumes] %s", msg)
                    result.errors.append(msg)
                    # Quota is per-day; no point continuing
                    result.success = False
                    break

                except Exception as exc:
                    msg = _safe_text(
                        f"Resume generation failed for job #{job.id}: {exc}"
                    )
                    logger.error("[generate_resumes] %s", msg)
                    result.errors.append(msg)

        result.data = {
            "jobs_checked": jobs_checked,
            "resumes_generated": resumes_generated,
            "skipped_low_score": skipped_low_score,
        }
        logger.info(
            "[generate_resumes] Done – %d resume(s) generated, %d skipped (low score).",
            resumes_generated, skipped_low_score,
        )

    except Exception as exc:
        msg = _safe_text(f"Critical error: {exc}")
        logger.exception("[generate_resumes] %s", msg)
        result.errors.append(msg)
        result.success = False

    return result.finish()


# ---------------------------------------------------------------------------
# Task 4: cleanup_old_jobs_task
# ---------------------------------------------------------------------------


def cleanup_old_jobs_task(days_old: int = 30) -> TaskResult:
    """Archive jobs that are older than *days_old* days and not yet applied.

    Sets ``status = "archived"`` so they are hidden from normal queries but
    remain in the database for auditing purposes.

    Args:
        days_old: Archive jobs scraped more than this many days ago.

    Returns:
        :class:`TaskResult` with ``data`` key ``jobs_archived``.
    """
    result = TaskResult(task_name="cleanup_old_jobs")
    logger.info("[cleanup_old_jobs] Starting – archiving jobs older than %d days.", days_old)

    try:
        from database.database import get_db
        from database.models import Job

        cutoff = datetime.now(timezone.utc) - timedelta(days=days_old)
        jobs_archived = 0

        with get_db() as db:
            old_jobs = (
                db.query(Job)
                .filter(
                    Job.date_scraped < cutoff,
                    Job.status.notin_(["applied", "archived"]),
                )
                .all()
            )
            logger.info("[cleanup_old_jobs] Found %d job(s) to archive.", len(old_jobs))

            for job in old_jobs:
                job.status = "archived"
                jobs_archived += 1

        result.data = {"jobs_archived": jobs_archived}
        logger.info("[cleanup_old_jobs] Done – %d job(s) archived.", jobs_archived)

    except Exception as exc:
        msg = _safe_text(f"Critical error: {exc}")
        logger.exception("[cleanup_old_jobs] %s", msg)
        result.errors.append(msg)
        result.success = False

    return result.finish()


# ---------------------------------------------------------------------------
# Task 5: daily_report_task
# ---------------------------------------------------------------------------


def daily_report_task() -> TaskResult:
    """Compile today's activity summary and emit a notification.

    Counts jobs scraped today, resumes generated, and any jobs above the
    notification threshold, then dispatches via
    :class:`~scheduler.notifications.NotificationService`.

    Returns:
        :class:`TaskResult` with ``data`` key ``report``.
    """
    result = TaskResult(task_name="daily_report")
    logger.info("[daily_report] Generating daily summary.")

    try:
        from database.database import get_db
        from database.models import Job, TailoredResume
        from scheduler.notifications import NotificationService

        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        with get_db() as db:
            new_today = (
                db.query(Job)
                .filter(Job.date_scraped >= today_start)
                .count()
            )
            analyzed_today = (
                db.query(Job)
                .filter(
                    Job.date_scraped >= today_start,
                    Job.status == "analyzed",
                )
                .count()
            )
            resumes_today = (
                db.query(TailoredResume)
                .filter(TailoredResume.generated_at >= today_start)
                .count()
            )

            # Top new jobs for the notification body
            top_jobs = (
                db.query(Job)
                .filter(Job.date_scraped >= today_start)
                .order_by(Job.date_scraped.desc())
                .limit(5)
                .all()
            )
            top_job_dicts = [
                {
                    "title": _safe_text(j.job_title or ""),
                    "company": _safe_text(j.company_name or ""),
                    "location": _safe_text(j.location or ""),
                    "status": j.status,
                }
                for j in top_jobs
            ]

        report = {
            "date": today_start.date().isoformat(),
            "new_jobs": new_today,
            "analyzed_today": analyzed_today,
            "resumes_generated_today": resumes_today,
            "top_jobs": top_job_dicts,
        }
        result.data = {"report": report}

        # Dispatch notification (console always; email only if configured)
        notifier = NotificationService()
        notifier.send_daily_report(report)

        logger.info(
            "[daily_report] Done – %d new jobs, %d analysed, %d resumes today.",
            new_today, analyzed_today, resumes_today,
        )

    except Exception as exc:
        msg = _safe_text(f"Critical error: {exc}")
        logger.exception("[daily_report] %s", msg)
        result.errors.append(msg)
        result.success = False

    return result.finish()
