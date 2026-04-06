"""End-to-end pipeline verification tests.

Covers:
  TestResumeDetection        — heuristic + NIM classifier (accept/reject)
  TestTailoringPipeline      — modify_resume() with realistic resume content
  TestScoreBreakdownStorage  — score_breakdown stored + returned by API
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.database import create_tables, drop_tables, get_db, reset_manager
from database.models import Job, MasterResume, TailoredResume
from pdf_generator.pdf_parser import ResumeClassifier

IN_MEMORY = "sqlite:///:memory:"

# ---------------------------------------------------------------------------
# Sample texts
# ---------------------------------------------------------------------------

RESUME_TEXT = """
Alex Rivera
alex@example.com | (555) 123-4567 | New York, NY

PROFESSIONAL SUMMARY
Senior software engineer with 6 years of Python and cloud experience.

TECHNICAL SKILLS
Python, Django, FastAPI, PostgreSQL, Redis, Docker, Kubernetes, AWS, Git, CI/CD

WORK EXPERIENCE
Senior Software Engineer — TechCorp (2021–Present)
• Built microservices handling 2M requests/day using FastAPI and Docker
• Reduced API latency by 40% through Redis caching
• Led migration to AWS EKS, saving $50k/year in infrastructure costs

Software Engineer — StartupXYZ (2018–2021)
• Developed REST APIs serving 500K+ mobile users
• Implemented CI/CD pipelines with GitHub Actions

EDUCATION
B.S. Computer Science — MIT, 2018

CERTIFICATIONS
AWS Certified Solutions Architect
Docker Certified Associate
"""

INVOICE_TEXT = """
INVOICE #12345
Bill To: Acme Corporation
Payment Due: April 30, 2026

Item           Qty    Unit Price    Total
Widget A        10      $25.00     $250.00
Widget B         5      $50.00     $250.00

Subtotal: $500.00
Tax Invoice Amount: $540.00
Total Amount Due: $540.00
"""

RESEARCH_TEXT = """
Abstract
This paper presents a novel approach to machine learning optimization.

1. Introduction
Recent advances in deep learning have shown promising results.

2. Methodology
We propose a new gradient-descent algorithm using adaptive learning rates.

3. Results and Discussion
Our findings demonstrate a 15% improvement over baseline.

References
[1] LeCun et al. (1998). Gradient-based learning. DOI: 10.1109/5.726791
"""

MARKETING_TEXT = """
Sofia Martinez
sofia@email.com | (555) 987-6543 | Los Angeles, CA

PROFESSIONAL SUMMARY
Digital marketing specialist with 5 years of SEO and SEM experience.

SKILLS
SEO, HubSpot, Google Ads, Salesforce, Content Marketing, Email Campaigns

WORK EXPERIENCE
Marketing Manager — BrandCo (2020–Present)
• Grew organic traffic by 200% via SEO optimisation
• Managed $500K annual Google Ads budget

EDUCATION
B.A. Marketing — UCLA, 2019
"""


# ---------------------------------------------------------------------------
# Tests: Resume detection
# ---------------------------------------------------------------------------


class TestResumeDetection(unittest.TestCase):

    def setUp(self) -> None:
        self.classifier = ResumeClassifier()

    def test_standard_resume_accepted_by_heuristic_alone(self) -> None:
        """Clear resume with multiple headers passes without NIM call."""
        with patch.object(self.classifier, "classify_with_nvidia") as mock_nim:
            result = self.classifier.classify(RESUME_TEXT)
            mock_nim.assert_not_called()
        self.assertEqual(result["verdict"], "resume")
        self.assertGreaterEqual(result["confidence"], 0.70)

    def test_invoice_rejected_by_heuristic_alone(self) -> None:
        """Invoice with billing signals rejected without NIM call."""
        with patch.object(self.classifier, "classify_with_nvidia") as mock_nim:
            result = self.classifier.classify(INVOICE_TEXT)
            mock_nim.assert_not_called()
        self.assertEqual(result["verdict"], "not_resume")

    def test_research_paper_rejected_by_heuristic_alone(self) -> None:
        """Academic paper with abstract/doi/bibliography → not_resume."""
        with patch.object(self.classifier, "classify_with_nvidia") as mock_nim:
            result = self.classifier.classify(RESEARCH_TEXT)
            mock_nim.assert_not_called()
        self.assertEqual(result["verdict"], "not_resume")

    def test_marketing_resume_accepted_by_heuristic(self) -> None:
        """Marketing-domain resume with standard headers should pass."""
        result = self.classifier.classify(MARKETING_TEXT)
        self.assertEqual(result["verdict"], "resume")

    def test_minimal_resume_escalates_to_nim(self) -> None:
        """Resume with partial signals (inconclusive heuristic) escalates to NIM."""
        # Confidence = 0.15 (email) + 0.15 (experience header) + 0.15 (skills header)
        # = 0.45 → inconclusive (between 0.30 and 0.70)
        minimal = "Jane Smith\njane@test.com\n\nExperience: 3 years Python\nSkills: SQL, Docker"
        nim_result = {
            "verdict": "resume", "confidence": 0.85,
            "document_type": "resume", "signals_found": [],
            "reason": "Contains contact info and skills", "method": "nvidia",
        }
        with patch.object(self.classifier, "classify_with_nvidia",
                          return_value=nim_result) as mock_nim:
            result = self.classifier.classify(minimal)
            mock_nim.assert_called_once()
        self.assertEqual(result["verdict"], "resume")

    def test_nim_accepted_resume_overrides_heuristic_inconclusive(self) -> None:
        """When heuristic is inconclusive and NIM says resume, result is resume."""
        inconclusive_h = {
            "verdict": "inconclusive", "confidence": 0.45,
            "signals_found": [], "document_type": "unknown",
            "reason": "", "method": "heuristic",
        }
        nim_ok = {
            "verdict": "resume", "confidence": 0.80,
            "document_type": "resume", "signals_found": [],
            "reason": "Has work experience and skills", "method": "nvidia",
        }
        with patch.object(self.classifier, "classify_heuristic", return_value=inconclusive_h):
            with patch.object(self.classifier, "classify_with_nvidia", return_value=nim_ok):
                result = self.classifier.classify("some ambiguous text")
        self.assertEqual(result["verdict"], "resume")

    def test_empty_text_never_crashes(self) -> None:
        """Empty string should return a valid verdict without exception."""
        result = self.classifier.classify("")
        self.assertIn(result["verdict"], ("not_resume", "inconclusive"))

    def test_verdict_always_has_required_keys(self) -> None:
        """classify() always returns the required keys regardless of verdict."""
        for text in (RESUME_TEXT, INVOICE_TEXT, RESEARCH_TEXT, ""):
            result = self.classifier.classify(text)
            for key in ("verdict", "confidence", "document_type",
                        "signals_found", "reason", "method"):
                self.assertIn(key, result, f"Missing key {key!r} for text={text[:20]!r}")


# ---------------------------------------------------------------------------
# Tests: Full tailoring pipeline (mocked NIM)
# ---------------------------------------------------------------------------


class TestTailoringPipeline(unittest.TestCase):

    def setUp(self) -> None:
        reset_manager(IN_MEMORY)
        create_tables()

    def tearDown(self) -> None:
        drop_tables()
        reset_manager(None)

    def _seed_resume(self, db) -> MasterResume:
        content = {
            "personal_info": {"name": "Alex Rivera", "email": "alex@test.com",
                              "phone": "555-1234", "location": "NYC"},
            "professional_summary": "Senior Python developer with 6 years experience.",
            "skills": ["Python", "Django", "PostgreSQL", "AWS", "Docker",
                       "REST APIs", "Git", "Redis", "Kubernetes", "CI/CD"],
            "work_experience": [{
                "title": "Senior Software Engineer",
                "company": "TechCorp",
                "location": "NYC",
                "start_date": "2021",
                "end_date": "Present",
                "bullets": [
                    "Built microservices handling 2M requests/day",
                    "Reduced API latency by 40% through Redis caching",
                    "Led team of 5 engineers on AWS EKS migration",
                ],
            }],
            "education": [{"degree": "B.S. Computer Science",
                           "institution": "MIT", "graduation_year": "2018", "gpa": ""}],
            "projects": [],
        }
        style = {
            "voice": "no_pronouns",
            "sentence_structure": {"style": "punchy"},
            "metric_usage": {"density": "moderate"},
            "format": {"bullet_char": "•", "capitalization": "sentence", "trailing_period": False},
            "structure": ["professional_summary", "skills", "work_experience", "education"],
        }
        mr = MasterResume(
            name="Alex Resume", content=content, is_active=True, is_sample=False,
            domain="software_engineering", style_fingerprint=style,
        )
        db.add(mr)
        db.commit()
        return mr

    def _seed_job(self, db) -> Job:
        job = Job(
            job_title="Backend Engineer",
            company_name="Startup Inc",
            job_description=(
                "We need a senior Python/Django developer with AWS and Docker experience. "
                "Must have PostgreSQL and REST API skills. Redis and Kubernetes a plus."
            ),
            application_url="https://example.com/job/1",
            source="linkedin",
            status="analyzed",
            domain="software_engineering",
            required_skills=["Python", "Django", "AWS", "Docker", "PostgreSQL", "REST APIs"],
            preferred_skills=["Redis", "Kubernetes"],
        )
        db.add(job)
        db.commit()
        return job

    def _make_mocked_rewriter(self):
        """Build a GeminiRewriter instance with _call_nvidia mocked."""
        from resume_engine.gemini_rewriter import GeminiRewriter
        from resume_engine.rate_limiter import RateLimiter
        rw = GeminiRewriter.__new__(GeminiRewriter)
        limiter = RateLimiter(
            rpm=100, rpd=5_000,
            usage_file=Path(tempfile.mktemp(suffix=".json")),
        )
        rw.model_id = "nvidia/llama-3.3-nemotron-super-49b-v1"
        rw._limiter = limiter
        rw._nvidia_limiter = limiter
        rw._max_retries = 1
        rw.api_call_count = 0

        # Side-effect increments api_call_count like the real method does
        def _mock_call(prompt):
            rw.api_call_count += 1
            return "Developed scalable Python and Django APIs deployed on AWS."

        rw._call_nvidia = _mock_call
        return rw

    def test_tailoring_produces_non_empty_bullets(self) -> None:
        """modify_resume with a real resume produces tailored bullets."""
        from resume_engine.modifier import ResumeModifier

        with get_db() as db:
            resume = self._seed_resume(db)
            job = self._seed_job(db)
            r_id, j_id = resume.id, job.id

        with get_db() as db:
            resume = db.query(MasterResume).get(r_id)
            job = db.query(Job).get(j_id)
            modifier = ResumeModifier(rewriter=self._make_mocked_rewriter())
            result = modifier.modify_resume(
                resume, job, style_fingerprint=resume.style_fingerprint
            )

        experience = result.content.get("work_experience", [])
        self.assertGreater(len(experience), 0, "No work experience in tailored resume")
        all_bullets = [b for exp in experience for b in exp.get("bullets", [])]
        self.assertGreater(len(all_bullets), 0, "No bullets in tailored work experience")

    def test_tailoring_preserves_skills_list(self) -> None:
        """Skills section must be non-empty after tailoring."""
        from resume_engine.modifier import ResumeModifier

        with get_db() as db:
            resume = self._seed_resume(db)
            job = self._seed_job(db)
            r_id, j_id = resume.id, job.id

        with get_db() as db:
            resume = db.query(MasterResume).get(r_id)
            job = db.query(Job).get(j_id)
            modifier = ResumeModifier(rewriter=self._make_mocked_rewriter())
            result = modifier.modify_resume(resume, job)

        self.assertIsInstance(result.content.get("skills"), list)
        self.assertGreater(len(result.content["skills"]), 0)

    def test_tailoring_api_calls_increment(self) -> None:
        """api_calls_used should be > 0 when content exists."""
        from resume_engine.modifier import ResumeModifier

        with get_db() as db:
            resume = self._seed_resume(db)
            job = self._seed_job(db)
            r_id, j_id = resume.id, job.id

        with get_db() as db:
            resume = db.query(MasterResume).get(r_id)
            job = db.query(Job).get(j_id)
            modifier = ResumeModifier(rewriter=self._make_mocked_rewriter())
            result = modifier.modify_resume(resume, job)

        self.assertGreater(result.api_calls_used, 0,
            "Expected api_calls_used > 0 — NIM rewriter mock should have been called")

    def test_empty_resume_content_produces_zero_api_calls(self) -> None:
        """Resume with no bullets/summary should produce 0 NIM calls."""
        from resume_engine.modifier import ResumeModifier

        with get_db() as db:
            empty_resume = MasterResume(
                name="Empty", content={
                    "personal_info": {"name": "Nobody"},
                    "professional_summary": "",
                    "skills": [],
                    "work_experience": [],
                    "education": [],
                    "projects": [],
                },
                is_active=True, is_sample=False, domain="other",
            )
            db.add(empty_resume)
            job = self._seed_job(db)
            db.commit()
            r_id, j_id = empty_resume.id, job.id

        with get_db() as db:
            resume = db.query(MasterResume).get(r_id)
            job = db.query(Job).get(j_id)
            rw = self._make_mocked_rewriter()
            modifier = ResumeModifier(rewriter=rw)
            result = modifier.modify_resume(resume, job)

        # Nothing to rewrite → NIM never called
        self.assertEqual(result.api_calls_used, 0)


# ---------------------------------------------------------------------------
# Tests: score_breakdown stored and returned correctly
# ---------------------------------------------------------------------------


class TestScoreBreakdownStorage(unittest.TestCase):

    def setUp(self) -> None:
        reset_manager(IN_MEMORY)
        create_tables()
        import os, tempfile as _tmp
        self.tmp = _tmp.mkdtemp()
        os.environ.setdefault("NVIDIA_API_KEY", "test-key")

        from web.settings_manager import SettingsManager
        import web.app as app_module
        sm = SettingsManager()
        sm.SETTINGS_PATH = str(Path(self.tmp) / "settings.json")
        self._orig_sm = app_module.settings_manager
        app_module.settings_manager = sm
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()

    def tearDown(self) -> None:
        import web.app as app_module
        app_module.settings_manager = self._orig_sm
        drop_tables()
        reset_manager(None)

    def _seed_resume_and_job(self):
        with get_db() as db:
            mr = MasterResume(
                name="Test SE Resume",
                content={
                    "personal_info": {"name": "Alex", "email": "", "phone": "", "location": ""},
                    "professional_summary": "Python developer.",
                    "skills": ["Python", "Django", "AWS", "Docker"],
                    "work_experience": [{
                        "title": "Engineer", "company": "Corp",
                        "location": "", "start_date": "2020", "end_date": "Present",
                        "bullets": ["Built REST APIs", "Deployed on AWS"],
                    }],
                    "education": [], "projects": [],
                },
                is_active=True, is_sample=False, domain="software_engineering",
                style_fingerprint={
                    "voice": "no_pronouns",
                    "sentence_structure": {"style": "moderate"},
                    "metric_usage": {"density": "light"},
                    "format": {},
                },
            )
            db.add(mr)
            job = Job(
                job_title="Backend Dev", company_name="Co",
                job_description="Python Django AWS developer needed.",
                application_url="https://co.com/job/99", source="linkedin",
                status="analyzed", domain="software_engineering",
                required_skills=["Python", "Django", "AWS", "PostgreSQL"],
                preferred_skills=["Docker", "Redis"],
            )
            db.add(job)
            db.commit()
            return mr.id, job.id

    def test_generate_resume_api_stores_score_breakdown(self) -> None:
        """POST /api/generate-resume stores score_breakdown in TailoredResume."""
        from resume_engine.modifier import ResumeModifier, ModificationResult

        resume_id, job_id = self._seed_resume_and_job()

        fake_result = ModificationResult(
            content={
                "personal_info": {"name": "Alex"},
                "professional_summary": "Rewritten summary.",
                "skills": ["Python", "Django", "AWS"],
                "work_experience": [{"title": "Eng", "company": "Co",
                                      "location": "", "start_date": "2020",
                                      "end_date": "Present",
                                      "bullets": ["Built scalable APIs."]}],
                "education": [], "projects": [],
            },
            metrics={},
            modification_log=[],
            validation_report={},
            api_calls_used=3,
        )

        with patch("resume_engine.modifier.ResumeModifier.modify_resume",
                   return_value=fake_result):
            r = self.client.post(
                "/api/generate-resume",
                json={"job_id": job_id},
                content_type="application/json",
            )

        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data["ok"])

        # Verify score_breakdown was stored in DB
        with get_db() as db:
            tr = db.query(TailoredResume).filter(TailoredResume.job_id == job_id).first()
            self.assertIsNotNone(tr)
            self.assertIsNotNone(tr.score_breakdown,
                "score_breakdown should be stored in TailoredResume")
            bd = tr.score_breakdown
            self.assertIn("required_skills", bd)
            self.assertIn("preferred_skills", bd)
            self.assertIn("experience", bd)
            self.assertIn("education", bd)
            self.assertIn("bonus", bd)

    def test_job_detail_returns_score_breakdown(self) -> None:
        """GET /api/jobs/<id> returns non-null score_breakdown after generation."""
        from resume_engine.modifier import ResumeModifier, ModificationResult

        resume_id, job_id = self._seed_resume_and_job()

        fake_result = ModificationResult(
            content={
                "personal_info": {"name": "Alex"},
                "professional_summary": "Summary.",
                "skills": ["Python"],
                "work_experience": [], "education": [], "projects": [],
            },
            metrics={},
            modification_log=[],
            validation_report={},
            api_calls_used=1,
        )

        with patch("resume_engine.modifier.ResumeModifier.modify_resume",
                   return_value=fake_result):
            self.client.post("/api/generate-resume", json={"job_id": job_id},
                             content_type="application/json")

        r = self.client.get(f"/api/jobs/{job_id}")
        self.assertEqual(r.status_code, 200)
        detail = r.get_json()

        bd = detail.get("score_breakdown")
        self.assertIsNotNone(bd, "score_breakdown should be non-null in job detail")

        # Verify structure
        self.assertIn("required_skills", bd)
        self.assertIsInstance(bd["required_skills"]["matched"], int)
        self.assertIsInstance(bd["required_skills"]["total"], int)
        self.assertIsInstance(bd["required_skills"]["score"], (int, float))

    def test_score_breakdown_reflects_actual_skill_match(self) -> None:
        """score_breakdown.required_skills.matched reflects actual overlap."""
        from resume_engine.modifier import ResumeModifier, ModificationResult
        from analyzer.scoring import ScoringEngine

        resume_id, job_id = self._seed_resume_and_job()

        # Get what the engine would compute before mocking the modifier
        with get_db() as db:
            mr = db.query(MasterResume).get(resume_id)
            job = db.query(Job).get(job_id)
            engine = ScoringEngine()
            score_result = engine.score(job, mr)
            expected_matched = score_result.score_breakdown["required_skills"]["matched"]
            expected_total = score_result.score_breakdown["required_skills"]["total"]

        fake_result = ModificationResult(
            content={"personal_info": {}, "professional_summary": "",
                     "skills": ["Python"], "work_experience": [], "education": [], "projects": []},
            metrics={},
            modification_log=[],
            validation_report={},
            api_calls_used=0,
        )

        with patch("resume_engine.modifier.ResumeModifier.modify_resume",
                   return_value=fake_result):
            self.client.post("/api/generate-resume", json={"job_id": job_id},
                             content_type="application/json")

        r = self.client.get(f"/api/jobs/{job_id}")
        bd = r.get_json()["score_breakdown"]
        self.assertEqual(bd["required_skills"]["matched"], expected_matched)
        self.assertEqual(bd["required_skills"]["total"], expected_total)


if __name__ == "__main__":
    unittest.main()
