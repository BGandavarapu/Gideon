"""
Tests for scraper/db_handler.py (Phase 1 + 2 integration).

All tests use an in-memory SQLite database via ``reset_manager`` so they
are fast, isolated, and never touch the filesystem.

Test classes
------------
TestSaveJobToDb          – insert / upsert / field-update behaviour
TestMergeSkills          – skill list merging edge cases
TestSavePostingsToBatch  – batch persistence, error isolation, progress CB
TestGetHelpers           – get_jobs_by_status, get_job_count
TestExceptions           – DatabasePersistenceError propagation
TestUtilsExtensions      – extract_relative_date, clean_html_text
"""

import re
from datetime import date, timedelta, timezone
from datetime import datetime as dt
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from database.database import reset_manager
from database.models import Job
from scraper.base_scraper import JobPosting
from scraper.db_handler import (
    BatchResult,
    _merge_skills,
    _posting_to_job_kwargs,
    get_job_count,
    get_jobs_by_status,
    save_job_to_db,
    save_postings_to_db,
)
from scraper.exceptions import DatabasePersistenceError
from scraper.utils import clean_html_text, extract_relative_date

IN_MEMORY = "sqlite:///:memory:"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_db():
    """Give every test its own clean in-memory database."""
    reset_manager(IN_MEMORY)
    from database.database import create_tables
    create_tables()
    yield
    from database.database import drop_tables
    drop_tables()


def _make_posting(
    n: int = 1,
    *,
    title: str = "Python Developer",
    company: str = "Acme Corp",
    source: str = "indeed",
    required_skills=None,
    preferred_skills=None,
    salary: Optional[str] = None,
    date_posted: Optional[date] = None,
) -> JobPosting:
    return JobPosting(
        job_title=f"{title} {n}",
        company_name=company,
        location="San Francisco, CA",
        job_description=f"Write Python code. Job #{n}.",
        application_url=f"https://www.indeed.com/viewjob?jk=abc{n:04d}",
        source=source,
        required_skills=required_skills,
        preferred_skills=preferred_skills,
        salary_range=salary,
        date_posted=date_posted,
    )


# ---------------------------------------------------------------------------
# save_job_to_db – insert behaviour
# ---------------------------------------------------------------------------


class TestSaveJobToDb:
    def test_insert_new_job_returns_is_new_true(self) -> None:
        posting = _make_posting(1)
        job, is_new = save_job_to_db(posting)
        assert is_new is True
        assert job.job_title == "Python Developer 1"

    def test_inserted_job_has_status_new(self) -> None:
        posting = _make_posting(2)
        job, _ = save_job_to_db(posting)
        assert job.status == "new"

    def test_inserted_job_has_date_scraped(self) -> None:
        posting = _make_posting(3)
        job, _ = save_job_to_db(posting)
        assert job.date_scraped is not None

    def test_insert_persists_all_fields(self) -> None:
        posting = _make_posting(
            4,
            required_skills=["Python", "AWS"],
            salary="USD 120,000",
            date_posted=date(2024, 5, 1),
        )
        job, _ = save_job_to_db(posting)
        assert job.required_skills == ["Python", "AWS"]
        assert job.salary_range == "USD 120,000"
        assert job.date_posted == date(2024, 5, 1)

    def test_duplicate_url_returns_is_new_false(self) -> None:
        posting = _make_posting(5)
        save_job_to_db(posting)          # first insert
        _, is_new = save_job_to_db(posting)  # second call same URL
        assert is_new is False

    def test_upsert_refreshes_date_scraped(self) -> None:
        posting = _make_posting(6)
        job1, _ = save_job_to_db(posting)
        first_scraped = job1.date_scraped

        # Small pause to ensure timestamp changes
        import time; time.sleep(0.01)
        job2, is_new = save_job_to_db(posting)
        assert is_new is False
        assert job2.date_scraped >= first_scraped

    def test_upsert_updates_job_title(self) -> None:
        posting = _make_posting(7)
        save_job_to_db(posting)

        updated = JobPosting(
            job_title="Senior Python Developer 7",   # changed
            company_name=posting.company_name,
            job_description=posting.job_description,
            application_url=posting.application_url,
            source=posting.source,
        )
        job, is_new = save_job_to_db(updated)
        assert is_new is False
        assert job.job_title == "Senior Python Developer 7"

    def test_upsert_preserves_status_if_not_new(self) -> None:
        posting = _make_posting(8)
        save_job_to_db(posting)

        # Manually advance status
        from database.database import get_db
        with get_db() as db:
            j = db.query(Job).filter_by(application_url=posting.application_url).first()
            j.status = "analyzed"

        # Re-scrape same URL
        job, _ = save_job_to_db(posting)
        assert job.status == "analyzed"   # status should NOT be reset

    def test_upsert_merges_new_skills(self) -> None:
        posting = _make_posting(9, required_skills=["Python"])
        save_job_to_db(posting)

        updated = JobPosting(
            job_title=posting.job_title,
            company_name=posting.company_name,
            job_description=posting.job_description,
            application_url=posting.application_url,
            source=posting.source,
            required_skills=["Python", "Docker"],  # Docker is new
        )
        job, _ = save_job_to_db(updated)
        assert "Docker" in job.required_skills
        assert "Python" in job.required_skills

    def test_string_required_skills_converted_to_list(self) -> None:
        posting = _make_posting(10, required_skills="Python, AWS, Docker")
        job, _ = save_job_to_db(posting)
        assert isinstance(job.required_skills, list)
        assert "AWS" in job.required_skills


# ---------------------------------------------------------------------------
# _posting_to_job_kwargs
# ---------------------------------------------------------------------------


class TestPostingToJobKwargs:
    def test_returns_dict_with_expected_keys(self) -> None:
        kwargs = _posting_to_job_kwargs(_make_posting(1))
        expected = {
            "job_title", "company_name", "location", "job_description",
            "required_skills", "preferred_skills", "salary_range",
            "application_url", "date_posted", "date_scraped", "source", "status",
        }
        assert set(kwargs.keys()) == expected

    def test_status_is_always_new(self) -> None:
        kwargs = _posting_to_job_kwargs(_make_posting(1))
        assert kwargs["status"] == "new"

    def test_string_skills_converted_to_list(self) -> None:
        kwargs = _posting_to_job_kwargs(_make_posting(1, required_skills="A, B, C"))
        assert kwargs["required_skills"] == ["A", "B", "C"]

    def test_list_skills_kept_as_list(self) -> None:
        kwargs = _posting_to_job_kwargs(_make_posting(1, required_skills=["X", "Y"]))
        assert kwargs["required_skills"] == ["X", "Y"]

    def test_none_skills_stay_none(self) -> None:
        kwargs = _posting_to_job_kwargs(_make_posting(1))
        assert kwargs["required_skills"] is None


# ---------------------------------------------------------------------------
# _merge_skills
# ---------------------------------------------------------------------------


class TestMergeSkills:
    def test_returns_incoming_when_existing_is_none(self) -> None:
        assert _merge_skills(None, ["Python"]) == ["Python"]

    def test_returns_none_when_both_empty(self) -> None:
        assert _merge_skills(None, None) is None
        assert _merge_skills([], []) is None

    def test_deduplicates_case_insensitively(self) -> None:
        result = _merge_skills(["Python", "AWS"], ["python", "Docker"])
        assert result.count("Python") == 1    # not duplicated
        assert "Docker" in result

    def test_preserves_existing_items(self) -> None:
        result = _merge_skills(["SQL", "Spark"], ["Python"])
        assert "SQL" in result
        assert "Spark" in result
        assert "Python" in result

    def test_converts_string_incoming_to_list(self) -> None:
        result = _merge_skills(["Python"], "AWS, Docker")
        assert "AWS" in result
        assert "Docker" in result

    def test_returns_existing_when_incoming_is_none(self) -> None:
        assert _merge_skills(["Python"], None) == ["Python"]


# ---------------------------------------------------------------------------
# save_postings_to_db – batch behaviour
# ---------------------------------------------------------------------------


class TestSavePostingsToBatch:
    def test_batch_inserts_all_postings(self) -> None:
        postings = [_make_posting(i) for i in range(1, 6)]
        result = save_postings_to_db(postings)
        assert result.saved == 5
        assert result.updated == 0
        assert result.failed == 0

    def test_batch_returns_batch_result_instance(self) -> None:
        result = save_postings_to_db([_make_posting(1)])
        assert isinstance(result, BatchResult)

    def test_batch_counts_updates_correctly(self) -> None:
        posting = _make_posting(1)
        save_postings_to_db([posting])          # first pass: 1 saved
        result = save_postings_to_db([posting]) # second pass: 1 updated
        assert result.saved == 0
        assert result.updated == 1

    def test_batch_total_processed(self) -> None:
        postings = [_make_posting(i) for i in range(1, 4)]
        result = save_postings_to_db(postings)
        assert result.total_processed == 3

    def test_batch_continues_past_individual_failure(self) -> None:
        good = _make_posting(1)
        bad = _make_posting(2)

        # Make save_job_to_db raise for only the bad posting
        original_save = save_job_to_db

        def selective_fail(p: JobPosting):
            if "abc0002" in p.application_url:
                raise DatabasePersistenceError("forced failure", url=p.application_url)
            return original_save(p)

        with patch("scraper.db_handler.save_job_to_db", side_effect=selective_fail):
            result = save_postings_to_db([good, bad])

        assert result.saved == 1
        assert result.failed == 1
        assert len(result.errors) == 1

    def test_progress_callback_called_for_each_posting(self) -> None:
        calls: list = []

        def cb(idx, total, posting, is_new):
            calls.append((idx, total, is_new))

        postings = [_make_posting(i) for i in range(1, 4)]
        save_postings_to_db(postings, on_progress=cb)
        assert len(calls) == 3
        assert calls[-1][0] == 3   # last idx

    def test_progress_callback_receives_none_on_failure(self) -> None:
        calls: list = []

        def cb(idx, total, posting, is_new):
            calls.append(is_new)

        posting = _make_posting(1)
        with patch(
            "scraper.db_handler.save_job_to_db",
            side_effect=DatabasePersistenceError("fail"),
        ):
            save_postings_to_db([posting], on_progress=cb)

        assert calls == [None]

    def test_batch_result_str(self) -> None:
        result = BatchResult(saved=3, updated=1, failed=0)
        assert "3" in str(result)
        assert "1" in str(result)

    def test_empty_batch_returns_zero_counts(self) -> None:
        result = save_postings_to_db([])
        assert result.saved == 0
        assert result.total_processed == 0


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


class TestGetHelpers:
    def test_get_jobs_by_status_returns_new_jobs(self) -> None:
        for i in range(3):
            save_job_to_db(_make_posting(i + 1))
        jobs = get_jobs_by_status("new")
        assert len(jobs) == 3

    def test_get_jobs_by_status_filters_correctly(self) -> None:
        save_job_to_db(_make_posting(1))
        from database.database import get_db
        with get_db() as db:
            db.query(Job).update({"status": "analyzed"})
        jobs = get_jobs_by_status("new", limit=10)
        assert len(jobs) == 0
        jobs = get_jobs_by_status("analyzed", limit=10)
        assert len(jobs) == 1

    def test_get_jobs_by_status_respects_limit(self) -> None:
        for i in range(5):
            save_job_to_db(_make_posting(i + 1))
        jobs = get_jobs_by_status("new", limit=2)
        assert len(jobs) == 2

    def test_get_job_count_returns_dict(self) -> None:
        for i in range(3):
            save_job_to_db(_make_posting(i + 1))
        counts = get_job_count()
        assert isinstance(counts, dict)
        assert counts.get("new", 0) == 3

    def test_get_job_count_empty_database(self) -> None:
        counts = get_job_count()
        assert counts == {}


# ---------------------------------------------------------------------------
# Utils extensions: clean_html_text and extract_relative_date
# ---------------------------------------------------------------------------


class TestCleanHtmlText:
    def test_strips_html_tags(self) -> None:
        assert clean_html_text("<b>Hello</b> <i>World</i>") == "Hello World"

    def test_decodes_html_entities(self) -> None:
        assert clean_html_text("AT&amp;T pays &gt;$100k") == "AT&T pays >$100k"

    def test_collapses_whitespace(self) -> None:
        assert clean_html_text("  lots   of   spaces  ") == "lots of spaces"

    def test_handles_none(self) -> None:
        assert clean_html_text(None) == ""  # type: ignore[arg-type]

    def test_handles_nbsp(self) -> None:
        result = clean_html_text("San&nbsp;Francisco")
        assert "San" in result
        assert "Francisco" in result


class TestExtractRelativeDate:
    def _today(self) -> date:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).date()

    def test_just_posted_returns_today(self) -> None:
        assert extract_relative_date("Just posted") == self._today()

    def test_today_returns_today(self) -> None:
        assert extract_relative_date("Today") == self._today()

    def test_hours_ago_returns_today(self) -> None:
        assert extract_relative_date("3 hours ago") == self._today()

    def test_n_days_ago(self) -> None:
        result = extract_relative_date("Posted 5 days ago")
        assert result == self._today() - timedelta(days=5)

    def test_yesterday(self) -> None:
        assert extract_relative_date("Yesterday") == self._today() - timedelta(days=1)

    def test_n_weeks_ago(self) -> None:
        assert extract_relative_date("2 weeks ago") == self._today() - timedelta(days=14)

    def test_a_week_ago(self) -> None:
        assert extract_relative_date("a week ago") == self._today() - timedelta(days=7)

    def test_n_months_ago(self) -> None:
        assert extract_relative_date("1 month ago") == self._today() - timedelta(days=30)

    def test_30_plus_days(self) -> None:
        assert extract_relative_date("30+ days ago") == self._today() - timedelta(days=30)

    def test_iso_date_returned_directly(self) -> None:
        assert extract_relative_date("2024-06-15") == date(2024, 6, 15)

    def test_iso_date_embedded_in_string(self) -> None:
        assert extract_relative_date("Posted on 2024-03-01") == date(2024, 3, 1)

    def test_unrecognised_string_returns_none(self) -> None:
        assert extract_relative_date("recently") is None

    def test_none_input_returns_none(self) -> None:
        assert extract_relative_date(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert extract_relative_date("") is None
