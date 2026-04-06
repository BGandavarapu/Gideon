"""
LinkedIn job scraper using Selenium WebDriver.

LinkedIn renders its job listings with heavy JavaScript, so we use a
headless Chrome browser to fully execute the page before parsing with
BeautifulSoup.  The scraper targets LinkedIn's public job search page
(no login required for basic listings).

Usage:
    >>> from scraper.linkedin_scraper import LinkedInScraper
    >>> with LinkedInScraper() as scraper:
    ...     jobs = scraper.scrape("python developer", "San Francisco, CA")
"""

import logging
import time
from datetime import date, datetime
from typing import List, Optional
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

from scraper.base_scraper import BaseScraper, JobPosting
from scraper.config import ScrapingConfig
from scraper.utils import clean_text, extract_salary_range, random_delay, save_raw_html

logger = logging.getLogger(__name__)

_LINKEDIN_BASE = "https://www.linkedin.com"
_JOBS_SEARCH_URL = (
    "https://www.linkedin.com/jobs/search/"
    "?keywords={keywords}&location={location}&f_TPR=r86400&start={start}"
)
_JOB_CARD_SELECTOR = "div.base-card"
_JOB_LINK_SELECTOR = "a.base-card__full-link"
_LOAD_MORE_PAUSE = 2.0  # Extra pause after scrolling to load dynamic content


class LinkedInScraper(BaseScraper):
    """Selenium-based scraper for LinkedIn public job listings.

    Operates entirely on the public (non-authenticated) job search endpoint,
    which LinkedIn makes available without a login for basic job data.

    Args:
        config: Optional custom :class:`~scraper.config.ScrapingConfig`.

    Attributes:
        driver: The Selenium :class:`~selenium.webdriver.Chrome` instance,
            lazily initialised on first use and torn down by :meth:`close`.
    """

    _SOURCE_NAME = "linkedin"

    def __init__(self, config: Optional[ScrapingConfig] = None) -> None:
        super().__init__(config)
        self._driver: Optional[webdriver.Chrome] = None

    # ------------------------------------------------------------------
    # Driver lifecycle
    # ------------------------------------------------------------------

    def _get_driver(self) -> webdriver.Chrome:
        """Return the shared WebDriver, initialising it on first call.

        Returns:
            A configured headless Chrome WebDriver instance.

        Raises:
            WebDriverException: If Chrome or ChromeDriver cannot be started.
        """
        if self._driver is not None:
            return self._driver

        chrome_options = Options()
        if self.config.selenium.headless:
            chrome_options.add_argument("--headless=new")

        chrome_options.add_argument(
            f"--window-size={self.config.selenium.window_width},"
            f"{self.config.selenium.window_height}"
        )
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option("useAutomationExtension", False)

        service = Service(ChromeDriverManager().install())
        self._driver = webdriver.Chrome(service=service, options=chrome_options)
        self._driver.set_page_load_timeout(self.config.selenium.page_load_timeout)
        self._driver.implicitly_wait(self.config.selenium.implicit_wait)

        logger.debug("Chrome WebDriver initialised (headless=%s).", self.config.selenium.headless)
        return self._driver

    def close(self) -> None:
        """Quit the WebDriver and release the browser process."""
        if self._driver is not None:
            try:
                self._driver.quit()
                logger.debug("Chrome WebDriver closed.")
            except WebDriverException as exc:
                logger.warning("Error closing WebDriver: %s", exc)
            finally:
                self._driver = None

    # ------------------------------------------------------------------
    # URL collection
    # ------------------------------------------------------------------

    def _fetch_job_urls(self, keywords: str, location: str) -> List[str]:
        """Collect individual job-posting URLs from LinkedIn search results.

        Paginates through the search results page (25 cards per page) until
        :attr:`~scraper.config.ScrapingConfig.max_jobs_per_search` URLs have
        been collected or no more results are available.

        Args:
            keywords: Job search terms.
            location: Geographic filter.

        Returns:
            Deduplicated list of absolute LinkedIn job URLs.
        """
        driver = self._get_driver()
        collected_urls: List[str] = []
        start = 0
        page = 1

        while len(collected_urls) < self.config.max_jobs_per_search:
            search_url = _JOBS_SEARCH_URL.format(
                keywords=quote_plus(keywords),
                location=quote_plus(location),
                start=start,
            )
            logger.debug("[LinkedIn] Loading search page %d: %s", page, search_url)

            try:
                driver.get(search_url)
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, _JOB_CARD_SELECTOR))
                )
            except TimeoutException:
                logger.info("[LinkedIn] No job cards found on page %d – stopping.", page)
                break
            except WebDriverException as exc:
                logger.error("[LinkedIn] Failed to load search page %d: %s", page, exc)
                if "invalid session" in str(exc).lower():
                    logger.info("[LinkedIn] Session died — will create fresh driver on next call.")
                    self._driver = None
                break

            self._scroll_to_load_all_cards(driver)

            soup = BeautifulSoup(driver.page_source, "lxml")
            page_urls = self._extract_urls_from_soup(soup)

            if not page_urls:
                logger.info("[LinkedIn] Empty result page %d – stopping pagination.", page)
                break

            for url in page_urls:
                if url not in collected_urls:
                    collected_urls.append(url)
                if len(collected_urls) >= self.config.max_jobs_per_search:
                    break

            logger.debug("[LinkedIn] Page %d yielded %d URLs (total so far: %d).",
                         page, len(page_urls), len(collected_urls))

            start += 25
            page += 1
            random_delay()

        return collected_urls[: self.config.max_jobs_per_search]

    def _scroll_to_load_all_cards(self, driver: webdriver.Chrome) -> None:
        """Scroll to the bottom of the search page to trigger lazy loading.

        Args:
            driver: Active WebDriver instance.
        """
        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(_LOAD_MORE_PAUSE)
        except WebDriverException as exc:
            logger.debug("[LinkedIn] Scroll failed (non-critical): %s", exc)

    def _extract_urls_from_soup(self, soup: BeautifulSoup) -> List[str]:
        """Parse job card URLs from a BeautifulSoup-parsed search page.

        Args:
            soup: Parsed HTML of a LinkedIn jobs search results page.

        Returns:
            List of absolute LinkedIn job posting URLs.
        """
        urls: List[str] = []
        for link_tag in soup.select(_JOB_LINK_SELECTOR):
            href = link_tag.get("href", "")
            if href and "/jobs/view/" in href:
                absolute = href.split("?")[0]  # Strip tracking params
                if absolute not in urls:
                    urls.append(absolute)
        return urls

    # ------------------------------------------------------------------
    # Per-page parsing
    # ------------------------------------------------------------------

    def _parse_job_page(self, url: str) -> Optional[JobPosting]:
        """Load and parse a single LinkedIn job posting page.

        Args:
            url: Absolute URL of the LinkedIn job posting.

        Returns:
            A populated :class:`~scraper.base_scraper.JobPosting`, or
            ``None`` if any critical field cannot be extracted.
        """
        driver = self._get_driver()
        logger.debug("[LinkedIn] Parsing job page: %s", url)

        try:
            driver.get(url)
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "h1.top-card-layout__title"))
            )
        except TimeoutException:
            logger.warning("[LinkedIn] Timed out waiting for job page: %s", url)
            return None
        except WebDriverException as exc:
            logger.warning("[LinkedIn] Failed to load job page %s: %s", url, exc)
            if "invalid session" in str(exc).lower():
                logger.info("[LinkedIn] Session died — will create fresh driver on next call.")
                self._driver = None
            return None

        html = driver.page_source
        save_raw_html(f"linkedin_{url.split('/')[-1]}.html", html)

        soup = BeautifulSoup(html, "lxml")

        job_title = self._extract_text(soup, "h1.top-card-layout__title")
        company_name = self._extract_text(soup, "a.topcard__org-name-link") or \
                       self._extract_text(soup, "span.topcard__flavor")
        location = self._extract_text(soup, "span.topcard__flavor--bullet")
        description = self._extract_description(soup)
        salary = self._extract_salary(soup)
        date_posted = self._extract_date_posted(soup)

        if not job_title or not company_name or not description:
            logger.warning(
                "[LinkedIn] Missing critical fields on %s "
                "(title=%r, company=%r, desc_len=%d).",
                url, job_title, company_name, len(description or ""),
            )
            return None

        try:
            posting = JobPosting(
                job_title=clean_text(job_title),
                company_name=clean_text(company_name),
                location=clean_text(location) or None,
                job_description=clean_text(description),
                salary_range=extract_salary_range(salary),
                application_url=url,
                date_posted=date_posted,
                source=self._SOURCE_NAME,
            )
        except ValueError as exc:
            logger.warning("[LinkedIn] Could not construct JobPosting for %s: %s", url, exc)
            return None

        random_delay()
        return posting

    # ------------------------------------------------------------------
    # Field extraction helpers
    # ------------------------------------------------------------------

    def _extract_text(self, soup: BeautifulSoup, selector: str) -> Optional[str]:
        """Return stripped inner text of the first element matching *selector*.

        Args:
            soup: Parsed page HTML.
            selector: CSS selector string.

        Returns:
            Stripped text, or ``None`` if the element is absent.
        """
        element = soup.select_one(selector)
        return element.get_text(strip=True) if element else None

    def _extract_description(self, soup: BeautifulSoup) -> Optional[str]:
        """Extract the full job description text.

        Args:
            soup: Parsed job page HTML.

        Returns:
            Full job description as a plain-text string, or ``None``.
        """
        desc_div = soup.select_one("div.description__text") or \
                   soup.select_one("div.show-more-less-html__markup")
        if not desc_div:
            return None
        return desc_div.get_text(separator="\n", strip=True)

    def _extract_salary(self, soup: BeautifulSoup) -> Optional[str]:
        """Attempt to extract salary information from the job page.

        Args:
            soup: Parsed job page HTML.

        Returns:
            Raw salary string, or ``None`` if not listed.
        """
        salary_element = soup.select_one("span.compensation__salary") or \
                         soup.select_one("div.salary-main-rail__salary-info")
        return salary_element.get_text(strip=True) if salary_element else None

    def _extract_date_posted(self, soup: BeautifulSoup) -> Optional[date]:
        """Parse the posting date from a ``<time>`` element or text heuristic.

        Args:
            soup: Parsed job page HTML.

        Returns:
            Parsed :class:`~datetime.date`, or ``None`` if unparseable.
        """
        time_tag = soup.find("time")
        if time_tag:
            datetime_attr = time_tag.get("datetime", "")
            if datetime_attr:
                try:
                    return datetime.fromisoformat(datetime_attr.split("T")[0]).date()
                except ValueError:
                    pass
        return None
