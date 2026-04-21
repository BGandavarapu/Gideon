"""Tests for SettingsManager and Settings API endpoints.

Covers:
- SettingsManager unit tests (load, save, set_mode, set_schedule, validation)
- Flask API tests via test client (GET /api/settings, PATCH /api/settings/automation/<task>)
- Scheduler integration tests (conditional registration based on settings)
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web.settings_manager import SettingsManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_sm(tmp_dir: str) -> SettingsManager:
    """Return a SettingsManager that writes to a temp directory."""
    sm = SettingsManager()
    sm.SETTINGS_PATH = str(Path(tmp_dir) / "settings.json")
    return sm


# ---------------------------------------------------------------------------
# SettingsManager unit tests
# ---------------------------------------------------------------------------

class TestSettingsManagerDefaults(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sm = make_sm(self.tmp)

    def test_defaults_on_first_load(self):
        """Missing file → load() returns defaults and creates the file."""
        data = self.sm.load()
        self.assertEqual(data["automation"]["scrape"]["mode"], "manual")
        self.assertEqual(data["automation"]["generate"]["mode"], "manual")
        self.assertTrue(Path(self.sm.SETTINGS_PATH).exists(),
                        "settings.json should be created on first load")

    def test_corrupt_file_returns_defaults(self):
        """Corrupt JSON → load() silently resets to defaults."""
        Path(self.sm.SETTINGS_PATH).write_text("NOT JSON", encoding="utf-8")
        data = self.sm.load()
        self.assertEqual(data["automation"]["scrape"]["mode"], "manual")

    def test_defaults_contain_schedules(self):
        data = self.sm.load()
        self.assertEqual(data["automation"]["scrape"]["schedule"], "09:00")
        self.assertEqual(data["automation"]["generate"]["schedule"], "10:00")


class TestSettingsManagerPersistence(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_settings_persist_across_instances(self):
        """Writes from one instance are visible from a new instance."""
        sm1 = make_sm(self.tmp)
        sm1.set_mode("scrape", "automatic")

        sm2 = make_sm(self.tmp)
        self.assertEqual(sm2.get_mode("scrape"), "automatic")

    def test_save_updates_last_updated(self):
        sm = make_sm(self.tmp)
        sm.set_mode("generate", "automatic")
        data = sm.load()
        self.assertIsNotNone(data["last_updated"])

    def test_set_and_get_mode_roundtrip(self):
        sm = make_sm(self.tmp)
        sm.set_mode("scrape", "automatic")
        self.assertEqual(sm.get_mode("scrape"), "automatic")
        sm.set_mode("scrape", "manual")
        self.assertEqual(sm.get_mode("scrape"), "manual")

    def test_set_and_get_schedule_roundtrip(self):
        sm = make_sm(self.tmp)
        sm.set_schedule("scrape", "08:30")
        self.assertEqual(sm.get_schedule("scrape"), "08:30")


class TestSettingsManagerValidation(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sm = make_sm(self.tmp)

    def test_set_mode_invalid_task_raises(self):
        with self.assertRaises(ValueError):
            self.sm.set_mode("invalid_task", "manual")

    def test_set_mode_analyze_raises(self):
        """analyze is not a toggleable task."""
        with self.assertRaises(ValueError):
            self.sm.set_mode("analyze", "manual")

    def test_set_mode_invalid_mode_raises(self):
        with self.assertRaises(ValueError):
            self.sm.set_mode("scrape", "semi-auto")

    def test_set_mode_invalid_mode_maybe_raises(self):
        with self.assertRaises(ValueError):
            self.sm.set_mode("scrape", "maybe")

    def test_set_schedule_invalid_format_raises_25_99(self):
        with self.assertRaises(ValueError):
            self.sm.set_schedule("scrape", "25:99")

    def test_set_schedule_invalid_format_raises_9am(self):
        with self.assertRaises(ValueError):
            self.sm.set_schedule("scrape", "9am")

    def test_set_schedule_invalid_format_raises_empty(self):
        with self.assertRaises(ValueError):
            self.sm.set_schedule("scrape", "")

    def test_set_schedule_invalid_task_raises(self):
        with self.assertRaises(ValueError):
            self.sm.set_schedule("analyze", "09:00")

    def test_set_schedule_valid_boundary_values(self):
        self.sm.set_schedule("scrape", "00:00")
        self.assertEqual(self.sm.get_schedule("scrape"), "00:00")
        self.sm.set_schedule("scrape", "23:59")
        self.assertEqual(self.sm.get_schedule("scrape"), "23:59")

    def test_merge_defaults_preserves_extra_keys(self):
        """If settings.json has extra keys, they are preserved."""
        sm = make_sm(self.tmp)
        settings = sm.load()
        settings["custom_key"] = "custom_value"
        sm.save(settings)
        loaded = sm.load()
        self.assertEqual(loaded.get("custom_key"), "custom_value")


# ---------------------------------------------------------------------------
# Flask API tests
# ---------------------------------------------------------------------------

class TestSettingsAPI(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Patch the global settings_manager in app to use our temp path
        import web.app as app_module
        self._orig_sm = app_module.settings_manager
        app_module.settings_manager = make_sm(self.tmp)

        from web.app import app
        app.config["TESTING"] = True
        self.client = app.test_client()

    def tearDown(self):
        import web.app as app_module
        app_module.settings_manager = self._orig_sm

    def test_api_get_settings_200(self):
        r = self.client.get("/api/settings")
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn("automation", data)
        self.assertIn("scrape", data["automation"])
        self.assertIn("generate", data["automation"])

    def test_api_get_settings_has_modes(self):
        r = self.client.get("/api/settings")
        data = json.loads(r.data)
        self.assertIn("mode", data["automation"]["scrape"])
        self.assertIn("mode", data["automation"]["generate"])

    def test_api_patch_mode_valid_automatic(self):
        r = self.client.patch(
            "/api/settings/automation/scrape",
            data=json.dumps({"mode": "automatic"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        body = json.loads(r.data)
        self.assertEqual(body["status"], "saved")
        self.assertEqual(body["task"], "scrape")
        self.assertEqual(body["settings"]["mode"], "automatic")

    def test_api_patch_mode_valid_manual(self):
        r = self.client.patch(
            "/api/settings/automation/generate",
            data=json.dumps({"mode": "manual"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        body = json.loads(r.data)
        self.assertEqual(body["settings"]["mode"], "manual")

    def test_api_patch_schedule_valid(self):
        r = self.client.patch(
            "/api/settings/automation/scrape",
            data=json.dumps({"schedule": "08:30"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        body = json.loads(r.data)
        self.assertEqual(body["settings"]["schedule"], "08:30")

    def test_api_patch_mode_invalid_task_analyze_400(self):
        """analyze is not a toggleable task — must return 400."""
        r = self.client.patch(
            "/api/settings/automation/analyze",
            data=json.dumps({"mode": "manual"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)

    def test_api_patch_mode_invalid_task_cleanup_400(self):
        r = self.client.patch(
            "/api/settings/automation/cleanup",
            data=json.dumps({"mode": "manual"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)

    def test_api_patch_mode_invalid_value_400(self):
        r = self.client.patch(
            "/api/settings/automation/scrape",
            data=json.dumps({"mode": "maybe"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)

    def test_api_patch_schedule_invalid_format_400(self):
        r = self.client.patch(
            "/api/settings/automation/scrape",
            data=json.dumps({"schedule": "9am"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)

    def test_api_patch_schedule_invalid_25_99_400(self):
        r = self.client.patch(
            "/api/settings/automation/scrape",
            data=json.dumps({"schedule": "25:99"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)

    def test_api_patch_no_body_400(self):
        r = self.client.patch(
            "/api/settings/automation/scrape",
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)

    def test_settings_page_200(self):
        r = self.client.get("/settings")
        self.assertEqual(r.status_code, 200)
        html = r.data.decode("utf-8", errors="replace")
        self.assertIn("Settings", html)
        self.assertIn("Automation Mode", html)
        self.assertIn("Job Scraping", html)
        self.assertIn("Resume Generation", html)
        self.assertIn("Job Analysis", html)
        self.assertIn("Always Auto", html)

    def test_settings_page_has_toggle_buttons(self):
        r = self.client.get("/settings")
        html = r.data.decode("utf-8", errors="replace")
        self.assertIn("setMode", html)
        self.assertIn("setSchedule", html)

    def test_api_patch_mode_persisted_to_file(self):
        """After PATCH, GET /api/settings returns the updated value."""
        self.client.patch(
            "/api/settings/automation/scrape",
            data=json.dumps({"mode": "automatic"}),
            content_type="application/json",
        )
        r = self.client.get("/api/settings")
        data = json.loads(r.data)
        self.assertEqual(data["automation"]["scrape"]["mode"], "automatic")


# ---------------------------------------------------------------------------
# Scheduler integration tests
# ---------------------------------------------------------------------------

class TestSchedulerRegistration(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _make_scheduler(self, scrape_mode: str, generate_mode: str):
        """Create a SchedulerManager with mocked settings."""
        from scheduler.scheduler import SchedulerManager

        sm_mock = MagicMock()
        sm_mock.get_mode.side_effect = lambda task: (
            scrape_mode if task == "scrape" else generate_mode
        )
        sm_mock.get_schedule.side_effect = lambda task: (
            "09:00" if task == "scrape" else "10:00"
        )

        mgr = SchedulerManager.__new__(SchedulerManager)

        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR
        mgr._scheduler = BackgroundScheduler()
        mgr._config = {
            "test_mode": False,
            "cleanup_days": 30,
            "search_configs": [],
            "auto_generate_threshold": 35.0,
        }
        mgr._is_running = False
        mgr._scheduler.add_listener(lambda e: None, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)

        return mgr, sm_mock

    def test_scheduler_registers_scrape_when_automatic(self):
        mgr, sm_mock = self._make_scheduler("automatic", "manual")
        with patch("web.settings_manager.SettingsManager", return_value=sm_mock):
            mgr._register_jobs()
        job_ids = [j.id for j in mgr._scheduler.get_jobs()]
        self.assertIn("scrape_jobs", job_ids)

    def test_scheduler_skips_scrape_when_manual(self):
        mgr, sm_mock = self._make_scheduler("manual", "manual")
        with patch("web.settings_manager.SettingsManager", return_value=sm_mock):
            mgr._register_jobs()
        job_ids = [j.id for j in mgr._scheduler.get_jobs()]
        self.assertNotIn("scrape_jobs", job_ids)

    def test_scheduler_registers_generate_when_automatic(self):
        mgr, sm_mock = self._make_scheduler("manual", "automatic")
        with patch("web.settings_manager.SettingsManager", return_value=sm_mock):
            mgr._register_jobs()
        job_ids = [j.id for j in mgr._scheduler.get_jobs()]
        self.assertIn("generate_resumes", job_ids)

    def test_scheduler_skips_generate_when_manual(self):
        mgr, sm_mock = self._make_scheduler("manual", "manual")
        with patch("web.settings_manager.SettingsManager", return_value=sm_mock):
            mgr._register_jobs()
        job_ids = [j.id for j in mgr._scheduler.get_jobs()]
        self.assertNotIn("generate_resumes", job_ids)

    def test_scheduler_always_registers_cleanup_and_report(self):
        mgr, sm_mock = self._make_scheduler("manual", "manual")
        with patch("web.settings_manager.SettingsManager", return_value=sm_mock):
            mgr._register_jobs()
        job_ids = [j.id for j in mgr._scheduler.get_jobs()]
        self.assertIn("cleanup_old_jobs", job_ids)
        self.assertIn("daily_report", job_ids)

    def test_reschedule_task_adds_job_when_automatic(self):
        """reschedule_task('scrape') adds job when mode is automatic."""
        from scheduler.scheduler import SchedulerManager

        sm_mock = MagicMock()
        sm_mock.get_mode.return_value = "automatic"
        sm_mock.get_schedule.return_value = "08:30"

        mgr = SchedulerManager.__new__(SchedulerManager)
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR
        mgr._scheduler = BackgroundScheduler()
        mgr._scheduler.start()
        mgr._config = {
            "test_mode": False,
            "search_configs": [],
            "auto_generate_threshold": 35.0,
            "cleanup_days": 30,
        }
        mgr._is_running = True
        mgr._scheduler.add_listener(lambda e: None, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)

        try:
            with patch("web.settings_manager.SettingsManager", return_value=sm_mock):
                mgr.reschedule_task("scrape")
            job_ids = [j.id for j in mgr._scheduler.get_jobs()]
            self.assertIn("scrape_jobs", job_ids)
        finally:
            mgr._scheduler.shutdown(wait=False)

    def test_reschedule_task_removes_job_when_manual(self):
        """reschedule_task('scrape') removes job when mode is manual."""
        from scheduler.scheduler import SchedulerManager
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR
        from scheduler.tasks import scrape_jobs_task

        sm_mock = MagicMock()
        sm_mock.get_mode.return_value = "manual"

        mgr = SchedulerManager.__new__(SchedulerManager)
        mgr._scheduler = BackgroundScheduler()
        mgr._scheduler.start()
        mgr._config = {"test_mode": False, "search_configs": [], "auto_generate_threshold": 35.0, "cleanup_days": 30}
        mgr._is_running = True
        mgr._scheduler.add_listener(lambda e: None, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)

        # Pre-add the job so we can verify removal
        mgr._scheduler.add_job(
            scrape_jobs_task, args=[[]], trigger=CronTrigger(hour=9),
            id="scrape_jobs", replace_existing=True,
        )
        self.assertIn("scrape_jobs", [j.id for j in mgr._scheduler.get_jobs()])

        try:
            with patch("web.settings_manager.SettingsManager", return_value=sm_mock):
                mgr.reschedule_task("scrape")
            job_ids = [j.id for j in mgr._scheduler.get_jobs()]
            self.assertNotIn("scrape_jobs", job_ids)
        finally:
            mgr._scheduler.shutdown(wait=False)

    def test_reschedule_task_skips_when_scheduler_not_running(self):
        """reschedule_task() does not raise if scheduler is stopped."""
        from scheduler.scheduler import SchedulerManager
        from apscheduler.schedulers.background import BackgroundScheduler

        mgr = SchedulerManager.__new__(SchedulerManager)
        mgr._scheduler = BackgroundScheduler()
        mgr._config = {}
        mgr._is_running = False  # not running

        # Should not raise
        mgr.reschedule_task("scrape")


# ---------------------------------------------------------------------------
# Dashboard integration: auto badge
# ---------------------------------------------------------------------------

class TestDashboardAutoBadge(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        import web.app as app_module
        self._orig_sm = app_module.settings_manager
        app_module.settings_manager = make_sm(self.tmp)
        from web.app import app
        app.config["TESTING"] = True
        self.client = app.test_client()

    def tearDown(self):
        import web.app as app_module
        app_module.settings_manager = self._orig_sm

    def _get_dashboard_html(self) -> str:
        """Fetch dashboard, mocking the DB layer to avoid sqlite table errors."""
        from unittest.mock import MagicMock, patch

        mock_db = MagicMock()
        mock_db.__enter__ = lambda s: mock_db
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_query = MagicMock()
        mock_query.count.return_value = 0
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.limit.return_value = mock_query
        mock_query.options.return_value = mock_query
        mock_query.all.return_value = []
        mock_db.query.return_value = mock_query

        import web.app as app_module
        with patch.object(app_module, "get_db", return_value=mock_db):
            r = self.client.get("/dashboard")
        self.assertEqual(r.status_code, 200)
        return r.data.decode("utf-8", errors="replace")

    def test_dashboard_contains_auto_badge_elements(self):
        html = self._get_dashboard_html()
        self.assertIn("badge-scrape", html)
        self.assertIn("badge-generate", html)
        self.assertIn("refreshAutoBadges", html)
        self.assertIn("/api/settings", html)

    def test_settings_nav_link_in_base(self):
        html = self._get_dashboard_html()
        self.assertIn("/settings", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
