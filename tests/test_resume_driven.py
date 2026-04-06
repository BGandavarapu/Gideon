"""Tests for the Resume-Driven Job Search feature.

Covers:
- Sample resume seeding (9 domains, unique domains, style_fingerprint populated)
- GET /api/sample-resume/<domain>
- PATCH /api/resume/mode with optional domain param
- GET /api/active-context
- SettingsManager.get_industry_search_configs + get_active_domain
- scrape_jobs_task() resume-driven domain logic
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analyzer.domain_detector import DOMAINS
from database.database import create_tables, drop_tables, get_db, reset_manager
from database.models import MasterResume
from web.settings_manager import SettingsManager

IN_MEMORY = "sqlite:///:memory:"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_sm(tmp_dir: str) -> SettingsManager:
    sm = SettingsManager()
    sm.SETTINGS_PATH = str(Path(tmp_dir) / "settings.json")
    return sm


def _make_flask_app(tmp_dir: str):
    """Create Flask test client with in-memory DB and isolated settings."""
    reset_manager(IN_MEMORY)
    create_tables()
    os.environ["NVIDIA_API_KEY"] = "test-key"

    import importlib
    import web.app as app_module
    importlib.reload(app_module)

    app_module.settings_manager.SETTINGS_PATH = str(Path(tmp_dir) / "settings.json")
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client(), app_module.settings_manager


def _seed_sample(db, domain: str, name: str = None) -> MasterResume:
    mr = MasterResume(
        name=name or f"{domain.replace('_', ' ').title()} Sample Resume",
        content={"skills": ["Python", "SQL"], "work_experience": [],
                 "education": [], "professional_summary": "Test"},
        is_active=False,
        is_sample=True,
        domain=domain,
        style_fingerprint=None,
    )
    db.add(mr)
    db.commit()
    return mr


# ---------------------------------------------------------------------------
# Seeding tests (use the real data/sample_resumes/ directory)
# ---------------------------------------------------------------------------

class TestSampleResumeSeeding(unittest.TestCase):

    def setUp(self):
        reset_manager(IN_MEMORY)
        create_tables()

    def tearDown(self):
        drop_tables()
        reset_manager(None)

    def test_sample_resumes_seeded_for_all_domains(self):
        """All 9 industry domains should have a sample resume after init."""
        with get_db() as db:
            samples = db.query(MasterResume).filter(MasterResume.is_sample == True).all()
        # Must have at least 9 (one per domain)
        self.assertGreaterEqual(len(samples), 9)

    def test_sample_resume_domains_are_unique(self):
        """No two sample resumes should share the same domain."""
        with get_db() as db:
            samples = db.query(MasterResume).filter(MasterResume.is_sample == True).all()
        domains = [s.domain for s in samples if s.domain]
        self.assertEqual(len(domains), len(set(domains)), "Duplicate sample domains found")

    def test_sample_resumes_have_content(self):
        """Each sample resume must have a non-empty skills list."""
        with get_db() as db:
            samples = db.query(MasterResume).filter(MasterResume.is_sample == True).all()
        for s in samples:
            content = s.content or {}
            skills = content.get("skills", [])
            self.assertGreater(
                len(skills), 0,
                f"Sample resume '{s.name}' has no skills",
            )

    def test_sample_resumes_have_domain(self):
        """Each sample resume must have a non-null domain."""
        with get_db() as db:
            samples = db.query(MasterResume).filter(MasterResume.is_sample == True).all()
        for s in samples:
            self.assertIsNotNone(s.domain, f"Sample resume '{s.name}' has no domain")

    def test_sample_resumes_have_style_fingerprint(self):
        """Each sample resume should have a style_fingerprint extracted."""
        with get_db() as db:
            samples = db.query(MasterResume).filter(MasterResume.is_sample == True).all()
        for s in samples:
            self.assertIsNotNone(
                s.style_fingerprint,
                f"Sample resume '{s.name}' has no style_fingerprint",
            )


# ---------------------------------------------------------------------------
# SettingsManager industry config tests
# ---------------------------------------------------------------------------

class TestIndustrySearchConfigs(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sm = make_sm(self.tmp)

    def test_all_domains_have_industry_configs(self):
        """Every domain in DOMAINS (except 'other') should have >= 2 configs."""
        for domain in DOMAINS:
            if domain == "other":
                continue
            configs = self.sm.get_industry_search_configs(domain)
            self.assertGreaterEqual(
                len(configs), 2,
                f"Domain '{domain}' has fewer than 2 industry configs",
            )

    def test_industry_configs_have_required_keys(self):
        """Each config must have keywords, location, source, max_results."""
        for domain in DOMAINS:
            for cfg in self.sm.get_industry_search_configs(domain):
                for key in ("keywords", "location", "source", "max_results"):
                    self.assertIn(key, cfg, f"Config for {domain} missing key '{key}'")

    def test_unknown_domain_returns_empty(self):
        configs = self.sm.get_industry_search_configs("space_cowboy")
        self.assertEqual(configs, [])

    def test_marketing_configs_contain_marketing_keywords(self):
        configs = self.sm.get_industry_search_configs("marketing")
        keywords = [c["keywords"].lower() for c in configs]
        self.assertTrue(
            any("marketing" in k for k in keywords),
            "No 'marketing' keyword in marketing industry configs",
        )

    def test_get_active_domain_returns_none_when_no_active(self):
        """When no active resume, get_active_domain returns None."""
        reset_manager(IN_MEMORY)
        create_tables()
        try:
            domain = self.sm.get_active_domain()
            self.assertIsNone(domain)
        finally:
            drop_tables()
            reset_manager(None)

    def test_get_active_domain_returns_domain(self):
        """When an active resume with domain is set, get_active_domain returns it."""
        reset_manager(IN_MEMORY)
        create_tables()
        try:
            with get_db() as db:
                mr = MasterResume(
                    name="Test", content={"skills": []},
                    is_active=True, is_sample=False, domain="finance",
                )
                db.add(mr)
                db.commit()
            result = self.sm.get_active_domain()
            self.assertEqual(result, "finance")
        finally:
            drop_tables()
            reset_manager(None)


# ---------------------------------------------------------------------------
# Flask API tests
# ---------------------------------------------------------------------------

class TestSampleResumeAPI(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.client, self.sm = _make_flask_app(self.tmp)

    def tearDown(self):
        drop_tables()
        reset_manager(None)

    def test_get_sample_resume_software(self):
        """GET /api/sample-resume/software_engineering returns 200 with data."""
        res = self.client.get("/api/sample-resume/software_engineering")
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertIn("name", data)
        self.assertGreaterEqual(data["skills_count"], 5)
        self.assertIn("industry_search_configs", data)
        self.assertIsInstance(data["industry_search_configs"], list)
        self.assertGreater(len(data["industry_search_configs"]), 0)

    def test_get_sample_resume_marketing(self):
        """GET /api/sample-resume/marketing returns Sofia Martinez."""
        with get_db() as db:
            _seed_sample(db, "marketing", "Marketing Sample Resume")
        res = self.client.get("/api/sample-resume/marketing")
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertEqual(data["domain"], "marketing")
        self.assertIn("industry_search_configs", data)

    def test_get_sample_resume_invalid_domain(self):
        """GET /api/sample-resume/<unknown> returns 404."""
        res = self.client.get("/api/sample-resume/underwater_basket_weaving")
        self.assertEqual(res.status_code, 404)

    def test_get_sample_resume_domain_missing_from_db(self):
        """GET /api/sample-resume/<valid_domain> with no DB row returns 404."""
        # Drop all sample resumes to simulate missing domain
        with get_db() as db:
            db.query(MasterResume).filter(MasterResume.domain == "design").delete()
            db.commit()
        res = self.client.get("/api/sample-resume/design")
        self.assertEqual(res.status_code, 404)


class TestPatchResumeModeWithDomain(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.client, self.sm = _make_flask_app(self.tmp)

    def tearDown(self):
        drop_tables()
        reset_manager(None)

    def test_patch_mode_sample_with_domain(self):
        """PATCH /api/resume/mode with domain activates correct sample."""
        # Activate marketing sample and verify it's active
        res = self.client.patch(
            "/api/resume/mode",
            data=json.dumps({"mode": "sample", "domain": "marketing"}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertEqual(data["domain"], "marketing")
        # Verify DB state: marketing sample is active, others are not
        with get_db() as db:
            active_samples = db.query(MasterResume).filter(
                MasterResume.is_sample == True,
                MasterResume.is_active == True,
            ).all()
            self.assertEqual(len(active_samples), 1)
            self.assertEqual(active_samples[0].domain, "marketing")

    def test_patch_mode_sample_without_domain_backward_compatible(self):
        """PATCH /api/resume/mode without domain still works (any sample)."""
        res = self.client.patch(
            "/api/resume/mode",
            data=json.dumps({"mode": "sample"}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertEqual(data["status"], "switched")

    def test_patch_mode_sample_invalid_domain(self):
        """PATCH with invalid domain returns 400."""
        res = self.client.patch(
            "/api/resume/mode",
            data=json.dumps({"mode": "sample", "domain": "space_cowboy"}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 400)

    def test_patch_mode_sample_missing_domain_in_db(self):
        """PATCH with domain that has no DB sample returns 404."""
        # Use a valid domain key but remove its sample from DB first
        with get_db() as db:
            db.query(MasterResume).filter(
                MasterResume.is_sample == True,
                MasterResume.domain == "design",
            ).delete()
            db.commit()
        res = self.client.patch(
            "/api/resume/mode",
            data=json.dumps({"mode": "sample", "domain": "design"}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 404)


class TestActiveContextAPI(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.client, self.sm = _make_flask_app(self.tmp)

    def tearDown(self):
        drop_tables()
        reset_manager(None)

    def test_active_context_returns_all_fields(self):
        """GET /api/active-context returns all required keys."""
        res = self.client.get("/api/active-context")
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertIn("active_resume", data)
        self.assertIn("industry_search_configs", data)
        self.assertIn("user_search_configs", data)
        self.assertIn("total_configs", data)
        self.assertIn("mode", data)

    def test_active_context_industry_configs_match_domain(self):
        """When active resume is marketing, industry configs contain marketing queries."""
        with get_db() as db:
            mr = MasterResume(
                name="Marketing Resume", content={"skills": ["SEO"]},
                is_active=True, is_sample=True, domain="marketing",
            )
            db.add(mr)
            db.commit()

        res = self.client.get("/api/active-context")
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertIsNotNone(data["active_resume"])
        self.assertEqual(data["active_resume"]["domain"], "marketing")

        cfgs = data["industry_search_configs"]
        self.assertGreater(len(cfgs), 0)
        keywords = [c["keywords"].lower() for c in cfgs]
        self.assertTrue(
            any("marketing" in k for k in keywords),
            f"No marketing keyword in configs: {keywords}",
        )

    def test_active_context_no_active_resume(self):
        """When no active resume, active_resume is None."""
        with get_db() as db:
            db.query(MasterResume).update({"is_active": False})
            db.commit()
        res = self.client.get("/api/active-context")
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertIsNone(data["active_resume"])


# ---------------------------------------------------------------------------
# Scheduler task tests
# ---------------------------------------------------------------------------

class TestScrapingResumeDriven(unittest.TestCase):

    def setUp(self):
        reset_manager(IN_MEMORY)
        create_tables()

    def tearDown(self):
        drop_tables()
        reset_manager(None)

    @patch("scraper.linkedin_scraper.LinkedInScraper")
    @patch("scraper.db_handler.save_postings_to_db")
    def test_scrape_uses_active_domain_industry_configs(self, mock_save, mock_scraper_cls):
        """scrape_jobs_task uses industry configs for the active resume's domain."""
        mock_scraper = MagicMock()
        mock_scraper.__enter__ = lambda s: s
        mock_scraper.__exit__ = MagicMock(return_value=False)
        mock_scraper.scrape.return_value = []
        mock_scraper_cls.return_value = mock_scraper

        batch = MagicMock(); batch.saved = 0; batch.updated = 0
        mock_save.return_value = batch

        # Set active resume domain = marketing
        with get_db() as db:
            mr = MasterResume(
                name="Marketing Resume", content={"skills": []},
                is_active=True, is_sample=True, domain="marketing",
            )
            db.add(mr)
            db.commit()

        from scheduler.tasks import scrape_jobs_task
        result = scrape_jobs_task()

        # All scrape calls should use marketing-related keywords
        self.assertGreater(mock_scraper.scrape.call_count, 0)
        called_keywords = [
            call[1].get("keywords", "") if call[1] else ""
            for call in mock_scraper.scrape.call_args_list
        ]
        # At least one call should mention "marketing"
        self.assertTrue(
            any("marketing" in k.lower() for k in called_keywords),
            f"No marketing keywords in scrape calls: {called_keywords}",
        )

    @patch("scraper.linkedin_scraper.LinkedInScraper")
    @patch("scraper.db_handler.save_postings_to_db")
    def test_scrape_falls_back_when_no_domain(self, mock_save, mock_scraper_cls):
        """scrape_jobs_task falls back to user search_configs when no active domain."""
        mock_scraper = MagicMock()
        mock_scraper.__enter__ = lambda s: s
        mock_scraper.__exit__ = MagicMock(return_value=False)
        mock_scraper.scrape.return_value = []
        mock_scraper_cls.return_value = mock_scraper

        batch = MagicMock(); batch.saved = 0; batch.updated = 0
        mock_save.return_value = batch

        # Active resume with NO domain
        with get_db() as db:
            mr = MasterResume(
                name="Resume No Domain", content={"skills": []},
                is_active=True, is_sample=False, domain=None,
            )
            db.add(mr)
            db.commit()

        with tempfile.TemporaryDirectory() as tmp:
            sm = SettingsManager()
            sm.SETTINGS_PATH = str(Path(tmp) / "settings.json")
            # User has 2 enabled configs
            sm.add_search_config({"keywords": "devops engineer", "location": "Remote",
                                   "source": "linkedin", "domain": "software_engineering"})
            sm.add_search_config({"keywords": "data engineer", "location": "NYC",
                                   "source": "linkedin", "domain": "data_analytics"})

            from scheduler.tasks import scrape_jobs_task
            with patch.object(SettingsManager, "SETTINGS_PATH", sm.SETTINGS_PATH):
                result = scrape_jobs_task()

        # Should have called scraper for the 2 user configs (no domain = fallback)
        self.assertGreaterEqual(mock_scraper.scrape.call_count, 2)


if __name__ == "__main__":
    unittest.main()
