"""
Scraper package for the Gideon application.

Provides web scrapers for extracting job postings from LinkedIn and Indeed,
with built-in rate limiting, retry logic, and respectful scraping practices.

Public surface
--------------
    BaseScraper, JobPosting     – abstract base + data transfer object
    IndeedScraper               – requests + BeautifulSoup implementation
    LinkedInScraper             – Selenium headless-Chrome implementation
    save_job_to_db              – persist a single JobPosting (upsert)
    save_postings_to_db         – persist a batch with per-row error isolation
    BatchResult                 – summary of a batch persistence run
    ScraperError and subclasses – typed exception hierarchy
"""

from scraper.base_scraper import BaseScraper, JobPosting
from scraper.db_handler import BatchResult, save_job_to_db, save_postings_to_db
from scraper.exceptions import (
    BlockedError,
    DatabasePersistenceError,
    NetworkError,
    ParseError,
    RateLimitError,
    ScraperError,
)
from scraper.indeed_scraper import IndeedScraper
from scraper.linkedin_scraper import LinkedInScraper

__all__ = [
    # Core
    "BaseScraper",
    "JobPosting",
    # Scrapers
    "IndeedScraper",
    "LinkedInScraper",
    # DB helpers
    "save_job_to_db",
    "save_postings_to_db",
    "BatchResult",
    # Exceptions
    "ScraperError",
    "NetworkError",
    "RateLimitError",
    "ParseError",
    "BlockedError",
    "DatabasePersistenceError",
]
