"""Tests for StyleExtractor and style-aware NIM rewriting.

Covers:
- StyleExtractor.extract() — all dimension detectors
- Rewriter.rewrite_bullet_point() — style constraint injection
- ResumeModifier._enforce_structure_order()
- Flask upload route stores and returns style fingerprint
- Flask generate-resume route passes style_fingerprint to modifier
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import os
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Sample fixtures
# ---------------------------------------------------------------------------

SAMPLE_PUNCHY_RESUME = {
    "professional_summary": "Engineer with 5 years building scalable systems.",
    "work_experience": [
        {
            "title": "SWE",
            "company": "TechCorp",
            "bullets": [
                "Built REST APIs serving 1M+ requests daily",
                "Reduced latency by 40% via caching",
                "Led team of 5 engineers",
                "Shipped 3 major features per quarter",
                "Cut infrastructure costs by $50K annually",
            ],
        }
    ],
    "skills": ["Python", "AWS"],
    "education": [{"degree": "B.S. CS", "institution": "UC Berkeley"}],
    "certifications": [],
    "projects": [],
}

SAMPLE_FIRST_PERSON_RESUME = {
    "professional_summary": "I am a software engineer with 5 years experience.",
    "work_experience": [
        {
            "title": "SWE",
            "company": "Corp",
            "bullets": [
                "I built the authentication system",
                "I led a team of 3 developers",
                "I reduced costs by 20%",
            ],
        }
    ],
    "skills": ["Python"],
    "education": [],
    "certifications": [],
    "projects": [],
}

SAMPLE_DETAILED_RESUME = {
    "professional_summary": "Experienced senior engineer.",
    "work_experience": [
        {
            "title": "Senior Engineer",
            "company": "BigCo",
            "bullets": [
                "Architected and implemented a distributed microservices platform that reduced operational costs by 35% while improving system throughput by over 200 percent",
                "Led cross-functional team of 12 engineers across three time zones delivering a complete platform rewrite in under 18 months with zero downtime",
                "Designed the company's data ingestion pipeline handling over 5 billion events per day using Apache Kafka and custom stream processing",
            ],
        }
    ],
    "skills": ["Java"],
    "education": [],
    "certifications": [],
    "projects": [],
}

SAMPLE_PERIOD_RESUME = {
    "professional_summary": "Developer.",
    "work_experience": [
        {
            "title": "Dev",
            "company": "Co",
            "bullets": [
                "Built APIs using Python.",
                "Managed deployments.",
                "Reduced latency by 30%.",
                "Wrote unit tests.",
            ],
        }
    ],
    "skills": [],
    "education": [],
    "certifications": [],
    "projects": [],
}

SAMPLE_DASH_BULLETS_RESUME = {
    "professional_summary": "Developer.",
    "work_experience": [
        {
            "title": "Dev",
            "company": "Co",
            "bullets": [
                "- Built APIs using Python",
                "- Managed deployments",
                "- Reduced latency by 30%",
            ],
        }
    ],
    "skills": [],
    "education": [],
    "certifications": [],
    "projects": [],
}


# ---------------------------------------------------------------------------
# StyleExtractor tests
# ---------------------------------------------------------------------------


class TestStyleExtractorBasic:
    """Basic extract() contract tests."""

    def setup_method(self):
        from resume_engine.style_extractor import StyleExtractor
        self.extractor = StyleExtractor()

    def test_extract_returns_all_keys(self):
        result = self.extractor.extract(SAMPLE_PUNCHY_RESUME)
        for key in ("voice", "sentence_structure", "metric_usage",
                    "structure", "format", "extracted_at", "bullet_count"):
            assert key in result, f"Missing key: {key}"

    def test_extract_empty_resume_returns_defaults(self):
        result = self.extractor.extract({})
        assert isinstance(result, dict)
        assert "voice" in result
        assert "sentence_structure" in result
        assert result["bullet_count"] == 0

    def test_extract_none_returns_defaults(self):
        result = self.extractor.extract(None)  # type: ignore[arg-type]
        assert isinstance(result, dict)
        assert "voice" in result

    def test_bullet_count_correct(self):
        result = self.extractor.extract(SAMPLE_PUNCHY_RESUME)
        assert result["bullet_count"] == 5

    def test_extracted_at_is_iso_string(self):
        result = self.extractor.extract(SAMPLE_PUNCHY_RESUME)
        at = result["extracted_at"]
        assert isinstance(at, str)
        assert "T" in at  # ISO format contains T separator


class TestVoiceDetection:

    def setup_method(self):
        from resume_engine.style_extractor import StyleExtractor
        self.extractor = StyleExtractor()

    def test_detect_voice_no_pronouns(self):
        result = self.extractor.extract(SAMPLE_PUNCHY_RESUME)
        assert result["voice"] == "no_pronouns"

    def test_detect_voice_first_person(self):
        result = self.extractor.extract(SAMPLE_FIRST_PERSON_RESUME)
        assert result["voice"] == "first_person"

    def test_detect_voice_third_person(self):
        resume = {
            "professional_summary": "He is an experienced engineer.",
            "work_experience": [
                {
                    "title": "Engineer",
                    "company": "Co",
                    "bullets": [
                        "He led the team to success",
                        "He managed his budget carefully",
                        "He delivered results ahead of schedule",
                    ],
                }
            ],
            "skills": [],
            "education": [],
            "certifications": [],
            "projects": [],
        }
        result = self.extractor.extract(resume)
        assert result["voice"] == "third_person"

    def test_detect_voice_empty_text(self):
        result = self.extractor._detect_voice("")
        assert result == "no_pronouns"


class TestSentenceStructure:

    def setup_method(self):
        from resume_engine.style_extractor import StyleExtractor
        self.extractor = StyleExtractor()

    def test_detect_sentence_structure_punchy(self):
        result = self.extractor.extract(SAMPLE_PUNCHY_RESUME)
        ss = result["sentence_structure"]
        assert ss["style"] == "punchy"
        assert ss["avg_word_count"] <= 12

    def test_detect_sentence_structure_detailed(self):
        result = self.extractor.extract(SAMPLE_DETAILED_RESUME)
        ss = result["sentence_structure"]
        assert ss["style"] == "detailed"
        assert ss["avg_word_count"] > 20

    def test_detect_sentence_structure_empty_bullets(self):
        result = self.extractor._detect_sentence_structure([])
        assert result["style"] == "moderate"
        assert result["avg_word_count"] == 0.0

    def test_sentence_structure_has_all_keys(self):
        result = self.extractor.extract(SAMPLE_PUNCHY_RESUME)
        ss = result["sentence_structure"]
        for k in ("style", "avg_word_count", "min_word_count", "max_word_count"):
            assert k in ss


class TestMetricUsage:

    def setup_method(self):
        from resume_engine.style_extractor import StyleExtractor
        self.extractor = StyleExtractor()

    def test_detect_metric_usage_heavy(self):
        result = self.extractor.extract(SAMPLE_PUNCHY_RESUME)
        mu = result["metric_usage"]
        assert mu["density"] == "heavy"
        assert mu["ratio"] >= 0.4

    def test_detect_metric_usage_light(self):
        resume = {
            "professional_summary": "",
            "work_experience": [
                {
                    "title": "Dev",
                    "company": "Co",
                    "bullets": [
                        "Led the team to deliver projects",
                        "Improved communication across departments",
                        "Collaborated with product and design",
                        "Mentored junior engineers",
                        "Contributed to open source projects",
                    ],
                }
            ],
            "skills": [],
            "education": [],
            "certifications": [],
            "projects": [],
        }
        result = self.extractor.extract(resume)
        assert result["metric_usage"]["density"] == "light"

    def test_detect_metric_usage_zero_bullets(self):
        result = self.extractor._detect_metric_usage([])
        assert result["density"] == "light"
        assert result["ratio"] == 0.0

    def test_metric_usage_has_all_keys(self):
        result = self.extractor.extract(SAMPLE_PUNCHY_RESUME)
        for k in ("density", "ratio", "bullets_with_metrics", "total_bullets"):
            assert k in result["metric_usage"]


class TestStructureDetection:

    def setup_method(self):
        from resume_engine.style_extractor import StyleExtractor
        self.extractor = StyleExtractor()

    def test_detect_structure_order(self):
        result = self.extractor.extract(SAMPLE_PUNCHY_RESUME)
        structure = result["structure"]
        assert "professional_summary" in structure
        assert "work_experience" in structure
        # summary should come before work_experience
        assert structure.index("professional_summary") < structure.index("work_experience")

    def test_detect_structure_excludes_empty_sections(self):
        resume = {**SAMPLE_PUNCHY_RESUME, "projects": []}
        result = self.extractor.extract(resume)
        assert "projects" not in result["structure"]

    def test_detect_structure_excludes_none_sections(self):
        resume = {**SAMPLE_PUNCHY_RESUME, "certifications": None}
        result = self.extractor.extract(resume)
        assert "certifications" not in result["structure"]

    def test_detect_structure_empty_resume(self):
        result = self.extractor.extract({})
        assert result["structure"] == []


class TestFormatDetection:

    def setup_method(self):
        from resume_engine.style_extractor import StyleExtractor
        self.extractor = StyleExtractor()

    def test_detect_format_bullet_char_dash(self):
        result = self.extractor.extract(SAMPLE_DASH_BULLETS_RESUME)
        assert result["format"]["bullet_char"] == "-"

    def test_detect_format_bullet_char_none(self):
        result = self.extractor.extract(SAMPLE_PUNCHY_RESUME)
        # No bullet chars in punchy resume bullets — "none"
        assert result["format"]["bullet_char"] == "none"

    def test_detect_format_trailing_period_true(self):
        result = self.extractor.extract(SAMPLE_PERIOD_RESUME)
        assert result["format"]["trailing_period"] is True

    def test_detect_format_trailing_period_false(self):
        result = self.extractor.extract(SAMPLE_PUNCHY_RESUME)
        assert result["format"]["trailing_period"] is False

    def test_detect_format_capitalization_upper(self):
        result = self.extractor.extract(SAMPLE_PUNCHY_RESUME)
        assert result["format"]["capitalization"] == "upper"

    def test_detect_format_empty_bullets(self):
        result = self.extractor._detect_format([])
        assert result["bullet_char"] == "none"
        assert result["capitalization"] == "upper"
        assert result["trailing_period"] is False

    def test_detect_format_has_all_keys(self):
        result = self.extractor.extract(SAMPLE_PUNCHY_RESUME)
        for k in ("bullet_char", "capitalization", "trailing_period"):
            assert k in result["format"]


# ---------------------------------------------------------------------------
# Rewriter style constraint injection tests
# ---------------------------------------------------------------------------


class TestRewriterStyleConstraints:
    """Verify that style constraints are injected into / omitted from prompts."""

    STYLE_FINGERPRINT = {
        "voice": "no_pronouns",
        "sentence_structure": {"style": "punchy", "avg_word_count": 9},
        "metric_usage": {"density": "heavy"},
        "format": {
            "bullet_char": "•",
            "capitalization": "upper",
            "trailing_period": False,
        },
    }

    def _make_rewriter(self):
        from resume_engine.rewriter import Rewriter, _NVIDIA_MODEL_ID
        from resume_engine.rate_limiter import RateLimiter
        rw = Rewriter.__new__(Rewriter)
        rw.api_call_count = 0
        rw._max_retries = 1
        rw.model_id = _NVIDIA_MODEL_ID
        nvidia_limiter = MagicMock(spec=RateLimiter)
        nvidia_limiter.rpm = 60
        nvidia_limiter.rpd = 5000
        rw._nvidia_limiter = nvidia_limiter
        rw._limiter = nvidia_limiter
        rw._nvidia_client = None
        rw._call_nvidia = MagicMock(return_value=None)
        return rw

    def test_style_constraints_injected_into_prompt(self):
        rw = self._make_rewriter()
        captured = []

        def mock_call(prompt, model="primary"):
            captured.append(prompt)
            return "Built scalable APIs reducing latency by 40%"

        rw._call_nvidia = mock_call

        rw.rewrite_bullet_point(
            "Built some APIs",
            ["Django", "REST"],
            "Senior SWE role",
            style_fingerprint=self.STYLE_FINGERPRINT,
        )

        assert captured, "No prompt captured"
        assert "STYLE CONSTRAINTS" in captured[0]
        assert "HARD RULES" in captured[0]

    def test_style_constraints_voice_no_pronouns_in_prompt(self):
        rw = self._make_rewriter()
        captured = []

        def mock_call(prompt, model="primary"):
            captured.append(prompt)
            return "Optimised system performance"

        rw._call_nvidia = mock_call

        rw.rewrite_bullet_point(
            "I optimised the system",
            ["Python"],
            "SWE role",
            style_fingerprint=self.STYLE_FINGERPRINT,
        )

        assert "Omit all pronouns" in captured[0]

    def test_style_constraints_not_injected_when_none(self):
        rw = self._make_rewriter()
        captured = []

        def mock_call(prompt, model="primary"):
            captured.append(prompt)
            return "Built APIs"

        rw._call_nvidia = mock_call

        rw.rewrite_bullet_point(
            "Built APIs",
            ["Django"],
            "SWE role",
            style_fingerprint=None,
        )

        assert "STYLE CONSTRAINTS" not in captured[0]

    def test_style_constraints_metrics_heavy_in_prompt(self):
        rw = self._make_rewriter()
        captured = []

        def mock_call(prompt, model="primary"):
            captured.append(prompt)
            return "Reduced latency by 40%"

        rw._call_nvidia = mock_call

        rw.rewrite_bullet_point(
            "Reduced latency by 40%",
            ["Redis"],
            "SWE role",
            style_fingerprint=self.STYLE_FINGERPRINT,
        )

        assert "PRESERVE all numbers" in captured[0] or "data-driven" in captured[0]

    def test_style_constraints_first_person_in_prompt(self):
        rw = self._make_rewriter()
        captured = []

        def mock_call(prompt, model="primary"):
            captured.append(prompt)
            return "I built APIs"

        rw._call_nvidia = mock_call

        fp_style = {**self.STYLE_FINGERPRINT, "voice": "first_person"}
        rw.rewrite_bullet_point(
            "Built APIs",
            ["Django"],
            "SWE role",
            style_fingerprint=fp_style,
        )

        assert "first-person pronouns" in captured[0] or "first person" in captured[0].lower()

    def test_build_style_constraints_returns_empty_for_none(self):
        rw = self._make_rewriter()
        result = rw._build_style_constraints(None)
        assert result == ""

    def test_build_style_constraints_returns_empty_for_empty_dict(self):
        rw = self._make_rewriter()
        result = rw._build_style_constraints({})
        assert result == ""

    def test_summary_style_constraints_injected(self):
        rw = self._make_rewriter()
        captured = []

        def mock_call(prompt, model="primary"):
            captured.append(prompt)
            return "Senior engineer with 5 years delivering results."

        rw._call_nvidia = mock_call

        rw.generate_professional_summary(
            "Engineer with experience.",
            "Senior SWE",
            ["Python", "AWS"],
            years_experience=5,
            style_fingerprint=self.STYLE_FINGERPRINT,
        )

        assert captured
        assert "STYLE CONSTRAINTS" in captured[0]


# ---------------------------------------------------------------------------
# ResumeModifier._enforce_structure_order tests
# ---------------------------------------------------------------------------


class TestEnforceStructureOrder:
    """Tests for the section-ordering helper."""

    def setup_method(self):
        from resume_engine.modifier import ResumeModifier
        # Build a modifier without requiring a live NVIDIA NIM key
        self.modifier = ResumeModifier.__new__(ResumeModifier)

    def test_order_matches_structure(self):
        order = [
            "professional_summary",
            "skills",
            "work_experience",
            "education",
        ]
        content = {
            "work_experience": [{"title": "SWE"}],
            "skills": ["Python"],
            "professional_summary": "Engineer",
            "education": [{"degree": "BS"}],
        }
        result = self.modifier._enforce_structure_order(content, order)
        keys = list(result.keys())
        assert keys[0] == "professional_summary"
        assert keys[1] == "skills"
        assert keys[2] == "work_experience"
        assert keys[3] == "education"

    def test_extra_keys_appended_at_end(self):
        order = ["professional_summary", "work_experience"]
        content = {
            "work_experience": [],
            "professional_summary": "text",
            "certifications": [],
            "personal_info": {"name": "Jane"},
        }
        result = self.modifier._enforce_structure_order(content, order)
        keys = list(result.keys())
        assert keys[0] == "professional_summary"
        assert keys[1] == "work_experience"
        # certifications and personal_info present somewhere after
        assert "certifications" in keys
        assert "personal_info" in keys

    def test_missing_keys_in_order_skipped(self):
        order = ["professional_summary", "nonexistent_section", "work_experience"]
        content = {
            "professional_summary": "text",
            "work_experience": [],
        }
        result = self.modifier._enforce_structure_order(content, order)
        assert "nonexistent_section" not in result
        assert list(result.keys()) == ["professional_summary", "work_experience"]

    def test_empty_structure_order_returns_original(self):
        content = {"work_experience": [], "skills": []}
        result = self.modifier._enforce_structure_order(content, [])
        assert result == content

    def test_all_data_preserved(self):
        order = ["skills", "work_experience"]
        content = {"work_experience": [1, 2], "skills": ["Python", "AWS"]}
        result = self.modifier._enforce_structure_order(content, order)
        assert result["skills"] == ["Python", "AWS"]
        assert result["work_experience"] == [1, 2]


# ---------------------------------------------------------------------------
# Flask upload route — style fingerprint stored and returned
# ---------------------------------------------------------------------------


class TestUploadStoresStyleFingerprint:
    """Integration tests for upload route style extraction."""

    IN_MEMORY = "sqlite:///:memory:"

    def setup_method(self):
        from database.database import reset_manager, create_tables
        reset_manager(self.IN_MEMORY)
        create_tables()

        import tempfile, os
        self._settings_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        )
        self._settings_file.write("{}")
        self._settings_file.close()

        os.environ["SETTINGS_FILE"] = self._settings_file.name

        # Create Flask test client
        import web.app as web_app
        web_app.app.config["TESTING"] = True
        self.client = web_app.app.test_client()

    def teardown_method(self):
        from database.database import reset_manager
        reset_manager(self.IN_MEMORY)
        try:
            os.unlink(self._settings_file.name)
        except Exception:
            pass
        if "SETTINGS_FILE" in os.environ:
            del os.environ["SETTINGS_FILE"]

    def _make_fake_pdf(self) -> bytes:
        return b"%PDF-1.4 fake pdf content"

    def _make_parsed_resume(self) -> dict:
        return {
            "personal_info": {"name": "Jane Doe", "email": "jane@example.com"},
            "professional_summary": "Engineer with 5 years experience.",
            "work_experience": [
                {
                    "title": "SWE",
                    "company": "Corp",
                    "dates": "2020-Present",
                    "bullets": [
                        "Built REST APIs serving 1M+ requests",
                        "Reduced latency by 40%",
                        "Led team of 5",
                    ],
                }
            ],
            "skills": ["Python", "AWS"],
            "education": [{"degree": "B.S. CS"}],
            "certifications": [],
            "projects": [],
        }

    def test_upload_response_includes_style_key(self):
        parsed = self._make_parsed_resume()
        with patch("pdf_generator.pdf_parser.ResumePDFParser.parse", return_value=parsed):
            data = {
                "file": (io.BytesIO(self._make_fake_pdf()), "resume.pdf"),
                "name": "Test Resume",
            }
            resp = self.client.post(
                "/api/resume/upload",
                data=data,
                content_type="multipart/form-data",
            )
        assert resp.status_code == 200
        body = resp.get_json()
        assert "style" in body

    def test_upload_response_style_has_required_keys(self):
        parsed = self._make_parsed_resume()
        with patch("pdf_generator.pdf_parser.ResumePDFParser.parse", return_value=parsed):
            data = {
                "file": (io.BytesIO(self._make_fake_pdf()), "resume.pdf"),
                "name": "Test Resume",
            }
            resp = self.client.post(
                "/api/resume/upload",
                data=data,
                content_type="multipart/form-data",
            )
        body = resp.get_json()
        style = body.get("style", {})
        for k in ("voice", "structure", "metrics", "bullet_char"):
            assert k in style, f"Missing style key: {k}"

    def test_upload_stores_style_fingerprint_in_db(self):
        from database.database import get_db
        from database.models import MasterResume as MR
        parsed = self._make_parsed_resume()
        with patch("pdf_generator.pdf_parser.ResumePDFParser.parse", return_value=parsed):
            data = {
                "file": (io.BytesIO(self._make_fake_pdf()), "resume.pdf"),
                "name": "My Resume",
            }
            resp = self.client.post(
                "/api/resume/upload",
                data=data,
                content_type="multipart/form-data",
            )
        assert resp.status_code == 200
        body = resp.get_json()
        resume_id = body.get("id")
        assert resume_id is not None

        with get_db() as db:
            mr = db.query(MR).filter(MR.id == resume_id).first()
            assert mr is not None
            assert mr.style_fingerprint is not None
            assert isinstance(mr.style_fingerprint, dict)
            assert mr.style_fingerprint.get("voice") in (
                "first_person", "third_person", "no_pronouns"
            )

    def test_upload_style_voice_no_pronouns_for_punchy_resume(self):
        parsed = self._make_parsed_resume()
        with patch("pdf_generator.pdf_parser.ResumePDFParser.parse", return_value=parsed):
            data = {
                "file": (io.BytesIO(self._make_fake_pdf()), "resume.pdf"),
                "name": "Resume",
            }
            resp = self.client.post(
                "/api/resume/upload",
                data=data,
                content_type="multipart/form-data",
            )
        body = resp.get_json()
        # Punchy action-verb resume → no pronouns
        assert body["style"]["voice"] == "no_pronouns"


# ---------------------------------------------------------------------------
# Flask generate-resume route passes style_fingerprint to modifier
# ---------------------------------------------------------------------------


class TestGenerateResumePassesStyle:
    """Verify that the generate-resume API route passes style_fingerprint."""

    IN_MEMORY = "sqlite:///:memory:"

    def setup_method(self):
        from database.database import reset_manager, create_tables, get_db
        from database.models import MasterResume as MR, Job
        import json as _json

        reset_manager(self.IN_MEMORY)
        create_tables()

        self._settings_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        )
        self._settings_file.write("{}")
        self._settings_file.close()
        os.environ["SETTINGS_FILE"] = self._settings_file.name

        self._sample_style = {
            "voice": "no_pronouns",
            "sentence_structure": {"style": "punchy", "avg_word_count": 9.0,
                                   "min_word_count": 4, "max_word_count": 12},
            "metric_usage": {"density": "heavy", "ratio": 0.6,
                             "bullets_with_metrics": 3, "total_bullets": 5},
            "structure": ["professional_summary", "work_experience", "skills"],
            "format": {"bullet_char": "•", "capitalization": "upper",
                       "trailing_period": False},
            "extracted_at": "2026-01-01T00:00:00+00:00",
            "bullet_count": 5,
        }

        with get_db() as db:
            master = MR(
                name="My Resume",
                content={
                    "personal_info": {"name": "J", "email": "j@j.com"},
                    "professional_summary": "Engineer.",
                    "work_experience": [],
                    "skills": ["Python"],
                    "education": [],
                    "certifications": [],
                    "projects": [],
                },
                is_active=True,
                is_sample=False,
                style_fingerprint=self._sample_style,
            )
            db.add(master)
            db.flush()
            self._master_id = master.id

            from datetime import datetime, timezone
            job = Job(
                job_title="SWE",
                company_name="Corp",
                job_description="Python developer role",
                application_url="https://example.com/job/1",
                required_skills=_json.dumps(["Python"]),
                preferred_skills=_json.dumps([]),
                status="analyzed",
                source="test",
                date_scraped=datetime.now(timezone.utc),
            )
            db.add(job)
            db.flush()
            self._job_id = job.id
            db.commit()

        import web.app as web_app
        web_app.app.config["TESTING"] = True
        self.client = web_app.app.test_client()

    def teardown_method(self):
        from database.database import reset_manager
        reset_manager(self.IN_MEMORY)
        try:
            os.unlink(self._settings_file.name)
        except Exception:
            pass
        if "SETTINGS_FILE" in os.environ:
            del os.environ["SETTINGS_FILE"]

    def test_generate_resume_passes_style_fingerprint_to_modifier(self):
        # The route accesses tailored["content"] etc (dict-style)
        mock_result = {
            "content": {
                "personal_info": {},
                "professional_summary": "Tailored.",
                "work_experience": [],
                "skills": ["Python"],
                "education": [],
                "certifications": [],
                "projects": [],
            },
            "metrics": {
                "keyword_coverage_before": 50.0,
                "keyword_coverage_after": 70.0,
                "keyword_coverage_improvement": 20.0,
            },
            "modification_log": [],
            "validation_report": {"is_valid": True, "summary_stats": {}},
            "api_calls_used": 2,
        }

        captured_kwargs = {}

        def fake_modify(master, job, match_analysis=None, style_fingerprint=None):
            captured_kwargs["style_fingerprint"] = style_fingerprint
            return mock_result

        mock_modifier_instance = MagicMock()
        mock_modifier_instance.modify_resume.side_effect = fake_modify

        with patch("resume_engine.modifier.ResumeModifier",
                   return_value=mock_modifier_instance):
            resp = self.client.post(
                "/api/generate-resume",
                json={"job_id": self._job_id},
            )

        assert resp.status_code == 200, resp.get_data(as_text=True)
        passed_style = captured_kwargs.get("style_fingerprint")
        assert passed_style is not None
        assert passed_style.get("voice") == "no_pronouns"
