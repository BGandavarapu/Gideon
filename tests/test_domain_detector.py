"""Tests for the Multi-Domain Job Search feature.

Covers:
- DomainDetector.detect_from_text() for each major domain
- Title keyword weighting (3x)
- detect_from_resume() and detect_from_job()
- SettingsManager CRUD for search_configs and domain_resumes
- Flask API routes: GET/POST/PATCH/DELETE search-configs, GET/PATCH domain-resumes
- Scheduler task integration: scrape sets domain, generate uses domain resume
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

from analyzer.domain_detector import DOMAIN_KEYWORDS, DOMAINS, DomainDetector
from database.database import create_tables, drop_tables, get_db, reset_manager
from database.models import Job, MasterResume
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

    # Must import app AFTER reset so it picks up in-memory DB
    import importlib
    import web.app as app_module
    importlib.reload(app_module)

    app_module.settings_manager.SETTINGS_PATH = str(Path(tmp_dir) / "settings.json")
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client(), app_module.settings_manager


# ---------------------------------------------------------------------------
# DomainDetector unit tests
# ---------------------------------------------------------------------------

class TestDomainDetectorFromText(unittest.TestCase):

    def setUp(self):
        self.d = DomainDetector()
        # Patch out NIM so unit tests only exercise the heuristic
        self._nim_patch = patch.object(
            DomainDetector, "detect_with_nvidia", return_value=None
        )
        self._nim_patch.start()

    def tearDown(self):
        self._nim_patch.stop()

    def test_detect_software_engineering_from_title(self):
        result = self.d.detect_from_text(
            "We are looking for an experienced developer to build scalable systems.",
            job_title="Senior Software Engineer",
        )
        self.assertEqual(result["domain"], "software_engineering")
        self.assertIn("display_name", result)
        self.assertIn("confidence", result)

    def test_detect_marketing_from_text(self):
        result = self.d.detect_from_text(
            "SEO specialist with Google Analytics experience managing PPC campaigns "
            "and email marketing campaigns.",
            job_title="Digital Marketing Manager",
        )
        self.assertEqual(result["domain"], "marketing")

    def test_detect_product_management(self):
        result = self.d.detect_from_text(
            "We need a product manager to own the roadmap and OKRs, managing "
            "backlog grooming and sprint planning with stakeholders.",
            job_title="Product Manager",
        )
        self.assertEqual(result["domain"], "product_management")

    def test_detect_ai_ml(self):
        result = self.d.detect_from_text(
            "ML engineer building neural networks and large language models (LLMs) "
            "with deep learning experience.",
            job_title="Machine Learning Engineer",
        )
        self.assertEqual(result["domain"], "ai_ml")

    def test_detect_finance(self):
        result = self.d.detect_from_text(
            "Financial analyst for FP&A and DCF financial modeling and valuation.",
            job_title="Financial Analyst",
        )
        self.assertEqual(result["domain"], "finance")

    def test_detect_design(self):
        result = self.d.detect_from_text(
            "UX designer with Figma experience, wireframing and prototyping, "
            "conducting user research and usability testing.",
            job_title="Product Designer",
        )
        self.assertEqual(result["domain"], "design")

    def test_detect_sales(self):
        result = self.d.detect_from_text(
            "Account executive for enterprise sales, B2B SaaS sales, pipeline "
            "management and quota attainment.",
            job_title="Account Executive",
        )
        self.assertEqual(result["domain"], "sales")

    def test_detect_operations(self):
        result = self.d.detect_from_text(
            "Operations manager for process improvement, supply chain, vendor "
            "management and project management.",
            job_title="Operations Manager",
        )
        self.assertEqual(result["domain"], "operations")

    def test_detect_data_analytics(self):
        result = self.d.detect_from_text(
            "Data analyst with SQL, Tableau and Power BI experience, building "
            "dashboards and performing statistical analysis.",
            job_title="Data Analyst",
        )
        self.assertEqual(result["domain"], "data_analytics")

    def test_detect_other_for_unknown(self):
        result = self.d.detect_from_text(
            "General assistant role with varied duties. Performing administrative tasks.",
        )
        self.assertEqual(result["domain"], "other")

    def test_title_weighted_higher_than_body(self):
        """Title 'Software Engineer' should beat 5 marketing mentions in body."""
        result = self.d.detect_from_text(
            "marketing marketing marketing marketing marketing seo ppc email campaigns",
            job_title="Software Engineer",
        )
        self.assertEqual(result["domain"], "software_engineering")

    def test_returns_scores_dict(self):
        result = self.d.detect_from_text("python developer backend", job_title="")
        self.assertIn("scores", result)
        self.assertIsInstance(result["scores"], dict)
        for domain in DOMAIN_KEYWORDS:
            self.assertIn(domain, result["scores"])

    def test_empty_text_returns_other(self):
        result = self.d.detect_from_text("")
        self.assertEqual(result["domain"], "other")

    def test_confidence_between_0_and_1(self):
        result = self.d.detect_from_text(
            "software engineer backend python",
            job_title="Software Engineer",
        )
        self.assertGreaterEqual(result["confidence"], 0.0)
        self.assertLessEqual(result["confidence"], 1.0)


class TestDomainDetectorFromResume(unittest.TestCase):

    def setUp(self):
        self.d = DomainDetector()
        self._nim_patch = patch.object(
            DomainDetector, "detect_with_nvidia", return_value=None
        )
        self._nim_patch.start()

    def tearDown(self):
        self._nim_patch.stop()

    def test_detect_software_engineering_from_resume(self):
        content = {
            "professional_summary": "Backend software engineer with 5 years building scalable APIs.",
            "work_experience": [
                {
                    "title": "Software Engineer",
                    "company": "TechCorp",
                    "bullets": [
                        "Built backend REST APIs using Python and Django",
                        "Deployed infrastructure on AWS",
                    ],
                }
            ],
            "skills": ["Python", "Django", "AWS", "PostgreSQL"],
        }
        result = self.d.detect_from_resume(content)
        self.assertEqual(result["domain"], "software_engineering")

    def test_detect_marketing_from_resume(self):
        content = {
            "professional_summary": "Digital marketing manager with SEO and Google Analytics expertise.",
            "work_experience": [
                {
                    "title": "Marketing Manager",
                    "company": "BrandCo",
                    "bullets": [
                        "Led SEO and SEM campaigns using Google Ads and PPC",
                        "Managed email marketing with Mailchimp and HubSpot",
                    ],
                }
            ],
            "skills": ["SEO", "Google Analytics", "HubSpot", "Mailchimp"],
        }
        result = self.d.detect_from_resume(content)
        self.assertEqual(result["domain"], "marketing")

    def test_empty_resume_returns_other(self):
        result = self.d.detect_from_resume({})
        self.assertEqual(result["domain"], "other")


class TestDomainDetectorFromJob(unittest.TestCase):

    def setUp(self):
        self.d = DomainDetector()
        self._nim_patch = patch.object(
            DomainDetector, "detect_with_nvidia", return_value=None
        )
        self._nim_patch.start()

    def tearDown(self):
        self._nim_patch.stop()

    def test_detect_from_job_object(self):
        job = SimpleNamespace(
            job_title="Senior Product Manager",
            job_description="Own the product roadmap, manage OKRs and user stories.",
        )
        result = self.d.detect_from_job(job)
        self.assertEqual(result["domain"], "product_management")

    def test_from_job_missing_attributes(self):
        """Should not raise even if job object has no attributes."""
        result = self.d.detect_from_job(object())
        self.assertEqual(result["domain"], "other")


# ---------------------------------------------------------------------------
# SettingsManager search config CRUD tests
# ---------------------------------------------------------------------------

class TestSettingsSearchConfigs(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sm = make_sm(self.tmp)

    def test_default_configs_present(self):
        configs = self.sm.get_search_configs(enabled_only=False)
        self.assertGreaterEqual(len(configs), 3)

    def test_add_search_config_valid(self):
        new_id = self.sm.add_search_config({
            "keywords":    "marketing manager",
            "location":    "New York",
            "source":      "linkedin",
            "max_results": 15,
            "domain":      "marketing",
            "enabled":     True,
        })
        self.assertTrue(new_id.startswith("sc_"))
        configs = self.sm.get_search_configs(enabled_only=False)
        ids = [c["id"] for c in configs]
        self.assertIn(new_id, ids)

    def test_add_search_config_invalid_domain(self):
        with self.assertRaises(ValueError):
            self.sm.add_search_config({
                "keywords": "foo", "location": "bar",
                "source": "linkedin", "domain": "underwater_basket_weaving",
            })

    def test_add_search_config_invalid_source(self):
        with self.assertRaises(ValueError):
            self.sm.add_search_config({
                "keywords": "foo", "location": "bar",
                "source": "indeed", "domain": "marketing",
            })

    def test_add_search_config_missing_field(self):
        with self.assertRaises(ValueError):
            self.sm.add_search_config({
                "location": "bar", "source": "linkedin", "domain": "marketing",
            })

    def test_update_search_config(self):
        configs = self.sm.get_search_configs(enabled_only=False)
        cfg_id = configs[0]["id"]
        ok = self.sm.update_search_config(cfg_id, {"enabled": False, "max_results": 5})
        self.assertTrue(ok)
        updated = self.sm.get_search_configs(enabled_only=False)
        cfg = next(c for c in updated if c["id"] == cfg_id)
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["max_results"], 5)

    def test_update_nonexistent_config_returns_false(self):
        ok = self.sm.update_search_config("sc_nonexistent", {"enabled": False})
        self.assertFalse(ok)

    def test_delete_search_config(self):
        new_id = self.sm.add_search_config({
            "keywords": "designer", "location": "SF",
            "source": "linkedin", "domain": "design",
        })
        ok = self.sm.delete_search_config(new_id)
        self.assertTrue(ok)
        ids = [c["id"] for c in self.sm.get_search_configs(enabled_only=False)]
        self.assertNotIn(new_id, ids)

    def test_delete_nonexistent_returns_false(self):
        ok = self.sm.delete_search_config("sc_9999")
        self.assertFalse(ok)

    def test_enabled_only_filter(self):
        configs = self.sm.get_search_configs(enabled_only=False)
        if configs:
            self.sm.update_search_config(configs[0]["id"], {"enabled": False})
        all_configs = self.sm.get_search_configs(enabled_only=False)
        enabled_configs = self.sm.get_search_configs(enabled_only=True)
        self.assertLessEqual(len(enabled_configs), len(all_configs))


class TestSettingsDomainResumes(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sm = make_sm(self.tmp)

    def test_default_domain_resume_is_none(self):
        for domain in DOMAINS:
            self.assertIsNone(self.sm.get_domain_resume(domain))

    def test_set_and_get_domain_resume(self):
        self.sm.set_domain_resume("marketing", 42)
        self.assertEqual(self.sm.get_domain_resume("marketing"), 42)

    def test_clear_domain_resume(self):
        self.sm.set_domain_resume("sales", 10)
        self.sm.set_domain_resume("sales", None)
        self.assertIsNone(self.sm.get_domain_resume("sales"))

    def test_invalid_domain_raises(self):
        with self.assertRaises(ValueError):
            self.sm.set_domain_resume("underwater_basket_weaving", 1)


# ---------------------------------------------------------------------------
# Flask API tests
# ---------------------------------------------------------------------------

class TestSearchConfigsAPI(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.client, self.sm = _make_flask_app(self.tmp)

    def tearDown(self):
        drop_tables()
        reset_manager(None)

    def test_get_search_configs(self):
        res = self.client.get("/api/search-configs")
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertIn("configs", data)
        self.assertGreaterEqual(data["total"], 3)

    def test_add_search_config_valid(self):
        payload = {
            "keywords": "marketing manager", "location": "New York",
            "source": "linkedin", "max_results": 10,
            "domain": "marketing", "enabled": True,
        }
        res = self.client.post(
            "/api/search-configs",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 201)
        data = json.loads(res.data)
        self.assertEqual(data["status"], "created")
        self.assertIn("id", data)

    def test_add_search_config_invalid_domain(self):
        payload = {
            "keywords": "foo", "location": "bar",
            "source": "linkedin", "domain": "space_cowboy",
        }
        res = self.client.post(
            "/api/search-configs",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 400)

    def test_update_search_config(self):
        # First get a config id
        res = self.client.get("/api/search-configs")
        configs = json.loads(res.data)["configs"]
        cfg_id = configs[0]["id"]

        res2 = self.client.patch(
            f"/api/search-configs/{cfg_id}",
            data=json.dumps({"enabled": False}),
            content_type="application/json",
        )
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(json.loads(res2.data)["status"], "updated")

    def test_update_nonexistent_config(self):
        res = self.client.patch(
            "/api/search-configs/sc_nonexistent",
            data=json.dumps({"enabled": False}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 404)

    def test_delete_search_config(self):
        # Add one first
        payload = {
            "keywords": "temp delete", "location": "Nowhere",
            "source": "linkedin", "domain": "operations",
        }
        res = self.client.post(
            "/api/search-configs",
            data=json.dumps(payload),
            content_type="application/json",
        )
        cfg_id = json.loads(res.data)["id"]

        res2 = self.client.delete(f"/api/search-configs/{cfg_id}")
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(json.loads(res2.data)["status"], "deleted")

    def test_delete_nonexistent_config(self):
        res = self.client.delete("/api/search-configs/sc_nonexistent_9999")
        self.assertEqual(res.status_code, 404)


class TestDomainResumesAPI(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.client, self.sm = _make_flask_app(self.tmp)

    def tearDown(self):
        drop_tables()
        reset_manager(None)

    def _seed_resume(self) -> int:
        with get_db() as db:
            mr = MasterResume(
                name="Test Resume",
                content={"skills": ["Python"]},
                is_active=True,
                is_sample=False,
            )
            db.add(mr)
            db.commit()
            return mr.id

    def test_get_domain_resumes(self):
        res = self.client.get("/api/domain-resumes")
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertIn("mappings", data)
        for domain in DOMAINS:
            self.assertIn(domain, data["mappings"])

    def test_set_domain_resume_valid(self):
        resume_id = self._seed_resume()
        res = self.client.patch(
            "/api/domain-resumes/marketing",
            data=json.dumps({"resume_id": resume_id}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(json.loads(res.data)["resume_id"], resume_id)
        self.assertEqual(self.sm.get_domain_resume("marketing"), resume_id)

    def test_set_domain_resume_invalid_domain(self):
        res = self.client.patch(
            "/api/domain-resumes/invalid_domain",
            data=json.dumps({"resume_id": 1}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 400)

    def test_set_domain_resume_nonexistent_resume(self):
        res = self.client.patch(
            "/api/domain-resumes/marketing",
            data=json.dumps({"resume_id": 9999}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 404)

    def test_set_domain_resume_clear(self):
        resume_id = self._seed_resume()
        self.sm.set_domain_resume("sales", resume_id)
        res = self.client.patch(
            "/api/domain-resumes/sales",
            data=json.dumps({"resume_id": None}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(self.sm.get_domain_resume("sales"))


class TestResumeDomainOverrideAPI(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.client, self.sm = _make_flask_app(self.tmp)
        with get_db() as db:
            mr = MasterResume(
                name="Test Resume",
                content={"skills": ["Python"]},
                is_active=True,
                is_sample=True,
            )
            db.add(mr)
            db.commit()
            self.resume_id = mr.id

    def tearDown(self):
        drop_tables()
        reset_manager(None)

    def test_patch_resume_domain_valid(self):
        res = self.client.patch(
            f"/api/resume/{self.resume_id}/domain",
            data=json.dumps({"domain": "marketing"}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertEqual(data["domain"], "marketing")

    def test_patch_resume_domain_invalid(self):
        res = self.client.patch(
            f"/api/resume/{self.resume_id}/domain",
            data=json.dumps({"domain": "space_cowboy"}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 400)

    def test_patch_resume_domain_not_found(self):
        res = self.client.patch(
            "/api/resume/9999/domain",
            data=json.dumps({"domain": "marketing"}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 404)


# ---------------------------------------------------------------------------
# Master resume delete API tests
# ---------------------------------------------------------------------------


class TestDeleteMasterResumeAPI(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.client, _ = _make_flask_app(self.tmp)
        with get_db() as db:
            # One sample resume (should not be deletable)
            sample = MasterResume(
                name="Sample Resume",
                content={"skills": []},
                is_active=False,
                is_sample=True,
                domain="software_engineering",
            )
            # One uploaded, active resume
            active = MasterResume(
                name="Active Upload",
                content={"skills": ["Python"]},
                is_active=True,
                is_sample=False,
                domain="software_engineering",
            )
            # One uploaded, inactive resume
            inactive = MasterResume(
                name="Old Upload",
                content={"skills": ["Java"]},
                is_active=False,
                is_sample=False,
                domain="marketing",
            )
            db.add_all([sample, active, inactive])
            db.commit()
            self.sample_id   = sample.id
            self.active_id   = active.id
            self.inactive_id = inactive.id

    def tearDown(self):
        drop_tables()
        reset_manager(None)

    def test_delete_inactive_uploaded_resume_succeeds(self):
        res = self.client.delete(f"/api/resumes/master/{self.inactive_id}")
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["deleted_id"], self.inactive_id)

    def test_deleted_resume_no_longer_in_db(self):
        self.client.delete(f"/api/resumes/master/{self.inactive_id}")
        with get_db() as db:
            remaining = db.query(MasterResume).filter(MasterResume.id == self.inactive_id).first()
        self.assertIsNone(remaining)

    def test_delete_sample_resume_returns_403(self):
        res = self.client.delete(f"/api/resumes/master/{self.sample_id}")
        self.assertEqual(res.status_code, 403)
        data = json.loads(res.data)
        self.assertIn("error", data)

    def test_sample_resume_not_deleted_after_403(self):
        self.client.delete(f"/api/resumes/master/{self.sample_id}")
        with get_db() as db:
            still_there = db.query(MasterResume).filter(MasterResume.id == self.sample_id).first()
        self.assertIsNotNone(still_there)

    def test_delete_active_resume_returns_409(self):
        res = self.client.delete(f"/api/resumes/master/{self.active_id}")
        self.assertEqual(res.status_code, 409)
        data = json.loads(res.data)
        self.assertIn("error", data)

    def test_active_resume_not_deleted_after_409(self):
        self.client.delete(f"/api/resumes/master/{self.active_id}")
        with get_db() as db:
            still_active = db.query(MasterResume).filter(
                MasterResume.id == self.active_id, MasterResume.is_active == True
            ).first()
        self.assertIsNotNone(still_active)

    def test_delete_nonexistent_resume_returns_404(self):
        res = self.client.delete("/api/resumes/master/9999")
        self.assertEqual(res.status_code, 404)


# ---------------------------------------------------------------------------
# Scheduler task integration tests
# ---------------------------------------------------------------------------

class TestScraperTaskDomain(unittest.TestCase):

    def setUp(self):
        reset_manager(IN_MEMORY)
        create_tables()

    def tearDown(self):
        drop_tables()
        reset_manager(None)

    @patch("scraper.linkedin_scraper.LinkedInScraper")
    @patch("scraper.db_handler.save_postings_to_db")
    def test_scrape_task_reads_from_settings_when_no_args(
        self, mock_save, mock_scraper_cls
    ):
        """scrape_jobs_task() with no args should read configs from SettingsManager."""
        from scheduler.tasks import scrape_jobs_task

        mock_scraper = MagicMock()
        mock_scraper.__enter__ = lambda s: s
        mock_scraper.__exit__ = MagicMock(return_value=False)
        mock_scraper.scrape.return_value = []
        mock_scraper_cls.return_value = mock_scraper

        batch = MagicMock()
        batch.saved = 0
        batch.updated = 0
        mock_save.return_value = batch

        with tempfile.TemporaryDirectory() as tmp:
            sm = SettingsManager()
            sm.SETTINGS_PATH = str(Path(tmp) / "settings.json")
            # Patch SettingsManager inside tasks
            with patch("web.settings_manager.SettingsManager", return_value=sm):
                result = scrape_jobs_task()  # no args — reads from settings

        # Default settings have 3 configs, so scraper should be called 3 times
        self.assertEqual(mock_scraper.scrape.call_count, 3)

    @patch("scraper.linkedin_scraper.LinkedInScraper")
    @patch("scraper.db_handler.save_postings_to_db")
    def test_scrape_task_uses_provided_configs(self, mock_save, mock_scraper_cls):
        """Explicit configs are passed through correctly."""
        from scheduler.tasks import scrape_jobs_task

        mock_scraper = MagicMock()
        mock_scraper.__enter__ = lambda s: s
        mock_scraper.__exit__ = MagicMock(return_value=False)
        mock_scraper.scrape.return_value = []
        mock_scraper_cls.return_value = mock_scraper

        batch = MagicMock()
        batch.saved = 0
        batch.updated = 0
        mock_save.return_value = batch

        configs = [
            {"keywords": "marketing manager", "location": "NY",
             "max_results": 5, "domain": "marketing"},
        ]
        result = scrape_jobs_task(search_configs=configs)
        self.assertEqual(mock_scraper.scrape.call_count, 1)


class TestGenerateTaskDomainResume(unittest.TestCase):

    def setUp(self):
        reset_manager(IN_MEMORY)
        create_tables()

    def tearDown(self):
        drop_tables()
        reset_manager(None)

    def test_get_resume_for_job_falls_back_to_active(self):
        """When no domain mapping exists, active resume is used."""
        from scheduler.tasks import _get_resume_for_job

        with get_db() as db:
            mr = MasterResume(
                name="Active Resume", content={"skills": []},
                is_active=True, is_sample=False,
            )
            db.add(mr)
            db.commit()
            mr_id = mr.id

            job = SimpleNamespace(domain="design")
            with tempfile.TemporaryDirectory() as tmp:
                sm = SettingsManager()
                sm.SETTINGS_PATH = str(Path(tmp) / "settings.json")
                with patch("web.settings_manager.SettingsManager", return_value=sm):
                    result = _get_resume_for_job(job, db)

        self.assertIsNotNone(result)
        self.assertEqual(result.id, mr_id)

    def test_get_resume_for_job_uses_domain_mapping(self):
        """Domain-specific resume is returned when mapping exists.

        We test by temporarily redirecting SettingsManager's SETTINGS_PATH to
        a file that has the domain mapping, which avoids patching complexity.
        """
        from scheduler.tasks import _get_resume_for_job

        with tempfile.TemporaryDirectory() as tmp:
            settings_path = str(Path(tmp) / "settings.json")

            with get_db() as db:
                active = MasterResume(
                    name="Active Resume", content={"skills": []},
                    is_active=True, is_sample=False,
                )
                domain_mr = MasterResume(
                    name="Marketing Resume", content={"skills": []},
                    is_active=False, is_sample=False,
                )
                db.add(active)
                db.add(domain_mr)
                db.commit()
                domain_mr_id = domain_mr.id
                active_id = active.id

                # Write domain mapping directly to the settings file used by
                # SettingsManager so the live instance reads the right mapping.
                import json as _json
                _json.dump({
                    "automation": {},
                    "resume_mode": "own",
                    "search_configs": [],
                    "domain_resumes": {"marketing": domain_mr_id},
                    "last_updated": None,
                }, open(settings_path, "w"))

                job = SimpleNamespace(domain="marketing", id=1)

                # Patch SETTINGS_PATH at class level so all instances use tmp file
                with patch.object(SettingsManager, "SETTINGS_PATH", settings_path):
                    result = _get_resume_for_job(job, db)

                self.assertIsNotNone(result)
                # Should use domain-mapped resume, not the active one
                self.assertNotEqual(result.id, active_id, "Should not fall back to active resume")
                self.assertEqual(result.id, domain_mr_id)


# ---------------------------------------------------------------------------
# Expanded keyword coverage tests
# ---------------------------------------------------------------------------

class TestExpandedKeywords(unittest.TestCase):
    """Verify that newly added keyword variants and tech terms detect correctly."""

    def setUp(self):
        self.d = DomainDetector()

    # -- software_engineering --------------------------------------------------

    def test_react_developer_title_is_software_engineering(self):
        result = self.d.detect_from_text(
            "Built performant web applications and REST APIs.",
            job_title="React Developer",
        )
        self.assertEqual(result["domain"], "software_engineering")

    def test_python_developer_title_is_software_engineering(self):
        result = self.d.detect_from_text(
            "Developed microservices and CI/CD pipelines using Docker.",
            job_title="Python Developer",
        )
        self.assertEqual(result["domain"], "software_engineering")

    def test_golang_developer_title_is_software_engineering(self):
        result = self.d.detect_from_text(
            "Designed and deployed scalable backend services.",
            job_title="Go Developer",
        )
        self.assertEqual(result["domain"], "software_engineering")

    def test_kubernetes_in_body_is_software_engineering(self):
        result = self.d.detect_from_text(
            "Managed Kubernetes clusters, Docker containers, and Terraform infrastructure.",
            job_title="Cloud Engineer",
        )
        self.assertEqual(result["domain"], "software_engineering")

    # -- ai_ml -----------------------------------------------------------------

    def test_pytorch_in_body_is_ai_ml(self):
        result = self.d.detect_from_text(
            "Trained PyTorch models, fine-tuned LLMs using HuggingFace, tracked experiments with MLflow.",
            job_title="ML Researcher",
        )
        self.assertEqual(result["domain"], "ai_ml")

    def test_prompt_engineer_title_is_ai_ml(self):
        result = self.d.detect_from_text(
            "Designed prompts and evaluated LLM outputs using embeddings.",
            job_title="Prompt Engineer",
        )
        self.assertEqual(result["domain"], "ai_ml")

    # -- data_analytics --------------------------------------------------------

    def test_tableau_in_body_is_data_analytics(self):
        result = self.d.detect_from_text(
            "Built Tableau dashboards, created dbt models on Snowflake, designed ETL pipelines.",
            job_title="Analytics Engineer",
        )
        self.assertEqual(result["domain"], "data_analytics")

    def test_power_bi_in_body_is_data_analytics(self):
        result = self.d.detect_from_text(
            "Developed Power BI reports and data warehouse models in Redshift.",
            job_title="Reporting Analyst",
        )
        self.assertEqual(result["domain"], "data_analytics")

    # -- marketing -------------------------------------------------------------

    def test_digital_strategist_title_is_marketing(self):
        result = self.d.detect_from_text(
            "Ran Google Ads, Facebook Ads, and HubSpot email campaigns for B2B SaaS.",
            job_title="Digital Strategist",
        )
        self.assertEqual(result["domain"], "marketing")

    def test_content_writer_title_is_marketing(self):
        result = self.d.detect_from_text(
            "Created SEO-driven content using Ahrefs and SEMrush keyword research.",
            job_title="Content Writer",
        )
        self.assertEqual(result["domain"], "marketing")

    # -- design ----------------------------------------------------------------

    def test_figma_in_body_is_design(self):
        result = self.d.detect_from_text(
            "Delivered wireframing, prototyping, and usability testing in Figma.",
            job_title="Product Designer",
        )
        self.assertEqual(result["domain"], "design")

    def test_adobe_xd_in_body_is_design(self):
        result = self.d.detect_from_text(
            "Produced high-fidelity mockups in Adobe XD following design thinking methodology.",
            job_title="UX Designer",
        )
        self.assertEqual(result["domain"], "design")

    # -- finance ---------------------------------------------------------------

    def test_dcf_in_body_is_finance(self):
        result = self.d.detect_from_text(
            "Built DCF valuation models and financial statements analysis in Bloomberg.",
            job_title="Finance Analyst",
        )
        self.assertEqual(result["domain"], "finance")

    def test_gaap_in_body_is_finance(self):
        result = self.d.detect_from_text(
            "Prepared GAAP and IFRS financial reports; managed QuickBooks for month-end close.",
            job_title="Accounting Manager",
        )
        self.assertEqual(result["domain"], "finance")

    # -- sales -----------------------------------------------------------------

    def test_salesforce_in_body_is_sales(self):
        result = self.d.detect_from_text(
            "Managed Salesforce CRM, Outreach sequences, and quota attainment for enterprise accounts.",
            job_title="Account Executive",
        )
        self.assertEqual(result["domain"], "sales")

    def test_inside_sales_title_is_sales(self):
        result = self.d.detect_from_text(
            "Conducted outbound prospecting and pipeline management using SalesLoft.",
            job_title="Inside Sales Representative",
        )
        self.assertEqual(result["domain"], "sales")

    # -- operations ------------------------------------------------------------

    def test_six_sigma_in_body_is_operations(self):
        result = self.d.detect_from_text(
            "Applied Six Sigma and Lean methodologies for cross-functional process improvement.",
            job_title="Process Manager",
        )
        self.assertEqual(result["domain"], "operations")

    def test_ops_manager_title_is_operations(self):
        result = self.d.detect_from_text(
            "Led vendor management, KPI tracking, and OKR setting across supply chain teams.",
            job_title="Ops Manager",
        )
        self.assertEqual(result["domain"], "operations")

    # -- product_management ----------------------------------------------------

    def test_user_stories_in_body_is_product_management(self):
        result = self.d.detect_from_text(
            "Wrote user stories, managed sprint ceremonies in Confluence, drove go-to-market strategy.",
            job_title="Senior Product Manager",
        )
        self.assertEqual(result["domain"], "product_management")

    def test_customer_discovery_in_body_is_product_management(self):
        result = self.d.detect_from_text(
            "Ran customer discovery sessions and A/B testing to inform OKRs and product roadmap.",
            job_title="Product Strategist",
        )
        self.assertEqual(result["domain"], "product_management")


# ---------------------------------------------------------------------------
# Multi-domain feature tests
# ---------------------------------------------------------------------------


class TestMultiDomainAPI(unittest.TestCase):
    """Tests for PATCH /api/resume/<id>/domain with multiple domains."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.client, self.sm = _make_flask_app(self.tmp)
        with get_db() as db:
            mr = MasterResume(
                name="Multi Domain Test", content={"skills": []},
                is_active=True, is_sample=False, domain="software_engineering",
            )
            db.add(mr)
            db.commit()
            self.resume_id = mr.id

    def tearDown(self):
        drop_tables()
        reset_manager(None)

    def test_patch_single_domain_sets_domain_and_domains(self):
        res = self.client.patch(
            f"/api/resume/{self.resume_id}/domain",
            data=json.dumps({"domain": "marketing"}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertEqual(data["domain"], "marketing")
        self.assertEqual(data["domains"], ["marketing"])

    def test_patch_multi_domain_sets_both_fields(self):
        res = self.client.patch(
            f"/api/resume/{self.resume_id}/domain",
            data=json.dumps({"domains": ["software_engineering", "ai_ml"]}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertEqual(data["domain"], "software_engineering")
        self.assertEqual(data["domains"], ["software_engineering", "ai_ml"])

    def test_patch_multi_domain_persisted_in_db(self):
        self.client.patch(
            f"/api/resume/{self.resume_id}/domain",
            data=json.dumps({"domains": ["finance", "sales"]}),
            content_type="application/json",
        )
        with get_db() as db:
            mr = db.query(MasterResume).filter(MasterResume.id == self.resume_id).first()
        self.assertEqual(mr.domain, "finance")
        self.assertEqual(mr.domains, ["finance", "sales"])

    def test_patch_empty_domains_list_returns_400(self):
        res = self.client.patch(
            f"/api/resume/{self.resume_id}/domain",
            data=json.dumps({"domains": []}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 400)

    def test_patch_invalid_domain_in_list_returns_400(self):
        res = self.client.patch(
            f"/api/resume/{self.resume_id}/domain",
            data=json.dumps({"domains": ["software_engineering", "space_cowboy"]}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 400)

    def test_patch_other_domain_in_list_returns_400(self):
        res = self.client.patch(
            f"/api/resume/{self.resume_id}/domain",
            data=json.dumps({"domains": ["marketing", "other"]}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 400)

    def test_active_context_returns_configs_for_all_domains(self):
        """active-context should return merged configs for all domains."""
        # Set two domains on the active resume
        self.client.patch(
            f"/api/resume/{self.resume_id}/domain",
            data=json.dumps({"domains": ["marketing", "sales"]}),
            content_type="application/json",
        )
        res = self.client.get("/api/active-context")
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        keywords = [c["keywords"] for c in data.get("industry_search_configs", [])]
        # marketing has 3 configs, sales has 3 configs → 6 total (no overlap)
        self.assertEqual(len(keywords), 6)
        self.assertIn("marketing manager", keywords)
        self.assertIn("account executive", keywords)


class TestMultiDomainSettingsManager(unittest.TestCase):
    """Tests for SettingsManager multi-domain helpers."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sm = make_sm(self.tmp)

    def test_get_industry_search_configs_for_domains_single(self):
        cfgs = self.sm.get_industry_search_configs_for_domains(["marketing"])
        self.assertEqual(len(cfgs), 3)
        keywords = [c["keywords"] for c in cfgs]
        self.assertIn("marketing manager", keywords)

    def test_get_industry_search_configs_for_domains_multi(self):
        cfgs = self.sm.get_industry_search_configs_for_domains(["marketing", "sales"])
        self.assertEqual(len(cfgs), 6)

    def test_get_industry_search_configs_for_domains_deduplicates(self):
        # Same domain twice — should not double-count
        cfgs = self.sm.get_industry_search_configs_for_domains(["marketing", "marketing"])
        self.assertEqual(len(cfgs), 3)

    def test_get_industry_search_configs_for_domains_empty(self):
        cfgs = self.sm.get_industry_search_configs_for_domains([])
        self.assertEqual(cfgs, [])

    def test_get_active_domains_returns_list(self):
        reset_manager(IN_MEMORY)
        create_tables()
        try:
            with get_db() as db:
                mr = MasterResume(
                    name="Test", content={}, is_active=True, is_sample=False,
                    domain="software_engineering",
                    domains=["software_engineering", "ai_ml"],
                )
                db.add(mr)
                db.commit()
            result = self.sm.get_active_domains()
            self.assertEqual(result, ["software_engineering", "ai_ml"])
        finally:
            drop_tables()
            reset_manager(None)

    def test_get_active_domains_falls_back_to_single_domain(self):
        reset_manager(IN_MEMORY)
        create_tables()
        try:
            with get_db() as db:
                mr = MasterResume(
                    name="Test", content={}, is_active=True, is_sample=False,
                    domain="finance", domains=None,
                )
                db.add(mr)
                db.commit()
            result = self.sm.get_active_domains()
            self.assertEqual(result, ["finance"])
        finally:
            drop_tables()
            reset_manager(None)

    def test_get_active_domains_excludes_other(self):
        reset_manager(IN_MEMORY)
        create_tables()
        try:
            with get_db() as db:
                mr = MasterResume(
                    name="Test", content={}, is_active=True, is_sample=False,
                    domain="other", domains=["other"],
                )
                db.add(mr)
                db.commit()
            result = self.sm.get_active_domains()
            self.assertEqual(result, [])
        finally:
            drop_tables()
            reset_manager(None)


if __name__ == "__main__":
    unittest.main()
