"""
Abstract base class and shared data contract for all scrapers.

Every concrete scraper (LinkedIn, Indeed, …) must subclass
:class:`BaseScraper` and implement its three abstract methods.  The
:class:`JobPosting` dataclass is the canonical data transfer object passed
between the scraper layer and all downstream modules.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterator, List, Optional

from scraper.config import CONFIG, ScrapingConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data transfer object
# ---------------------------------------------------------------------------


@dataclass
class JobPosting:
    """Structured representation of a single scraped job posting.

    All fields map directly to the ``jobs`` database table defined in Phase 2.
    Fields that are not found on a particular job board are left as ``None``
    rather than raising errors, so that partial data is still useful.

    Attributes:
        job_title: Normalised job title string.
        company_name: Hiring company's name.
        location: Office location or "Remote".
        job_description: Full body text of the job description.
        required_skills: Bullet-point required qualifications (raw text).
        preferred_skills: Bullet-point preferred qualifications (raw text).
        salary_range: Salary or compensation range string, if listed.
        application_url: Canonical URL used to apply; also acts as dedup key.
        date_posted: Date the posting was published, if parseable.
        source: Which job board this was scraped from (e.g. ``"linkedin"``).
        date_scraped: UTC timestamp of when scraping occurred (auto-set).
    """

    job_title: str
    company_name: str
    job_description: str
    application_url: str
    source: str
    location: Optional[str] = None
    required_skills: Optional[str] = None
    preferred_skills: Optional[str] = None
    salary_range: Optional[str] = None
    date_posted: Optional[date] = None
    date_scraped: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """Validate required fields are non-empty after construction."""
        for attr in ("job_title", "company_name", "job_description", "application_url"):
            value = getattr(self, attr)
            if not value or not value.strip():
                raise ValueError(f"JobPosting.{attr} must be a non-empty string.")

    def to_dict(self) -> dict:
        """Serialise the posting to a plain dictionary.

        Returns:
            Dictionary with all fields, suitable for JSON serialisation or
            passing to SQLAlchemy ``**kwargs`` insertion patterns.
        """
        return {
            "job_title": self.job_title,
            "company_name": self.company_name,
            "location": self.location,
            "job_description": self.job_description,
            "required_skills": self.required_skills,
            "preferred_skills": self.preferred_skills,
            "salary_range": self.salary_range,
            "application_url": self.application_url,
            "date_posted": self.date_posted.isoformat() if self.date_posted else None,
            "date_scraped": self.date_scraped.isoformat(),
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# Abstract base scraper
# ---------------------------------------------------------------------------


class BaseScraper(ABC):
    """Abstract base class that all job-board scrapers must implement.

    Concrete subclasses only need to implement three methods:
    :meth:`_fetch_job_urls`, :meth:`_parse_job_page`, and optionally
    :meth:`close`.  The public :meth:`scrape` method handles orchestration,
    rate limiting, error aggregation, and progress logging.

    Args:
        config: Scraping configuration.  Defaults to the module-level
            singleton loaded from ``config.yaml``.

    Attributes:
        config: Active :class:`~scraper.config.ScrapingConfig`.
        source_name: Human-readable identifier for this scraper (set by
            subclasses via the ``_SOURCE_NAME`` class variable).
    """

    _SOURCE_NAME: str = "unknown"

    def __init__(self, config: Optional[ScrapingConfig] = None) -> None:
        self.config: ScrapingConfig = config or CONFIG
        self.source_name: str = self._SOURCE_NAME
        self._jobs_scraped: int = 0
        self._errors: int = 0
        logger.info("Initialised %s scraper.", self.source_name)

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    def _fetch_job_urls(self, keywords: str, location: str) -> List[str]:
        """Return a list of individual job-posting URLs for the query.

        Implementations should apply :data:`config.max_jobs_per_search` as
        a cap and must call :func:`~scraper.utils.random_delay` between
        pagination requests.

        Args:
            keywords: Search terms (e.g. ``"python developer"``).
            location: Geographic filter (e.g. ``"San Francisco, CA"``).

        Returns:
            List of absolute URLs pointing to individual job postings.
        """

    @abstractmethod
    def _parse_job_page(self, url: str) -> Optional[JobPosting]:
        """Fetch and parse a single job posting URL.

        Implementations should handle all HTTP/parsing errors internally
        and return ``None`` on unrecoverable failures so the caller can
        skip the listing without aborting the whole search.

        Args:
            url: Absolute URL of the job posting page.

        Returns:
            A populated :class:`JobPosting`, or ``None`` if parsing fails.
        """

    def close(self) -> None:
        """Release any resources held by this scraper (e.g. WebDriver).

        Subclasses that open browser sessions or connection pools should
        override this method.  The default implementation is a no-op.
        """

    # ------------------------------------------------------------------
    # Public orchestration method
    # ------------------------------------------------------------------

    def scrape(
        self,
        keywords: str,
        location: str = "",
        max_results: Optional[int] = None,
        on_progress: Optional[Callable[[int, int, "JobPosting"], None]] = None,
    ) -> List[JobPosting]:
        """Run a complete scrape for the given search query.

        Orchestrates URL collection, per-page parsing, rate limiting, and
        error counting.  Always returns a (possibly empty) list – never
        raises.

        Args:
            keywords: Job search keywords (e.g. ``"data engineer"``).
            location: Location filter; empty string means no filter.
            max_results: Override the config cap for this run.  If ``None``
                the value from :attr:`config.max_jobs_per_search` is used.
            on_progress: Optional callable invoked after each URL is parsed
                (successfully or not) with signature
                ``(completed: int, total: int, posting: JobPosting | None)``.
                Useful for Rich progress bars in CLI commands.

        Returns:
            List of successfully parsed :class:`JobPosting` objects.
        """
        # Apply per-call cap without permanently mutating the config
        original_cap = self.config.max_jobs_per_search
        if max_results is not None:
            self.config.max_jobs_per_search = max_results

        logger.info(
            "[%s] Starting scrape – keywords=%r, location=%r, max=%d.",
            self.source_name,
            keywords,
            location,
            self.config.max_jobs_per_search,
        )
        results: List[JobPosting] = []

        try:
            urls = self._fetch_job_urls(keywords, location)
        except Exception as exc:
            logger.error("[%s] Failed to fetch job URLs: %s", self.source_name, exc)
            self.config.max_jobs_per_search = original_cap
            return results

        total = len(urls)
        logger.info("[%s] Found %d job URL(s) to parse.", self.source_name, total)

        for completed, url in enumerate(urls, start=1):
            posting = self._parse_job_page(url)
            if posting is not None:
                results.append(posting)
                self._jobs_scraped += 1
                logger.debug(
                    "[%s] Parsed '%s' at %s.",
                    self.source_name,
                    posting.job_title,
                    url,
                )
            else:
                self._errors += 1

            if on_progress:
                on_progress(completed, total, posting)

        self.config.max_jobs_per_search = original_cap
        logger.info(
            "[%s] Scrape complete – %d jobs collected, %d errors.",
            self.source_name,
            len(results),
            self._errors,
        )
        return results

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> "BaseScraper":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @property
    def stats(self) -> dict:
        """Return a snapshot of runtime statistics.

        Returns:
            Dictionary with ``jobs_scraped`` and ``errors`` counts.
        """
        return {
            "source": self.source_name,
            "jobs_scraped": self._jobs_scraped,
            "errors": self._errors,
        }
