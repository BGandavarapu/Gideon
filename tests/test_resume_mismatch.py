"""Tests for the resume mismatch feature.

Covers:
- analyzed_with_resume_id column exists on Job
- Job.to_dict() includes the field
- Generation is allowed when analyzed_with_resume_id is None (old jobs)
- Generation returns 409 when active resume differs from analyzed_with
- Generation allowed when active resume matches analyzed_with
- Reanalyze route sets analyzed_with_resume_id correctly
- Reanalyze does not delete existing tailored resumes
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.database import create_tables, drop_tables, get_db, reset_manager
from database.models import Job, MasterResume, TailoredResume
from datetime import datetime, timezone

IN_MEMORY = "sqlite:///:memory:"


def _make_client(tmp_dir: str):
    """Create an isolated Flask test client with in-memory DB."""
    reset_manager(IN_MEMORY)
    create_tables()
    os.environ["NVIDIA_API_KEY"] = "test-key"
    import web.app as app_module
    importlib.reload(app_module)
    app_module.settings_manager.SETTINGS_PATH = str(Path(tmp_dir) / "settings.json")
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def _make_job(db, **kwargs) -> Job:
    defaults = dict(
        job_title="Software Engineer",
        company_name="ACME",
        job_description="Python Django REST API microservices",
        application_url=f"https://example.com/job/{id(kwargs)}",
        source="linkedin",
        status="analyzed",
    )
    defaults.update(kwargs)
    j = Job(**defaults)
    db.add(j)
    db.flush()
    return j


def _make_resume(db, name="Test Resume", active=False, domain="software_engineering") -> MasterResume:
    mr = MasterResume(
        name=name,
        content={"skills": ["Python"], "work_experience": []},
        is_active=active,
        is_sample=False,
        domain=domain,
    )
    db.add(mr)
    db.flush()
    return mr


class TestAnalyzedWithColumn(unittest.TestCase):
    """Schema and model-level tests."""

    def setUp(self):
        reset_manager(IN_MEMORY)
        create_tables()

    def tearDown(self):
        drop_tables()
        reset_manager(None)

    def test_column_exists_in_jobs_table(self):
        """PRAGMA table_info should list analyzed_with_resume_id."""
        with get_db() as db:
            rows = db.execute(
                __import__("sqlalchemy").text("PRAGMA table_info(jobs)")
            ).fetchall()
        col_names = [r[1] for r in rows]
        self.assertIn("analyzed_with_resume_id", col_names)

    def test_to_dict_includes_field(self):
        """Job.to_dict() must include analyzed_with_resume_id."""
        with get_db() as db:
            job = _make_job(db)
            db.commit()
            d = job.to_dict()
        self.assertIn("analyzed_with_resume_id", d)
        self.assertIsNone(d["analyzed_with_resume_id"])

    def test_to_dict_reflects_set_value(self):
        """to_dict() returns the stored resume id when set."""
        with get_db() as db:
            mr = _make_resume(db, active=True)
            job = _make_job(db, analyzed_with_resume_id=mr.id)
            db.commit()
            d = job.to_dict()
        self.assertEqual(d["analyzed_with_resume_id"], mr.id)


class TestGenerateMismatchGuard(unittest.TestCase):
    """API-level tests for POST /api/generate-resume."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.client = _make_client(self.tmp)

    def tearDown(self):
        drop_tables()
        reset_manager(None)

    def test_generate_allowed_when_analyzed_id_is_none(self):
        """Old jobs (analyzed_with_resume_id=None) must never be blocked."""
        with get_db() as db:
            mr = _make_resume(db, active=True)
            job = _make_job(db, analyzed_with_resume_id=None)
            db.commit()
            job_id = job.id

        mock_score = MagicMock()
        mock_score.total_score = 75.0
        mock_score.score_breakdown = {}

        mock_tailored = MagicMock()
        mock_tailored.content = {"skills": []}

        with patch("analyzer.scoring.ScoringEngine.score", return_value=mock_score), \
             patch("resume_engine.modifier.ResumeModifier.modify_resume", return_value=mock_tailored):
            res = self.client.post(
                "/api/generate-resume",
                data=json.dumps({"job_id": job_id}),
                content_type="application/json",
            )
        self.assertNotEqual(res.status_code, 409)

    def test_generate_409_when_resume_mismatch(self):
        """Generation must return 409 when analyzed_with != active resume."""
        with get_db() as db:
            resume_a = _make_resume(db, name="Old Resume", active=False, domain="marketing")
            resume_b = _make_resume(db, name="New Resume", active=True, domain="software_engineering")
            job = _make_job(db, analyzed_with_resume_id=resume_a.id)
            db.commit()
            job_id = job.id

        res = self.client.post(
            "/api/generate-resume",
            data=json.dumps({"job_id": job_id}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 409)
        data = json.loads(res.data)
        self.assertEqual(data["error"], "resume_mismatch")
        self.assertIn("analyzed_with_resume_name", data)
        self.assertIn("active_resume_name", data)
        self.assertEqual(data["analyzed_with_resume_name"], "Old Resume")
        self.assertEqual(data["active_resume_name"], "New Resume")

    def test_generate_allowed_when_ids_match(self):
        """No 409 when analyzed_with == active resume."""
        with get_db() as db:
            mr = _make_resume(db, name="Matching Resume", active=True)
            job = _make_job(db, analyzed_with_resume_id=mr.id)
            db.commit()
            job_id = job.id

        mock_score = MagicMock()
        mock_score.total_score = 80.0
        mock_score.score_breakdown = {}

        mock_tailored = MagicMock()
        mock_tailored.content = {"skills": []}

        with patch("analyzer.scoring.ScoringEngine.score", return_value=mock_score), \
             patch("resume_engine.modifier.ResumeModifier.modify_resume", return_value=mock_tailored):
            res = self.client.post(
                "/api/generate-resume",
                data=json.dumps({"job_id": job_id}),
                content_type="application/json",
            )
        self.assertNotEqual(res.status_code, 409)


class TestReanalyzeEndpoint(unittest.TestCase):
    """Tests for POST /api/jobs/<id>/reanalyze."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.client = _make_client(self.tmp)

    def tearDown(self):
        drop_tables()
        reset_manager(None)

    def test_reanalyze_sets_analyzed_with_resume_id(self):
        """After reanalyze, job.analyzed_with_resume_id == active resume."""
        with get_db() as db:
            mr = _make_resume(db, name="Active Resume", active=True)
            job = _make_job(db, analyzed_with_resume_id=None)
            db.commit()
            job_id, mr_id = job.id, mr.id

        mock_score = MagicMock()
        mock_score.total_score = 65.0

        with patch("analyzer.keyword_extractor.KeywordExtractor.extract_by_category",
                   return_value={"programming_languages": ["Python"], "soft_skills": ["teamwork"]}), \
             patch("analyzer.scoring.ScoringEngine.score", return_value=mock_score):
            res = self.client.post(f"/api/jobs/{job_id}/reanalyze")

        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["analyzed_with_resume_id"], mr_id)

        # Confirm persisted in DB
        with get_db() as db:
            updated = db.query(Job).filter(Job.id == job_id).first()
        self.assertEqual(updated.analyzed_with_resume_id, mr_id)

    def test_reanalyze_does_not_delete_tailored_resumes(self):
        """Existing tailored resumes survive reanalysis."""
        with get_db() as db:
            mr = _make_resume(db, name="Active Resume", active=True)
            job = _make_job(db, analyzed_with_resume_id=None)
            db.flush()
            tr = TailoredResume(
                job_id=job.id,
                master_resume_id=mr.id,
                tailored_content={"skills": []},
                match_score=70.0,
                generated_at=datetime.now(timezone.utc),
            )
            db.add(tr)
            db.commit()
            job_id = job.id

        mock_score = MagicMock()
        mock_score.total_score = 70.0

        with patch("analyzer.keyword_extractor.KeywordExtractor.extract_by_category",
                   return_value={"programming_languages": ["Python"], "soft_skills": []}), \
             patch("analyzer.scoring.ScoringEngine.score", return_value=mock_score):
            self.client.post(f"/api/jobs/{job_id}/reanalyze")

        with get_db() as db:
            count = db.query(TailoredResume).filter(TailoredResume.job_id == job_id).count()
        self.assertEqual(count, 1)

    def test_reanalyze_clears_mismatch(self):
        """After reanalyze, a subsequent generate call is no longer blocked."""
        with get_db() as db:
            resume_a = _make_resume(db, name="Old Resume", active=False, domain="marketing")
            resume_b = _make_resume(db, name="New Resume", active=True, domain="software_engineering")
            job = _make_job(db, analyzed_with_resume_id=resume_a.id)
            db.commit()
            job_id, rb_id = job.id, resume_b.id

        mock_score = MagicMock()
        mock_score.total_score = 72.0
        mock_score.score_breakdown = {}

        mock_tailored = MagicMock()
        mock_tailored.content = {"skills": []}

        with patch("analyzer.keyword_extractor.KeywordExtractor.extract_by_category",
                   return_value={"programming_languages": ["Python"], "soft_skills": []}), \
             patch("analyzer.scoring.ScoringEngine.score", return_value=mock_score):
            self.client.post(f"/api/jobs/{job_id}/reanalyze")

        # Now generate should not be blocked
        with patch("analyzer.scoring.ScoringEngine.score", return_value=mock_score), \
             patch("resume_engine.modifier.ResumeModifier.modify_resume", return_value=mock_tailored):
            res = self.client.post(
                "/api/generate-resume",
                data=json.dumps({"job_id": job_id}),
                content_type="application/json",
            )
        self.assertNotEqual(res.status_code, 409)

    def test_job_detail_includes_analyzed_with_fields(self):
        """GET /api/jobs/<id> returns analyzed_with_resume_id and name."""
        with get_db() as db:
            mr = _make_resume(db, name="Analyzer Resume", active=True)
            job = _make_job(db, analyzed_with_resume_id=mr.id)
            db.commit()
            job_id = job.id

        res = self.client.get(f"/api/jobs/{job_id}")
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertIn("analyzed_with_resume_id", data)
        self.assertIn("analyzed_with_resume_name", data)
        self.assertEqual(data["analyzed_with_resume_name"], "Analyzer Resume")


if __name__ == "__main__":
    unittest.main()
