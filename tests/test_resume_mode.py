"""Tests for Resume Mode Selector feature.

Covers:
- SettingsManager.get/set_resume_mode (unit)
- MasterResume.is_sample column (model)
- is_sample seeding via migration
- Flask API: GET/PATCH /api/resume/mode
- Flask API: POST /api/resume/upload (with mocked parser)
- Dashboard demo banner logic
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.database import reset_manager, create_tables, drop_tables, get_db
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


def _seed_sample(db) -> MasterResume:
    r = MasterResume(
        name="Master Resume v1",
        content={"skills": ["Python", "Django", "SQL"]},
        is_active=True,
        is_sample=True,
    )
    db.add(r)
    db.commit()
    return r


def _seed_user(db, name="Bhargav Resume") -> MasterResume:
    r = MasterResume(
        name=name,
        content={"skills": ["Python", "React", "AWS", "Docker"]},
        is_active=False,
        is_sample=False,
    )
    db.add(r)
    db.commit()
    return r


# ---------------------------------------------------------------------------
# SettingsManager — resume_mode unit tests
# ---------------------------------------------------------------------------

class TestSettingsResumeMode(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sm  = make_sm(self.tmp)

    def test_settings_resume_mode_default(self):
        """Fresh settings file → resume_mode defaults to 'sample'."""
        data = self.sm.load()
        self.assertEqual(data.get("resume_mode"), "sample")

    def test_get_resume_mode_default(self):
        self.assertEqual(self.sm.get_resume_mode(), "sample")

    def test_set_resume_mode_own(self):
        result = self.sm.set_resume_mode("own")
        self.assertTrue(result)
        self.assertEqual(self.sm.get_resume_mode(), "own")

    def test_set_resume_mode_sample(self):
        self.sm.set_resume_mode("own")
        self.sm.set_resume_mode("sample")
        self.assertEqual(self.sm.get_resume_mode(), "sample")

    def test_set_resume_mode_invalid_raises(self):
        with self.assertRaises(ValueError):
            self.sm.set_resume_mode("hybrid")

    def test_set_resume_mode_empty_raises(self):
        with self.assertRaises(ValueError):
            self.sm.set_resume_mode("")

    def test_resume_mode_persists_across_instances(self):
        self.sm.set_resume_mode("own")
        sm2 = make_sm(self.tmp)
        sm2.SETTINGS_PATH = self.sm.SETTINGS_PATH
        self.assertEqual(sm2.get_resume_mode(), "own")

    def test_settings_merges_missing_resume_mode_from_old_file(self):
        """Old settings.json without resume_mode should get 'sample' default on load."""
        old_data = {"automation": {"scrape": {"mode": "manual", "schedule": "09:00"},
                                    "generate": {"mode": "manual", "schedule": "10:00"}},
                    "last_updated": None}
        Path(self.sm.SETTINGS_PATH).write_text(json.dumps(old_data), encoding="utf-8")
        data = self.sm.load()
        self.assertEqual(data.get("resume_mode"), "sample")


# ---------------------------------------------------------------------------
# MasterResume model — is_sample column
# ---------------------------------------------------------------------------

class TestMasterResumeIsSampleColumn(unittest.TestCase):

    def setUp(self):
        reset_manager(IN_MEMORY)
        create_tables()

    def tearDown(self):
        drop_tables()

    def test_master_resume_has_is_sample_column(self):
        """MasterResume model must have an is_sample mapped column."""
        import sqlalchemy
        cols = {c.key for c in sqlalchemy.inspect(MasterResume).mapper.column_attrs}
        self.assertIn("is_sample", cols)

    def test_is_sample_defaults_to_false(self):
        with get_db() as db:
            r = MasterResume(name="Test", content={"skills": []}, is_active=True)
            db.add(r)
            db.commit()
            db.refresh(r)
            self.assertFalse(r.is_sample)

    def test_is_sample_can_be_true(self):
        with get_db() as db:
            r = MasterResume(name="Sample", content={}, is_active=True, is_sample=True)
            db.add(r)
            db.commit()
            db.refresh(r)
            self.assertTrue(r.is_sample)

    def test_to_dict_includes_is_sample(self):
        with get_db() as db:
            r = MasterResume(name="Test", content={}, is_active=True, is_sample=False)
            db.add(r)
            db.commit()
            db.refresh(r)
            d = r.to_dict()
            self.assertIn("is_sample", d)


# ---------------------------------------------------------------------------
# Flask API tests
# ---------------------------------------------------------------------------

class TestResumeModeAPI(unittest.TestCase):

    def setUp(self):
        # In-memory DB
        reset_manager(IN_MEMORY)
        create_tables()

        # Temp settings
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
        drop_tables()

    # ── GET /api/resume/mode ──────────────────────────────────────

    def test_api_get_resume_mode_200(self):
        r = self.client.get("/api/resume/mode")
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn("mode", data)
        self.assertIn("sample_resume", data)
        self.assertIn("user_resume", data)

    def test_api_get_resume_mode_default_sample(self):
        r = self.client.get("/api/resume/mode")
        data = json.loads(r.data)
        self.assertEqual(data["mode"], "sample")

    def test_api_get_resume_mode_sample_resume_null_when_no_db_row(self):
        # Remove all sample resumes so DB has none
        with get_db() as db:
            db.query(MasterResume).filter(MasterResume.is_sample == True).delete()
            db.commit()
        r = self.client.get("/api/resume/mode")
        data = json.loads(r.data)
        self.assertIsNone(data["sample_resume"])
        self.assertIsNone(data["user_resume"])

    def test_api_get_resume_mode_returns_sample_resume_info(self):
        with get_db() as db:
            _seed_sample(db)
        r = self.client.get("/api/resume/mode")
        data = json.loads(r.data)
        self.assertIsNotNone(data["sample_resume"])
        self.assertEqual(data["sample_resume"]["name"], "Master Resume v1")
        self.assertTrue(data["sample_resume"]["is_sample"])
        self.assertEqual(data["sample_resume"]["skills_count"], 3)

    def test_api_get_resume_mode_returns_user_resume_info(self):
        with get_db() as db:
            _seed_user(db)
        r = self.client.get("/api/resume/mode")
        data = json.loads(r.data)
        self.assertIsNotNone(data["user_resume"])
        self.assertFalse(data["user_resume"]["is_sample"])

    # ── PATCH /api/resume/mode ────────────────────────────────────

    def test_api_patch_mode_invalid_value_400(self):
        r = self.client.patch(
            "/api/resume/mode",
            data=json.dumps({"mode": "hybrid"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)

    def test_api_patch_mode_missing_body_400(self):
        r = self.client.patch(
            "/api/resume/mode",
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)

    def test_api_patch_mode_to_sample_no_sample_resume_404(self):
        # Remove all sample resumes so the PATCH has nothing to activate
        with get_db() as db:
            db.query(MasterResume).filter(MasterResume.is_sample == True).delete()
            db.commit()
        r = self.client.patch(
            "/api/resume/mode",
            data=json.dumps({"mode": "sample"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 404)

    def test_api_patch_mode_to_own_no_user_resume_404(self):
        r = self.client.patch(
            "/api/resume/mode",
            data=json.dumps({"mode": "own"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 404)
        data = json.loads(r.data)
        self.assertEqual(data["error"], "no_user_resume")

    def test_api_patch_mode_to_sample_success(self):
        with get_db() as db:
            _seed_sample(db)
        r = self.client.patch(
            "/api/resume/mode",
            data=json.dumps({"mode": "sample"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data["status"], "switched")
        self.assertEqual(data["mode"], "sample")

    def test_api_patch_mode_to_sample_activates_sample_resume(self):
        with get_db() as db:
            _seed_sample(db)
            _seed_user(db)
            # Make user active first
            db.query(MasterResume).filter(MasterResume.is_sample == False).update({"is_active": True})
            db.query(MasterResume).filter(MasterResume.is_sample == True).update({"is_active": False})
            db.commit()

        self.client.patch(
            "/api/resume/mode",
            data=json.dumps({"mode": "sample"}),
            content_type="application/json",
        )

        with get_db() as db:
            sample = db.query(MasterResume).filter(MasterResume.is_sample == True).first()
            user   = db.query(MasterResume).filter(MasterResume.is_sample == False).first()
            self.assertTrue(sample.is_active)
            self.assertFalse(user.is_active)

    def test_api_patch_mode_to_own_success(self):
        with get_db() as db:
            _seed_sample(db)
            _seed_user(db)
        r = self.client.patch(
            "/api/resume/mode",
            data=json.dumps({"mode": "own"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data["mode"], "own")

    def test_api_patch_mode_to_own_activates_user_resume(self):
        with get_db() as db:
            _seed_sample(db)
            _seed_user(db)

        self.client.patch(
            "/api/resume/mode",
            data=json.dumps({"mode": "own"}),
            content_type="application/json",
        )

        with get_db() as db:
            sample = db.query(MasterResume).filter(MasterResume.is_sample == True).first()
            user   = db.query(MasterResume).filter(MasterResume.is_sample == False).first()
            self.assertFalse(sample.is_active)
            self.assertTrue(user.is_active)

    def test_switching_to_sample_deactivates_user_resume(self):
        with get_db() as db:
            _seed_sample(db)
            u = _seed_user(db)
            u.is_active = True
            db.commit()

        self.client.patch(
            "/api/resume/mode",
            data=json.dumps({"mode": "sample"}),
            content_type="application/json",
        )

        with get_db() as db:
            user = db.query(MasterResume).filter(MasterResume.is_sample == False).first()
            self.assertFalse(user.is_active)

    def test_api_patch_updates_settings_json(self):
        import web.app as app_module
        with get_db() as db:
            _seed_sample(db)
        self.client.patch(
            "/api/resume/mode",
            data=json.dumps({"mode": "sample"}),
            content_type="application/json",
        )
        self.assertEqual(app_module.settings_manager.get_resume_mode(), "sample")

    # ── POST /api/resume/upload ───────────────────────────────────

    def test_upload_no_file_400(self):
        r = self.client.post("/api/resume/upload")
        self.assertEqual(r.status_code, 400)

    def test_upload_unsupported_extension_400(self):
        """Unsupported file types (.exe, .rtf, etc.) return 400."""
        r = self.client.post(
            "/api/resume/upload",
            data={"file": (io.BytesIO(b"binary data"), "resume.exe")},
            content_type="multipart/form-data",
        )
        self.assertEqual(r.status_code, 400)

    def test_upload_valid_pdf_creates_master_resume(self):
        fake_content = {
            "personal_info": {"name": "Test User", "email": "t@t.com", "phone": "", "location": ""},
            "professional_summary": "Test summary",
            "skills": ["Python", "Django"],
            "work_experience": [],
            "education": [],
            "projects": [],
        }
        with patch("pdf_generator.pdf_parser.ResumePDFParser.parse", return_value=fake_content):
            r = self.client.post(
                "/api/resume/upload",
                data={
                    "file": (io.BytesIO(b"%PDF-1.4 fake"), "resume.pdf"),
                    "name": "Test Resume",
                },
                content_type="multipart/form-data",
            )
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertTrue(data["ok"])
        self.assertIn("id", data)
        self.assertEqual(data["name"], "Test Resume")

    def test_upload_creates_non_sample_resume(self):
        fake_content = {
            "personal_info": {"name": "Test User", "email": "", "phone": "", "location": ""},
            "skills": ["Python"],
            "work_experience": [], "education": [], "projects": [],
            "professional_summary": "",
        }
        with patch("pdf_generator.pdf_parser.ResumePDFParser.parse", return_value=fake_content):
            r = self.client.post(
                "/api/resume/upload",
                data={"file": (io.BytesIO(b"%PDF"), "r.pdf")},
                content_type="multipart/form-data",
            )
        self.assertEqual(r.status_code, 200)
        rid = json.loads(r.data)["id"]
        with get_db() as db:
            mr = db.query(MasterResume).filter(MasterResume.id == rid).first()
            self.assertFalse(mr.is_sample)
            self.assertTrue(mr.is_active)

    def test_upload_auto_switches_to_own_mode(self):
        import web.app as app_module
        fake_content = {
            "personal_info": {"name": "Jane", "email": "", "phone": "", "location": ""},
            "skills": ["Python"], "work_experience": [{"title": "Dev", "company": "Co", "bullets": []}],
            "education": [], "projects": [],
            "professional_summary": "",
        }
        with patch("pdf_generator.pdf_parser.ResumePDFParser.parse", return_value=fake_content):
            self.client.post(
                "/api/resume/upload",
                data={"file": (io.BytesIO(b"%PDF"), "jane.pdf")},
                content_type="multipart/form-data",
            )
        self.assertEqual(app_module.settings_manager.get_resume_mode(), "own")

    def test_upload_deactivates_existing_resumes(self):
        with get_db() as db:
            _seed_sample(db)  # is_active = True by default
        fake_content = {
            "personal_info": {"name": "New", "email": "", "phone": "", "location": ""},
            "skills": ["Python"], "work_experience": [{"title": "Dev", "company": "Co", "bullets": []}],
            "education": [], "projects": [],
            "professional_summary": "",
        }
        with patch("pdf_generator.pdf_parser.ResumePDFParser.parse", return_value=fake_content):
            r = self.client.post(
                "/api/resume/upload",
                data={"file": (io.BytesIO(b"%PDF"), "new.pdf")},
                content_type="multipart/form-data",
            )
        self.assertEqual(r.status_code, 200)
        with get_db() as db:
            # Only the new one should be active
            active = db.query(MasterResume).filter(MasterResume.is_active == True).all()
            self.assertEqual(len(active), 1)
            self.assertFalse(active[0].is_sample)

    def test_upload_parse_error_returns_422(self):
        with patch("pdf_generator.pdf_parser.ResumePDFParser.parse",
                   side_effect=ValueError("Cannot extract text")):
            r = self.client.post(
                "/api/resume/upload",
                data={"file": (io.BytesIO(b"garbage"), "bad.pdf")},
                content_type="multipart/form-data",
            )
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()
