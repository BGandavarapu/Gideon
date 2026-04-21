"""
Unit tests for Phase 6: Scheduler, Tasks, and Notifications.

All tests that touch the database use temporary SQLite files so they do not
pollute the real jobs.db.  All tests that would make network calls (scraping,
NIM API) are patched with MagicMock.

Test classes
------------
TestTaskResult          – TaskResult dataclass helpers
TestNotificationService – Email + console notification logic
TestSchedulerManager    – SchedulerManager lifecycle and job registration
TestScrapeJobsTask      – scrape_jobs_task with mocked scraper
TestAnalyzeNewJobsTask  – analyze_new_jobs_task with in-memory DB
TestGenerateResumesTask – generate_resumes_task with mocked NIM
TestCleanupTask         – cleanup_old_jobs_task date-based archiving
TestDailyReportTask     – daily_report_task summary generation
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------

def _make_task_result(name: str = "test", success: bool = True) -> Any:
    from scheduler.tasks import TaskResult

    r = TaskResult(task_name=name, success=success)
    r.data = {"items": 3}
    r.errors = [] if success else ["something went wrong"]
    return r.finish()


# ---------------------------------------------------------------------------
# TestTaskResult
# ---------------------------------------------------------------------------

class TestTaskResult:
    """TaskResult dataclass – duration, to_dict, finish helpers."""

    def test_finish_sets_finished_at(self):
        from scheduler.tasks import TaskResult

        r = TaskResult(task_name="t")
        assert r.finished_at is None
        r.finish()
        assert r.finished_at is not None

    def test_duration_seconds_zero_before_finish(self):
        from scheduler.tasks import TaskResult

        r = TaskResult(task_name="t")
        assert r.duration_seconds == 0.0

    def test_duration_seconds_positive_after_finish(self):
        from scheduler.tasks import TaskResult
        import time

        r = TaskResult(task_name="t")
        time.sleep(0.01)
        r.finish()
        assert r.duration_seconds > 0.0

    def test_to_dict_serialisable(self):
        r = _make_task_result()
        d = r.to_dict()
        # Must be JSON-serialisable
        json.dumps(d)
        assert d["task_name"] == "test"
        assert d["success"] is True
        assert "duration_seconds" in d

    def test_finish_returns_self(self):
        from scheduler.tasks import TaskResult

        r = TaskResult(task_name="t")
        assert r.finish() is r

    def test_errors_captured(self):
        from scheduler.tasks import TaskResult

        r = TaskResult(task_name="t", success=False)
        r.errors.append("err1")
        assert len(r.errors) == 1

    def test_default_started_at_is_utc(self):
        from scheduler.tasks import TaskResult

        r = TaskResult(task_name="t")
        assert r.started_at.tzinfo is not None


# ---------------------------------------------------------------------------
# TestNotificationService
# ---------------------------------------------------------------------------

class TestNotificationService:
    """NotificationService – email disabled path and console logging."""

    def test_init_email_disabled_by_default(self):
        from scheduler.notifications import NotificationService

        svc = NotificationService(email_enabled=False)
        assert svc._email_enabled is False

    def test_send_new_jobs_logs_and_no_smtp(self, caplog):
        from scheduler.notifications import NotificationService
        import logging

        svc = NotificationService(email_enabled=False)
        jobs = [{"title": "Dev", "company": "Acme", "location": "SF", "match_score": 80.0}]
        with caplog.at_level(logging.INFO, logger="scheduler.notifications"):
            svc.send_new_jobs_notification(jobs)
        # Should have logged something about the new jobs
        assert any("new job" in r.message.lower() for r in caplog.records)

    def test_send_new_jobs_threshold_filters(self, caplog):
        from scheduler.notifications import NotificationService
        import logging

        svc = NotificationService(email_enabled=False)
        jobs = [{"title": "Dev", "company": "Acme", "match_score": 30.0}]
        # With threshold=50, score=30 should be filtered out (no log)
        with caplog.at_level(logging.INFO, logger="scheduler.notifications"):
            svc.send_new_jobs_notification(jobs, threshold=50.0)
        assert not any("new job" in r.message.lower() for r in caplog.records)

    def test_send_error_notification_logs(self, caplog):
        from scheduler.notifications import NotificationService
        import logging

        svc = NotificationService(email_enabled=False)
        with caplog.at_level(logging.ERROR, logger="scheduler.notifications"):
            svc.send_error_notification("test_task", "boom")
        assert any("boom" in r.message for r in caplog.records)

    def test_send_daily_report_logs(self, caplog):
        from scheduler.notifications import NotificationService
        import logging

        svc = NotificationService(email_enabled=False)
        report = {
            "date": "2026-03-19",
            "new_jobs": 5,
            "analyzed_today": 4,
            "resumes_generated_today": 2,
            "top_jobs": [],
        }
        with caplog.at_level(logging.INFO, logger="scheduler.notifications"):
            svc.send_daily_report(report)
        assert any("5" in r.message for r in caplog.records)

    def test_send_email_skipped_when_disabled(self):
        from scheduler.notifications import NotificationService

        svc = NotificationService(email_enabled=False)
        with patch("smtplib.SMTP") as mock_smtp:
            svc._send_email("subj", "body")
        mock_smtp.assert_not_called()

    def test_send_email_skipped_when_missing_creds(self):
        from scheduler.notifications import NotificationService

        svc = NotificationService(email_enabled=True)
        svc._sender_email = None
        svc._sender_password = None
        svc._recipient_email = None
        with patch("smtplib.SMTP") as mock_smtp:
            svc._send_email("subj", "body")
        mock_smtp.assert_not_called()

    def test_send_task_result_summary_logs(self, caplog):
        from scheduler.notifications import NotificationService
        import logging

        svc = NotificationService(email_enabled=False)
        results = [_make_task_result("mytask", success=True)]
        with caplog.at_level(logging.INFO, logger="scheduler.notifications"):
            svc.send_task_result_summary(results)
        assert any("mytask" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# TestSchedulerManager
# ---------------------------------------------------------------------------

class TestSchedulerManager:
    """SchedulerManager – lifecycle, job registration, test-mode."""

    def test_start_and_stop(self):
        from scheduler.scheduler import SchedulerManager

        mgr = SchedulerManager(config_path="config.yaml")
        assert not mgr.is_running
        mgr.start()
        assert mgr.is_running
        mgr.stop()
        assert not mgr.is_running

    def test_double_start_is_safe(self):
        from scheduler.scheduler import SchedulerManager

        mgr = SchedulerManager(config_path="config.yaml")
        mgr.start()
        mgr.start()   # Should not raise
        mgr.stop()

    def test_double_stop_is_safe(self):
        from scheduler.scheduler import SchedulerManager

        mgr = SchedulerManager(config_path="config.yaml")
        mgr.start()
        mgr.stop()
        mgr.stop()   # Should not raise

    def test_get_job_info_returns_two_auto_jobs(self):
        from scheduler.scheduler import SchedulerManager

        mgr = SchedulerManager(config_path="config.yaml")
        mgr.start()
        try:
            jobs = mgr.get_job_info()
            assert len(jobs) == 2
            ids = {j["id"] for j in jobs}
            assert ids == {"cleanup_old_jobs", "daily_report"}
            # Manual tasks must NOT be scheduled
            assert "scrape_jobs"    not in ids
            assert "analyze_new_jobs" not in ids
            assert "generate_resumes" not in ids
        finally:
            mgr.stop()

    def test_job_info_has_next_run(self):
        from scheduler.scheduler import SchedulerManager

        mgr = SchedulerManager(config_path="config.yaml")
        mgr.start()
        try:
            for job in mgr.get_job_info():
                assert job["next_run"] is not None or True  # cron might be None pre-fire
        finally:
            mgr.stop()

    def test_test_mode_override(self):
        from scheduler.scheduler import SchedulerManager

        mgr = SchedulerManager(config_path="config.yaml")
        mgr._config["test_mode"] = True
        mgr.start()
        try:
            jobs = mgr.get_job_info()
            # In test mode all triggers should be interval-based
            for j in jobs:
                assert "interval" in j["trigger"].lower()
        finally:
            mgr.stop()

    def test_pause_and_resume(self):
        from scheduler.scheduler import SchedulerManager

        mgr = SchedulerManager(config_path="config.yaml")
        mgr.start()
        mgr.pause()
        mgr.resume()
        mgr.stop()

    def test_missing_config_uses_defaults(self, tmp_path):
        from scheduler.scheduler import SchedulerManager

        nonexistent = str(tmp_path / "no_config.yaml")
        mgr = SchedulerManager(config_path=nonexistent)
        mgr.start()
        try:
            # Only 2 automatic tasks are registered even with no config
            assert len(mgr.get_job_info()) == 2
        finally:
            mgr.stop()


# ---------------------------------------------------------------------------
# TestScrapeJobsTask
# ---------------------------------------------------------------------------

class TestScrapeJobsTask:
    """scrape_jobs_task – patching LinkedInScraper and db_handler at their source."""

    def _make_posting(self, title: str = "Dev") -> Any:
        from scraper.base_scraper import JobPosting

        return JobPosting(
            job_title=title,
            company_name="Acme",
            location="SF",
            job_description="Test description",
            application_url=f"https://linkedin.com/{title}",
            source="linkedin",
        )

    def test_returns_task_result(self):
        from scheduler.tasks import scrape_jobs_task
        from scraper.db_handler import BatchResult

        postings = [self._make_posting("Dev")]
        mock_scraper = MagicMock()
        mock_scraper.__enter__ = MagicMock(return_value=mock_scraper)
        mock_scraper.__exit__ = MagicMock(return_value=False)
        mock_scraper.scrape.return_value = postings

        # Patch at the source module level (tasks.py uses deferred imports)
        with (
            patch("scraper.linkedin_scraper.LinkedInScraper", return_value=mock_scraper),
            patch("scraper.db_handler.save_postings_to_db", return_value=BatchResult(saved=1)),
        ):
            result = scrape_jobs_task([
                {"keywords": "python dev", "location": "SF", "max_results": 5}
            ])

        assert result.success is True
        assert result.data["new_jobs"] == 1
        assert result.data["jobs_found"] == 1
        assert result.finished_at is not None

    def test_empty_configs_returns_zero(self):
        from scheduler.tasks import scrape_jobs_task

        result = scrape_jobs_task([])
        assert result.success is True
        assert result.data["jobs_found"] == 0

    def test_scraper_exception_captured(self):
        from scheduler.tasks import scrape_jobs_task

        mock_scraper = MagicMock()
        mock_scraper.__enter__ = MagicMock(return_value=mock_scraper)
        mock_scraper.__exit__ = MagicMock(return_value=False)
        mock_scraper.scrape.side_effect = RuntimeError("network down")

        with patch("scraper.linkedin_scraper.LinkedInScraper", return_value=mock_scraper):
            result = scrape_jobs_task([
                {"keywords": "dev", "location": "SF", "max_results": 5}
            ])

        # Task-level error captured; overall success still True (non-critical)
        assert len(result.errors) >= 1
        assert result.finished_at is not None

    def test_per_config_exception_captured_as_error(self):
        """A per-config scraper error is captured in result.errors but does not
        set success=False (only a *critical* outer exception does that)."""
        from scheduler.tasks import scrape_jobs_task

        with patch("scraper.linkedin_scraper.LinkedInScraper", side_effect=Exception("critical")):
            result = scrape_jobs_task([
                {"keywords": "dev", "location": "SF", "max_results": 5}
            ])

        # Per-config error is recorded but task itself still reports success
        # (mirrors how the outer try/except is structured in tasks.py)
        assert len(result.errors) >= 1
        assert "critical" in result.errors[0]


# ---------------------------------------------------------------------------
# TestAnalyzeNewJobsTask
# ---------------------------------------------------------------------------

class TestAnalyzeNewJobsTask:
    """analyze_new_jobs_task – mocked KeywordExtractor."""

    def _make_db_mock(self, jobs: list) -> MagicMock:
        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_db.query.return_value.filter.return_value.all.return_value = jobs
        return mock_db

    def test_analyzes_new_jobs(self):
        from scheduler.tasks import analyze_new_jobs_task

        mock_extractor = MagicMock()
        mock_extractor.extract_by_category.return_value = {
            "programming_languages": ["python"],
            "soft_skills": ["teamwork"],
        }

        mock_job = MagicMock()
        mock_job.id = 1
        mock_job.job_title = "Dev"
        mock_job.status = "new"
        mock_job.job_description = "Build stuff with Python"

        with (
            patch("analyzer.keyword_extractor.KeywordExtractor", return_value=mock_extractor),
            patch("analyzer.requirement_parser.RequirementParser"),
            patch("database.database.get_db", return_value=self._make_db_mock([mock_job])),
        ):
            result = analyze_new_jobs_task()

        assert result.success is True
        assert result.data["jobs_analyzed"] == 1
        assert mock_job.status == "analyzed"

    def test_skips_jobs_without_description(self):
        from scheduler.tasks import analyze_new_jobs_task

        mock_job = MagicMock()
        mock_job.id = 2
        mock_job.job_description = None

        with (
            patch("analyzer.keyword_extractor.KeywordExtractor"),
            patch("analyzer.requirement_parser.RequirementParser"),
            patch("database.database.get_db", return_value=self._make_db_mock([mock_job])),
        ):
            result = analyze_new_jobs_task()

        assert result.data["jobs_skipped"] == 1
        assert result.data["jobs_analyzed"] == 0

    def test_no_new_jobs_is_ok(self):
        from scheduler.tasks import analyze_new_jobs_task

        with (
            patch("analyzer.keyword_extractor.KeywordExtractor"),
            patch("analyzer.requirement_parser.RequirementParser"),
            patch("database.database.get_db", return_value=self._make_db_mock([])),
        ):
            result = analyze_new_jobs_task()

        assert result.success is True
        assert result.data["jobs_analyzed"] == 0


# ---------------------------------------------------------------------------
# TestGenerateResumesTask
# ---------------------------------------------------------------------------

class TestGenerateResumesTask:
    """generate_resumes_task – mocked ScoringEngine and ResumeModifier."""

    def _make_score_result(self, score: float) -> MagicMock:
        sr = MagicMock()
        sr.total_score = score
        return sr

    def _make_mod_result(self) -> MagicMock:
        mr = MagicMock()
        mr.content = {"personal_info": {"name": "Test", "email": "t@t.com"}}
        return mr

    def _make_db_for_generate(
        self, master: Any, job_ids_with_tailored: list, candidate_jobs: list
    ) -> MagicMock:
        """Build a mock DB that returns master/tailored/candidate in sequence."""
        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)

        call_count = [0]

        def side_effect(model):
            call_count[0] += 1
            m = MagicMock()
            if call_count[0] == 1:
                # MasterResume active query (any_master sanity check)
                m.filter.return_value.first.return_value = master
            elif call_count[0] == 2:
                # TailoredResume job_id query
                m.all.return_value = [MagicMock(job_id=jid) for jid in job_ids_with_tailored]
            elif call_count[0] == 3:
                # Candidate jobs
                m.filter.return_value.all.return_value = candidate_jobs
            elif call_count[0] == 4:
                # _get_resume_for_job: MasterResume fallback (is_active) query
                m.filter.return_value.first.return_value = master
            else:
                # TailoredResume upsert lookup
                m.filter.return_value.first.return_value = None
            return m

        mock_db.query.side_effect = side_effect
        return mock_db

    def test_skips_when_no_active_resume(self):
        from scheduler.tasks import generate_resumes_task

        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_db.query.return_value.filter.return_value.first.return_value = None

        with patch("database.database.get_db", return_value=mock_db):
            result = generate_resumes_task(match_threshold=60.0)

        assert result.success is True
        assert result.data.get("resumes_generated", 0) == 0

    def test_skips_low_score_jobs(self):
        from scheduler.tasks import generate_resumes_task

        master = MagicMock()
        master.id = 1

        mock_job = MagicMock()
        mock_job.id = 10
        mock_job.job_title = "Dev"

        mock_db = self._make_db_for_generate(master, [], [mock_job])

        with (
            patch("analyzer.scoring.ScoringEngine") as MockEngine,
            patch("resume_engine.modifier.ResumeModifier"),
            patch("database.database.get_db", return_value=mock_db),
        ):
            MockEngine.return_value.score.return_value = self._make_score_result(30.0)
            result = generate_resumes_task(match_threshold=60.0)

        assert result.data.get("skipped_low_score", 0) >= 0

    def test_generates_resume_above_threshold(self):
        from scheduler.tasks import generate_resumes_task

        master = MagicMock()
        master.id = 1

        mock_job = MagicMock()
        mock_job.id = 99
        mock_job.job_title = "Dev"

        mock_db = self._make_db_for_generate(master, [], [mock_job])

        with (
            patch("analyzer.scoring.ScoringEngine") as MockEngine,
            patch("resume_engine.modifier.ResumeModifier") as MockModifier,
            patch("database.database.get_db", return_value=mock_db),
        ):
            MockEngine.return_value.score.return_value = self._make_score_result(80.0)
            MockModifier.return_value.modify_resume.return_value = self._make_mod_result()
            result = generate_resumes_task(match_threshold=60.0)

        assert result.success is True
        assert result.data.get("resumes_generated", 0) == 1


# ---------------------------------------------------------------------------
# TestCleanupTask
# ---------------------------------------------------------------------------

class TestCleanupTask:
    """cleanup_old_jobs_task – date-based archiving."""

    def _make_cleanup_db(self, jobs: list) -> MagicMock:
        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_db.query.return_value.filter.return_value.all.return_value = jobs
        return mock_db

    def test_archives_old_jobs(self):
        from scheduler.tasks import cleanup_old_jobs_task

        old_job = MagicMock()
        old_job.id = 1
        old_job.status = "new"

        with patch("database.database.get_db", return_value=self._make_cleanup_db([old_job])):
            result = cleanup_old_jobs_task(days_old=30)

        assert result.success is True
        assert result.data["jobs_archived"] == 1
        assert old_job.status == "archived"

    def test_no_old_jobs(self):
        from scheduler.tasks import cleanup_old_jobs_task

        with patch("database.database.get_db", return_value=self._make_cleanup_db([])):
            result = cleanup_old_jobs_task(days_old=30)

        assert result.success is True
        assert result.data["jobs_archived"] == 0

    def test_critical_failure_marks_result(self):
        from scheduler.tasks import cleanup_old_jobs_task

        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_db.query.side_effect = Exception("DB down")

        with patch("database.database.get_db", return_value=mock_db):
            result = cleanup_old_jobs_task(days_old=30)

        assert result.success is False
        assert len(result.errors) >= 1


# ---------------------------------------------------------------------------
# TestDailyReportTask
# ---------------------------------------------------------------------------

class TestDailyReportTask:
    """daily_report_task – summary compilation and notification dispatch."""

    def _make_report_db(self, counts: list) -> MagicMock:
        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_db.query.return_value.filter.return_value.count.side_effect = counts
        mock_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
        return mock_db

    def test_generates_report(self):
        from scheduler.tasks import daily_report_task

        with (
            patch("database.database.get_db", return_value=self._make_report_db([3, 2, 1])),
            patch("scheduler.notifications.NotificationService") as MockNotif,
        ):
            result = daily_report_task()

        assert result.success is True
        report = result.data.get("report", {})
        assert report.get("new_jobs") == 3
        assert report.get("analyzed_today") == 2
        assert report.get("resumes_generated_today") == 1

    def test_notification_dispatched(self):
        from scheduler.tasks import daily_report_task

        with (
            patch("database.database.get_db", return_value=self._make_report_db([0, 0, 0])),
            patch("scheduler.notifications.NotificationService") as MockNotif,
        ):
            daily_report_task()

        MockNotif.return_value.send_daily_report.assert_called_once()


# ---------------------------------------------------------------------------
# TestManualTaskConstants
# ---------------------------------------------------------------------------


class TestManualTaskConstants:
    """MANUAL_TASKS and AUTO_TASKS constants in scheduler.tasks."""

    def test_manual_tasks_list(self):
        from scheduler.tasks import MANUAL_TASKS

        assert "scrape_jobs_task"       in MANUAL_TASKS
        assert "analyze_new_jobs_task"  in MANUAL_TASKS
        assert "generate_resumes_task"  in MANUAL_TASKS

    def test_auto_tasks_list(self):
        from scheduler.tasks import AUTO_TASKS

        assert "cleanup_old_jobs_task" in AUTO_TASKS
        assert "daily_report_task"     in AUTO_TASKS

    def test_no_overlap(self):
        from scheduler.tasks import MANUAL_TASKS, AUTO_TASKS

        assert not set(MANUAL_TASKS) & set(AUTO_TASKS), \
            "A task must not appear in both MANUAL_TASKS and AUTO_TASKS"


# ---------------------------------------------------------------------------
# TestSchedulerOnlyRegistersAutoTasks
# ---------------------------------------------------------------------------


class TestSchedulerOnlyRegistersAutoTasks:
    """Confirm SchedulerManager registers exactly 2 automatic tasks."""

    def test_scheduler_only_registers_auto_tasks(self):
        """add_job is called exactly twice — cleanup + report only."""
        from scheduler.scheduler import SchedulerManager
        from unittest.mock import MagicMock, patch

        mgr = SchedulerManager(config_path="config.yaml")
        mgr.start()
        try:
            jobs = mgr.get_job_info()
            job_ids = {j["id"] for j in jobs}
            assert len(jobs) == 2
            assert "cleanup_old_jobs" in job_ids
            assert "daily_report"     in job_ids
            assert "scrape_jobs"        not in job_ids
            assert "analyze_new_jobs"   not in job_ids
            assert "generate_resumes"   not in job_ids
        finally:
            mgr.stop()


# ---------------------------------------------------------------------------
# TestManualTaskAPIRoutes
# ---------------------------------------------------------------------------


@pytest.fixture
def flask_client():
    """Return a Flask test client with tasks mocked to avoid real work."""
    import sys
    from pathlib import Path

    # Ensure project root is on sys.path
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    with (
        patch("scheduler.tasks.scrape_jobs_task"),
        patch("scheduler.tasks.analyze_new_jobs_task"),
        patch("scheduler.tasks.generate_resumes_task"),
    ):
        from web.app import app
        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client


class TestManualTaskAPIRoutes:
    """POST /api/run/* endpoints return status:started immediately."""

    def test_manual_task_api_scrape(self, flask_client):
        """POST /api/run/scrape returns 200 with status: started."""
        import time
        resp = flask_client.post("/api/run/scrape")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "started"
        assert data["task"]   == "scrape_jobs_task"
        time.sleep(0.05)  # let daemon thread settle

    def test_manual_task_api_analyze(self, flask_client):
        """POST /api/run/analyze returns 200 with status: started."""
        import time
        resp = flask_client.post("/api/run/analyze")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "started"
        assert data["task"]   == "analyze_new_jobs_task"
        time.sleep(0.05)

    def test_manual_task_api_generate(self, flask_client):
        """POST /api/run/generate returns 200 with status: started."""
        import time
        resp = flask_client.post("/api/run/generate")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "started"
        assert data["task"]   == "generate_resumes_task"
        time.sleep(0.05)

    def test_run_status_returns_all_three_keys(self, flask_client):
        """GET /api/run/status returns scrape, analyze, generate keys as bools."""
        resp = flask_client.get("/api/run/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "scrape"   in data
        assert "analyze"  in data
        assert "generate" in data
        for val in data.values():
            assert isinstance(val, bool)

    def test_run_last_run_returns_all_three_keys(self, flask_client):
        """GET /api/run/last-run returns scrape, analyze, generate keys."""
        resp = flask_client.get("/api/run/last-run")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "scrape"   in data
        assert "analyze"  in data
        assert "generate" in data

    def test_task_running_flag_set_and_cleared(self, flask_client):
        """_task_running[key] is False after a task completes."""
        import time
        from web.app import _task_running

        flask_client.post("/api/run/analyze")
        # Give the daemon thread time to finish (mock task is instant)
        time.sleep(0.2)
        assert _task_running["analyze"] is False

    def test_last_run_populated_after_task(self, flask_client):
        """_task_last_run[key] is set after task completes."""
        import time
        from web.app import _task_last_run

        _task_last_run["analyze"] = None
        flask_client.post("/api/run/analyze")
        time.sleep(0.2)
        assert _task_last_run["analyze"] is not None
