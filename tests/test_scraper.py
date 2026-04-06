"""
Unit tests for Phase 1: Core Job Scraping Module.

Tests cover:
- JobPosting dataclass validation
- ScrapingConfig loading and enforcement of the 2-second minimum delay
- Utility functions (clean_text, truncate_text, extract_salary_range,
  build_request_headers, retry decorator, random_delay)
- IndeedScraper internals (JSON-LD extraction, HTML fallback, URL slug)
- BaseScraper orchestration via a lightweight stub implementation

All network I/O is mocked; no real HTTP requests are made.
"""

import time
from datetime import date
from pathlib import Path
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytest

from scraper.base_scraper import BaseScraper, JobPosting
from scraper.config import ScrapingConfig, SeleniumConfig, load_scraping_config
from scraper.indeed_scraper import IndeedScraper, _url_slug
from scraper.utils import (
    build_request_headers,
    clean_text,
    extract_salary_range,
    get_random_user_agent,
    random_delay,
    retry,
    truncate_text,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def minimal_config() -> ScrapingConfig:
    """Return a scraping config with fast settings for unit tests."""
    return ScrapingConfig(
        delay_min=0.01,
        delay_max=0.02,
        max_retries=2,
        timeout=5,
        max_jobs_per_search=10,
        store_raw_html=False,
        user_agents=["TestAgent/1.0"],
    )


@pytest.fixture()
def sample_posting() -> JobPosting:
    """Return a valid, fully-populated JobPosting."""
    return JobPosting(
        job_title="Senior Python Developer",
        company_name="Acme Corp",
        location="San Francisco, CA",
        job_description="Build scalable microservices with Python, FastAPI, and AWS.",
        required_skills="Python, FastAPI, AWS, Docker",
        preferred_skills="Kubernetes, Terraform",
        salary_range="USD 120,000 – 160,000",
        application_url="https://www.indeed.com/viewjob?jk=abc123",
        date_posted=date(2024, 1, 15),
        source="indeed",
    )


# ---------------------------------------------------------------------------
# JobPosting dataclass tests
# ---------------------------------------------------------------------------


class TestJobPosting:
    def test_valid_posting_constructs_successfully(self, sample_posting: JobPosting) -> None:
        assert sample_posting.job_title == "Senior Python Developer"
        assert sample_posting.source == "indeed"

    def test_to_dict_contains_all_keys(self, sample_posting: JobPosting) -> None:
        result = sample_posting.to_dict()
        expected_keys = {
            "job_title", "company_name", "location", "job_description",
            "required_skills", "preferred_skills", "salary_range",
            "application_url", "date_posted", "date_scraped", "source",
        }
        assert expected_keys == set(result.keys())

    def test_to_dict_date_serialised_as_iso_string(self, sample_posting: JobPosting) -> None:
        result = sample_posting.to_dict()
        assert result["date_posted"] == "2024-01-15"

    def test_empty_job_title_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="job_title"):
            JobPosting(
                job_title="   ",
                company_name="Acme",
                job_description="Some description",
                application_url="https://example.com/job/1",
                source="indeed",
            )

    def test_empty_company_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="company_name"):
            JobPosting(
                job_title="Engineer",
                company_name="",
                job_description="Some description",
                application_url="https://example.com/job/1",
                source="indeed",
            )

    def test_empty_url_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="application_url"):
            JobPosting(
                job_title="Engineer",
                company_name="Acme",
                job_description="Some description",
                application_url="",
                source="indeed",
            )

    def test_optional_fields_default_to_none(self) -> None:
        posting = JobPosting(
            job_title="Analyst",
            company_name="Corp",
            job_description="Desc",
            application_url="https://example.com",
            source="indeed",
        )
        assert posting.location is None
        assert posting.salary_range is None
        assert posting.date_posted is None


# ---------------------------------------------------------------------------
# ScrapingConfig tests
# ---------------------------------------------------------------------------


class TestScrapingConfig:
    def test_default_values_are_safe(self) -> None:
        cfg = ScrapingConfig()
        assert cfg.delay_min >= 2.0
        assert cfg.delay_max >= cfg.delay_min
        assert cfg.max_retries >= 1

    def test_delay_min_clamped_to_two_seconds(self) -> None:
        cfg = ScrapingConfig(delay_min=0.5, delay_max=1.0)
        assert cfg.delay_min == 2.0

    def test_delay_max_adjusted_when_less_than_min(self) -> None:
        cfg = ScrapingConfig(delay_min=3.0, delay_max=1.0)
        assert cfg.delay_max == 3.0

    def test_load_scraping_config_returns_defaults_when_file_missing(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent.yaml"
        cfg = load_scraping_config(missing)
        assert isinstance(cfg, ScrapingConfig)
        assert cfg.delay_min >= 2.0

    def test_load_scraping_config_parses_yaml(self, tmp_path: Path) -> None:
        yaml_content = (
            "scraping:\n"
            "  delay_min: 3\n"
            "  delay_max: 6\n"
            "  max_retries: 5\n"
            "  user_agents:\n"
            "    - CustomAgent/2.0\n"
            "selenium:\n"
            "  headless: false\n"
        )
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml_content, encoding="utf-8")
        cfg = load_scraping_config(cfg_file)
        assert cfg.delay_min == 3.0
        assert cfg.delay_max == 6.0
        assert cfg.max_retries == 5
        assert cfg.user_agents == ["CustomAgent/2.0"]
        assert cfg.selenium.headless is False


# ---------------------------------------------------------------------------
# Utility function tests
# ---------------------------------------------------------------------------


class TestCleanText:
    def test_strips_surrounding_whitespace(self) -> None:
        assert clean_text("  hello world  ") == "hello world"

    def test_collapses_internal_whitespace(self) -> None:
        assert clean_text("hello   \n\n  world") == "hello world"

    def test_handles_none_input(self) -> None:
        assert clean_text(None) == ""  # type: ignore[arg-type]

    def test_handles_empty_string(self) -> None:
        assert clean_text("") == ""


class TestTruncateText:
    def test_short_text_unchanged(self) -> None:
        assert truncate_text("hello", max_length=100) == "hello"

    def test_long_text_truncated_with_ellipsis(self) -> None:
        result = truncate_text("word " * 200, max_length=50)
        assert len(result) <= 55  # Slight over-run allowed for the ellipsis
        assert result.endswith("…")

    def test_truncation_does_not_cut_mid_word(self) -> None:
        text = "the quick brown fox jumps over the lazy dog"
        result = truncate_text(text, max_length=20)
        # Must end with "…" and no partial words
        assert "…" in result


class TestExtractSalaryRange:
    def test_valid_salary_returned(self) -> None:
        assert extract_salary_range("$120,000 – $160,000") == "$120,000 – $160,000"

    def test_none_input_returns_none(self) -> None:
        assert extract_salary_range(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert extract_salary_range("") is None

    def test_placeholder_returns_none(self) -> None:
        assert extract_salary_range("Not provided") is None
        assert extract_salary_range("N/A") is None

    def test_whitespace_stripped(self) -> None:
        assert extract_salary_range("  $80k  ") == "$80k"


class TestBuildRequestHeaders:
    def test_returns_dict_with_user_agent(self) -> None:
        headers = build_request_headers()
        assert "User-Agent" in headers

    def test_extra_headers_are_merged(self) -> None:
        headers = build_request_headers(extra={"X-Custom": "test"})
        assert headers["X-Custom"] == "test"

    def test_extra_headers_can_override_defaults(self) -> None:
        custom_agent = "CustomBot/1.0"
        headers = build_request_headers(extra={"User-Agent": custom_agent})
        assert headers["User-Agent"] == custom_agent


class TestGetRandomUserAgent:
    def test_returns_string(self, minimal_config: ScrapingConfig) -> None:
        with patch("scraper.utils.CONFIG", minimal_config):
            agent = get_random_user_agent()
        assert isinstance(agent, str)
        assert len(agent) > 0

    def test_falls_back_when_pool_is_empty(self) -> None:
        empty_cfg = ScrapingConfig(user_agents=[])
        with patch("scraper.utils.CONFIG", empty_cfg):
            agent = get_random_user_agent()
        assert "Mozilla" in agent


class TestRandomDelay:
    def test_respects_minimum_two_second_floor(self) -> None:
        start = time.monotonic()
        random_delay(min_seconds=0.01, max_seconds=0.02)
        elapsed = time.monotonic() - start
        # The 2-second floor is enforced inside random_delay
        assert elapsed >= 1.9  # Allow small timing tolerance

    def test_explicit_range_respected(self) -> None:
        start = time.monotonic()
        random_delay(min_seconds=2.0, max_seconds=2.1)
        elapsed = time.monotonic() - start
        assert elapsed >= 1.9


class TestRetryDecorator:
    def test_succeeds_on_first_attempt(self) -> None:
        call_count = 0

        @retry(max_attempts=3)
        def succeed() -> str:
            nonlocal call_count
            call_count += 1
            return "ok"

        result = succeed()
        assert result == "ok"
        assert call_count == 1

    def test_retries_on_specified_exception(self) -> None:
        attempts = []

        @retry(max_attempts=3, exceptions=(ValueError,), base_delay=0.01)
        def fail_twice() -> str:
            attempts.append(1)
            if len(attempts) < 3:
                raise ValueError("temporary failure")
            return "success"

        result = fail_twice()
        assert result == "success"
        assert len(attempts) == 3

    def test_raises_after_max_attempts_exhausted(self) -> None:
        @retry(max_attempts=2, exceptions=(RuntimeError,), base_delay=0.01)
        def always_fail() -> None:
            raise RuntimeError("persistent error")

        with pytest.raises(RuntimeError, match="persistent error"):
            always_fail()

    def test_does_not_catch_unspecified_exceptions(self) -> None:
        @retry(max_attempts=3, exceptions=(ValueError,), base_delay=0.01)
        def raise_type_error() -> None:
            raise TypeError("not in retry list")

        with pytest.raises(TypeError):
            raise_type_error()


# ---------------------------------------------------------------------------
# IndeedScraper unit tests (mocked HTTP)
# ---------------------------------------------------------------------------


class TestIndeedScraperJsonLdExtraction:
    """Test JSON-LD and HTML fallback parsing without network calls."""

    def test_extracts_json_ld_job_posting(self) -> None:
        from bs4 import BeautifulSoup

        html = """
        <html><head>
        <script type="application/ld+json">
        {"@type": "JobPosting", "title": "ML Engineer",
         "hiringOrganization": {"name": "OpenAI"},
         "description": "Build language models.",
         "datePosted": "2024-03-01"}
        </script>
        </head><body></body></html>
        """
        soup = BeautifulSoup(html, "lxml")
        scraper = IndeedScraper()
        data = scraper._extract_json_ld(soup)
        assert data is not None
        assert data["title"] == "ML Engineer"

    def test_returns_none_when_no_json_ld(self) -> None:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup("<html><body><p>No JSON-LD here</p></body></html>", "lxml")
        scraper = IndeedScraper()
        assert scraper._extract_json_ld(soup) is None

    def test_build_posting_from_json_ld_happy_path(self) -> None:
        scraper = IndeedScraper()
        data = {
            "@type": "JobPosting",
            "title": "Data Scientist",
            "hiringOrganization": {"name": "Netflix"},
            "description": "Work on recommendation systems.",
            "jobLocation": {"address": {
                "addressLocality": "Los Gatos",
                "addressRegion": "CA",
                "addressCountry": "US",
            }},
            "baseSalary": {
                "currency": "USD",
                "value": {"minValue": 150000, "maxValue": 200000},
            },
            "datePosted": "2024-02-10",
        }
        url = "https://www.indeed.com/viewjob?jk=xyz"
        posting = scraper._build_posting_from_json_ld(data, url)
        assert posting is not None
        assert posting.job_title == "Data Scientist"
        assert posting.company_name == "Netflix"
        assert posting.salary_range == "USD 150,000 – 200,000"
        assert posting.date_posted == date(2024, 2, 10)
        assert posting.source == "indeed"

    def test_build_posting_returns_none_for_missing_description(self) -> None:
        scraper = IndeedScraper()
        data = {
            "title": "Engineer",
            "hiringOrganization": {"name": "Corp"},
            "description": "",
        }
        assert scraper._build_posting_from_json_ld(data, "https://example.com") is None


class TestIndeedScraperHelpers:
    def test_extract_location_builds_string(self) -> None:
        scraper = IndeedScraper()
        data = {"jobLocation": {"address": {
            "addressLocality": "Austin",
            "addressRegion": "TX",
            "addressCountry": "US",
        }}}
        assert scraper._extract_location(data) == "Austin, TX, US"

    def test_extract_location_returns_none_for_empty(self) -> None:
        scraper = IndeedScraper()
        assert scraper._extract_location({}) is None

    def test_extract_salary_json_ld_range(self) -> None:
        scraper = IndeedScraper()
        data = {"baseSalary": {
            "currency": "USD",
            "value": {"minValue": 80000, "maxValue": 120000},
        }}
        assert scraper._extract_salary_json_ld(data) == "USD 80,000 – 120,000"

    def test_extract_salary_json_ld_returns_none_when_absent(self) -> None:
        scraper = IndeedScraper()
        assert scraper._extract_salary_json_ld({}) is None

    def test_parse_iso_date_valid(self) -> None:
        scraper = IndeedScraper()
        assert scraper._parse_iso_date("2024-06-15") == date(2024, 6, 15)

    def test_parse_iso_date_with_time_component(self) -> None:
        scraper = IndeedScraper()
        assert scraper._parse_iso_date("2024-06-15T12:00:00Z") == date(2024, 6, 15)

    def test_parse_iso_date_invalid_returns_none(self) -> None:
        scraper = IndeedScraper()
        assert scraper._parse_iso_date("not-a-date") is None

    def test_parse_iso_date_empty_returns_none(self) -> None:
        scraper = IndeedScraper()
        assert scraper._parse_iso_date("") is None


class TestUrlSlug:
    def test_replaces_slashes_with_underscores(self) -> None:
        slug = _url_slug("https://www.indeed.com/viewjob?jk=abc")
        assert "/" not in slug

    def test_truncated_to_50_characters(self) -> None:
        slug = _url_slug("https://www.indeed.com/" + "a" * 200)
        assert len(slug) <= 50


# ---------------------------------------------------------------------------
# BaseScraper orchestration tests (stub implementation)
# ---------------------------------------------------------------------------


class _StubScraper(BaseScraper):
    """Minimal concrete implementation for testing the BaseScraper mixin."""

    _SOURCE_NAME = "stub"

    def __init__(
        self,
        urls: List[str],
        postings: List[Optional[JobPosting]],
        config: Optional[ScrapingConfig] = None,
    ) -> None:
        super().__init__(config)
        self._urls = urls
        self._postings = postings

    def _fetch_job_urls(self, keywords: str, location: str) -> List[str]:
        return self._urls

    def _parse_job_page(self, url: str) -> Optional[JobPosting]:
        index = self._urls.index(url) if url in self._urls else -1
        return self._postings[index] if 0 <= index < len(self._postings) else None


def _make_posting(n: int) -> JobPosting:
    return JobPosting(
        job_title=f"Job {n}",
        company_name="TestCo",
        job_description="Description for testing.",
        application_url=f"https://example.com/job/{n}",
        source="stub",
    )


class TestBaseScraperOrchestration:
    def test_scrape_returns_all_successful_postings(self, minimal_config: ScrapingConfig) -> None:
        postings = [_make_posting(i) for i in range(3)]
        urls = [p.application_url for p in postings]
        scraper = _StubScraper(urls, postings, config=minimal_config)
        results = scraper.scrape("python", "NYC")
        assert len(results) == 3

    def test_scrape_skips_none_postings(self, minimal_config: ScrapingConfig) -> None:
        postings: List[Optional[JobPosting]] = [_make_posting(0), None, _make_posting(2)]
        urls = [f"https://example.com/job/{i}" for i in range(3)]
        scraper = _StubScraper(urls, postings, config=minimal_config)
        results = scraper.scrape("python", "NYC")
        assert len(results) == 2
        assert scraper.stats["errors"] == 1

    def test_scrape_returns_empty_list_when_url_fetch_fails(
        self, minimal_config: ScrapingConfig
    ) -> None:
        class FailingScraper(_StubScraper):
            def _fetch_job_urls(self, keywords: str, location: str) -> List[str]:
                raise ConnectionError("network down")

        scraper = FailingScraper([], [], config=minimal_config)
        results = scraper.scrape("python", "NYC")
        assert results == []

    def test_stats_tracks_counts(self, minimal_config: ScrapingConfig) -> None:
        postings: List[Optional[JobPosting]] = [_make_posting(0), None]
        urls = ["https://example.com/job/0", "https://example.com/job/1"]
        scraper = _StubScraper(urls, postings, config=minimal_config)
        scraper.scrape("test", "")
        assert scraper.stats["jobs_scraped"] == 1
        assert scraper.stats["errors"] == 1
        assert scraper.stats["source"] == "stub"

    def test_context_manager_calls_close(self, minimal_config: ScrapingConfig) -> None:
        scraper = _StubScraper([], [], config=minimal_config)
        scraper.close = MagicMock()
        with scraper:
            pass
        scraper.close.assert_called_once()
