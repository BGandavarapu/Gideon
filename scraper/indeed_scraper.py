"""
Indeed job scraper using requests + BeautifulSoup.

Indeed's public search results are largely server-side rendered, making a
lightweight ``requests``-based approach sufficient for most listing pages.
Individual job detail pages that require JavaScript are handled by falling
back to the structured data embedded in the page's ``<script type="application/ld+json">``
tag before resorting to CSS selectors.

robots.txt
----------
Indeed's robots.txt allows crawling of ``/jobs`` and individual job pages for
most user-agent tokens.  This scraper checks allowance on initialisation and
logs a warning (but still proceeds) if the path is restricted, per the
project's fail-open policy.

Usage:
    >>> from scraper.indeed_scraper import IndeedScraper
    >>> with IndeedScraper() as scraper:
    ...     jobs = scraper.scrape("data analyst", "New York, NY", max_results=10)
"""

import json
import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

from scraper.base_scraper import BaseScraper, JobPosting
from scraper.config import ScrapingConfig
from scraper.exceptions import BlockedError, RateLimitError
from scraper.utils import (
    build_request_headers,
    clean_html_text,
    clean_text,
    extract_relative_date,
    extract_salary_range,
    is_scraping_allowed,
    random_delay,
    retry,
    save_raw_html,
    truncate_text,
)

logger = logging.getLogger(__name__)

_INDEED_BASE = "https://www.indeed.com"
_SEARCH_URL = (
    "https://www.indeed.com/jobs"
    "?q={keywords}&l={location}&sort=date&start={start}"
)
_RESULTS_PER_PAGE = 15  # Indeed shows ~15 results per page

# Selectors tried in order for the date-posted text on search result cards
_DATE_SELECTORS = [
    "span.date",
    "span[data-testid='myJobsStateDate']",
    "span.result-link-bar-container span",
]

# Indicators that the page is a CAPTCHA / bot-detection wall
_BLOCK_SIGNALS = [
    "please verify you are a human",
    "unusual traffic from your computer",
    "access denied",
    "captcha",
]


class IndeedScraper(BaseScraper):
    """Requests + BeautifulSoup scraper for Indeed job listings.

    Uses a persistent :class:`requests.Session` with rotating User-Agent
    headers.  Structured JSON-LD metadata is preferred over CSS selectors
    wherever available, making the parser more resilient to HTML layout
    changes.

    On first instantiation a robots.txt check is performed and the result
    is logged.  The scraper proceeds regardless (fail-open), matching the
    project-wide policy for transient or unexpected robots.txt states.

    Args:
        config: Optional custom :class:`~scraper.config.ScrapingConfig`.
        check_robots: If ``True`` (default), check robots.txt on init.

    Attributes:
        _session: Reusable :class:`~requests.Session` for connection pooling.
    """

    _SOURCE_NAME = "indeed"

    def __init__(
        self,
        config: Optional[ScrapingConfig] = None,
        check_robots: bool = True,
    ) -> None:
        super().__init__(config)
        self._session = requests.Session()
        self._session.headers.update(build_request_headers())
        if check_robots:
            allowed = is_scraping_allowed(_INDEED_BASE, "/jobs")
            if not allowed:
                logger.warning(
                    "[Indeed] robots.txt restricts /jobs – proceeding with caution."
                )

    def close(self) -> None:
        """Close the underlying requests session."""
        try:
            self._session.close()
            logger.debug("Indeed requests session closed.")
        except Exception as exc:
            logger.warning("Error closing requests session: %s", exc)

    # ------------------------------------------------------------------
    # URL collection
    # ------------------------------------------------------------------

    def _fetch_job_urls(self, keywords: str, location: str) -> List[str]:
        """Collect individual job-posting URLs from Indeed search results.

        Paginates through Indeed's search results pages until
        :attr:`~scraper.config.ScrapingConfig.max_jobs_per_search` URLs
        have been gathered or no more results are returned.

        Args:
            keywords: Job search terms.
            location: Geographic filter (city, state, or postal code).

        Returns:
            Deduplicated list of absolute Indeed job URLs.
        """
        collected_urls: List[str] = []
        start = 0
        page = 1

        while len(collected_urls) < self.config.max_jobs_per_search:
            search_url = _SEARCH_URL.format(
                keywords=quote_plus(keywords),
                location=quote_plus(location),
                start=start,
            )
            logger.debug("[Indeed] Loading search page %d: %s", page, search_url)

            html = self._get_page(search_url)
            if not html:
                logger.info("[Indeed] Received empty response on page %d – stopping.", page)
                break

            soup = BeautifulSoup(html, "lxml")
            page_urls = self._extract_urls_from_soup(soup)

            if not page_urls:
                logger.info("[Indeed] No job links found on page %d – stopping.", page)
                break

            for url in page_urls:
                if url not in collected_urls:
                    collected_urls.append(url)
                if len(collected_urls) >= self.config.max_jobs_per_search:
                    break

            logger.debug(
                "[Indeed] Page %d yielded %d URLs (total so far: %d).",
                page, len(page_urls), len(collected_urls),
            )

            start += _RESULTS_PER_PAGE
            page += 1
            random_delay()

        return collected_urls[: self.config.max_jobs_per_search]

    def _extract_urls_from_soup(self, soup: BeautifulSoup) -> List[str]:
        """Parse job card links from a BeautifulSoup-parsed search page.

        Tries multiple selector patterns to accommodate layout variations.
        Deduplicates by stripping tracking query parameters before the
        ``vjk=`` token so the same posting is not fetched twice.

        Args:
            soup: Parsed HTML of an Indeed jobs search results page.

        Returns:
            Deduplicated list of absolute URLs for individual job postings.
        """
        urls: List[str] = []
        selectors = [
            "a[id^='job_']",
            "h2.jobTitle a",
            "a.jcs-JobTitle",
        ]
        for selector in selectors:
            links = soup.select(selector)
            if links:
                for link in links:
                    href = link.get("href", "")
                    if href.startswith("/"):
                        href = urljoin(_INDEED_BASE, href)
                    # Canonicalise: keep only up to the first extra `&` after `jk=`
                    href = self._canonicalise_url(href)
                    if href and href not in urls:
                        urls.append(href)
                break  # Stop after the first selector that yields results

        return urls

    @staticmethod
    def _canonicalise_url(url: str) -> str:
        """Strip non-essential query parameters from an Indeed job URL.

        Keeps ``jk=`` (the job key) and drops click-tracking tokens so
        duplicate-detection based on URL comparison is reliable.

        Args:
            url: Raw href from a search result card.

        Returns:
            Cleaned URL string.
        """
        import re as _re

        match = _re.search(r"jk=[a-zA-Z0-9]+", url)
        if match:
            return f"{_INDEED_BASE}/viewjob?{match.group()}"
        return url

    # ------------------------------------------------------------------
    # Per-page parsing
    # ------------------------------------------------------------------

    def _parse_job_page(self, url: str) -> Optional[JobPosting]:
        """Fetch and parse a single Indeed job posting.

        First attempts to extract structured JSON-LD data (reliable),
        then falls back to CSS-selector-based parsing.

        Args:
            url: Absolute URL of the Indeed job posting.

        Returns:
            A populated :class:`~scraper.base_scraper.JobPosting`, or
            ``None`` on failure.
        """
        logger.debug("[Indeed] Parsing job page: %s", url)

        html = self._get_page(url)
        if not html:
            return None

        save_raw_html(f"indeed_{_url_slug(url)}.html", html)
        soup = BeautifulSoup(html, "lxml")

        structured = self._extract_json_ld(soup)
        if structured:
            return self._build_posting_from_json_ld(structured, url)

        return self._build_posting_from_html(soup, url)

    def _build_posting_from_json_ld(
        self, data: Dict[str, Any], url: str
    ) -> Optional[JobPosting]:
        """Construct a :class:`~scraper.base_scraper.JobPosting` from JSON-LD data.

        Args:
            data: Parsed JSON-LD ``JobPosting`` schema dictionary.
            url: Source URL (used as the application URL).

        Returns:
            Populated :class:`~scraper.base_scraper.JobPosting`, or ``None``.
        """
        job_title = data.get("title", "")
        company_name = self._extract_org_name(data)
        description = data.get("description", "")
        location = self._extract_location(data)
        salary = self._extract_salary_json_ld(data)
        date_posted = self._parse_iso_date(data.get("datePosted", ""))

        description = clean_text(
            BeautifulSoup(description, "lxml").get_text(separator="\n")
            if "<" in description else description
        )

        if not all([job_title, company_name, description]):
            return None

        try:
            return JobPosting(
                job_title=clean_text(job_title),
                company_name=clean_text(company_name),
                location=location,
                job_description=truncate_text(description),
                salary_range=extract_salary_range(salary),
                application_url=url,
                date_posted=date_posted,
                source=self._SOURCE_NAME,
            )
        except ValueError as exc:
            logger.warning("[Indeed] JSON-LD posting invalid for %s: %s", url, exc)
            return None

    def _build_posting_from_html(
        self, soup: BeautifulSoup, url: str
    ) -> Optional[JobPosting]:
        """Construct a posting from raw HTML via CSS selectors (fallback).

        Args:
            soup: Parsed job detail page HTML.
            url: Source URL.

        Returns:
            Populated :class:`~scraper.base_scraper.JobPosting`, or ``None``.
        """
        job_title = self._css_text(
            soup,
            ["h1.jobsearch-JobInfoHeader-title", "h1[data-testid='jobsearch-JobInfoHeader-title']"],
        )
        company_name = self._css_text(
            soup,
            ["div[data-testid='inlineHeader-companyName'] a",
             "div.jobsearch-InlineCompanyRating a"],
        )
        location = self._css_text(
            soup,
            ["div[data-testid='job-location']", "div.jobsearch-JobInfoHeader-subtitle span"],
        )
        desc_tag = soup.select_one("div#jobDescriptionText") or \
                   soup.select_one("div.jobsearch-jobDescriptionText")
        description = desc_tag.get_text(separator="\n", strip=True) if desc_tag else ""

        salary = self._css_text(
            soup,
            ["div[data-testid='attribute_snippet_testid']",
             "span[data-testid='salaryInfoAndJobType']"],
        )

        if not all([job_title, company_name, description]):
            logger.warning(
                "[Indeed] HTML fallback missing fields for %s "
                "(title=%r, company=%r).", url, job_title, company_name,
            )
            return None

        try:
            return JobPosting(
                job_title=clean_text(job_title),
                company_name=clean_text(company_name),
                location=clean_text(location) or None,
                job_description=truncate_text(clean_text(description)),
                salary_range=extract_salary_range(salary),
                application_url=url,
                source=self._SOURCE_NAME,
            )
        except ValueError as exc:
            logger.warning("[Indeed] HTML posting invalid for %s: %s", url, exc)
            return None

    # ------------------------------------------------------------------
    # HTTP helper
    # ------------------------------------------------------------------

    @retry(exceptions=(requests.RequestException,))
    def _get_page(self, url: str) -> Optional[str]:
        """Perform a GET request and return the response body.

        Decorated with :func:`~scraper.utils.retry` so transient network
        errors are retried automatically.

        Raises:
            RateLimitError: On HTTP 429 or 503 (caller should back off).
            BlockedError: When CAPTCHA / bot-detection content is detected.

        Args:
            url: Target URL.

        Returns:
            HTML body as a string, or ``None`` on non-200 HTTP status.
        """
        self._session.headers.update(build_request_headers())
        response = self._session.get(url, timeout=self.config.timeout)

        if response.status_code in (429, 503):
            raise RateLimitError(
                f"Rate limited by Indeed ({response.status_code})",
                url=url,
                status_code=response.status_code,
            )

        if response.status_code == 200:
            body = response.text
            body_lower = body.lower()
            for signal in _BLOCK_SIGNALS:
                if signal in body_lower:
                    raise BlockedError(
                        "Indeed returned a bot-detection page.", url=url
                    )
            return body

        logger.warning("[Indeed] HTTP %d for URL: %s", response.status_code, url)
        return None

    # ------------------------------------------------------------------
    # Extraction helpers
    # ------------------------------------------------------------------

    def _extract_json_ld(self, soup: BeautifulSoup) -> Optional[Dict[str, Any]]:
        """Locate and parse a JSON-LD ``JobPosting`` block in the page.

        Args:
            soup: Parsed page HTML.

        Returns:
            Parsed JSON-LD dictionary, or ``None`` if absent / malformed.
        """
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if isinstance(data, dict) and data.get("@type") == "JobPosting":
                    return data
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("@type") == "JobPosting":
                            return item
            except (json.JSONDecodeError, TypeError):
                continue
        return None

    def _extract_org_name(self, data: Dict[str, Any]) -> str:
        """Extract employer name from JSON-LD data.

        Args:
            data: JSON-LD ``JobPosting`` dictionary.

        Returns:
            Company name string, or empty string if absent.
        """
        hiring_org = data.get("hiringOrganization", {})
        if isinstance(hiring_org, dict):
            return hiring_org.get("name", "")
        return str(hiring_org)

    def _extract_location(self, data: Dict[str, Any]) -> Optional[str]:
        """Extract a location string from JSON-LD job location data.

        Args:
            data: JSON-LD ``JobPosting`` dictionary.

        Returns:
            Human-readable location string, or ``None``.
        """
        job_location = data.get("jobLocation", {})
        if not isinstance(job_location, dict):
            return None
        address = job_location.get("address", {})
        if not isinstance(address, dict):
            return None
        parts = [
            address.get("addressLocality", ""),
            address.get("addressRegion", ""),
            address.get("addressCountry", ""),
        ]
        return ", ".join(part for part in parts if part) or None

    def _extract_salary_json_ld(self, data: Dict[str, Any]) -> Optional[str]:
        """Build a salary range string from JSON-LD ``baseSalary`` data.

        Args:
            data: JSON-LD ``JobPosting`` dictionary.

        Returns:
            Formatted salary string, or ``None``.
        """
        base_salary = data.get("baseSalary", {})
        if not isinstance(base_salary, dict):
            return None
        value = base_salary.get("value", {})
        if not isinstance(value, dict):
            return None
        min_val = value.get("minValue")
        max_val = value.get("maxValue")
        currency = base_salary.get("currency", "USD")
        if min_val and max_val:
            return f"{currency} {min_val:,.0f} – {max_val:,.0f}"
        if min_val or max_val:
            return f"{currency} {min_val or max_val:,.0f}"
        return None

    def _parse_iso_date(self, raw: str) -> Optional[date]:
        """Parse an ISO-8601 date string to a :class:`~datetime.date`.

        Args:
            raw: Date string (e.g. ``"2024-01-15"`` or ``"2024-01-15T00:00:00Z"``).

        Returns:
            :class:`~datetime.date` or ``None`` if the string is not parseable.
        """
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.split("T")[0]).date()
        except ValueError:
            return None

    def _css_text(self, soup: BeautifulSoup, selectors: List[str]) -> Optional[str]:
        """Return the text of the first element matching any selector in *selectors*.

        Args:
            soup: Parsed HTML.
            selectors: Ordered list of CSS selectors to try.

        Returns:
            Stripped text of the first match, or ``None``.
        """
        for selector in selectors:
            element = soup.select_one(selector)
            if element:
                return element.get_text(strip=True)
        return None


def _url_slug(url: str) -> str:
    """Derive a safe filename slug from a URL.

    Args:
        url: Any absolute URL string.

    Returns:
        A filesystem-safe slug (max 50 characters).
    """
    slug = url.replace("https://", "").replace("http://", "").replace("/", "_")
    return slug[:50]
