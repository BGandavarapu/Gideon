"""Tests for pdf_generator/pdf_parser.py — ResumeClassifier and ResumePDFParser.

Coverage:
  TestResumeClassifierHeuristic  — Stage 1 heuristic classification
  TestResumeClassifierNvidia     — Stage 2 NVIDIA NIM classification (mocked)
  TestResumeClassifierPipeline   — Full classify() pipeline
  TestResumePDFParserGate        — parse() raises NotAResumeError for non-resumes
  TestUploadAPIClassification    — Flask /api/resume/upload integration tests
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pdf_generator.pdf_parser import NotAResumeError, ResumeClassifier, ResumePDFParser

# ---------------------------------------------------------------------------
# Sample texts
# ---------------------------------------------------------------------------

SAMPLE_RESUME_TEXT = """
John Smith
john.smith@email.com | (555) 123-4567 | San Francisco, CA

PROFESSIONAL SUMMARY
Experienced software engineer with 5 years in Python development.

WORK EXPERIENCE
Senior Software Engineer — Acme Corp (2021–Present)
• Built REST APIs serving 1M+ requests daily
• Led team of 4 engineers

EDUCATION
B.S. Computer Science — UC Berkeley, 2019

SKILLS
Python, Django, PostgreSQL, AWS, Docker
"""

SAMPLE_INVOICE_TEXT = """
INVOICE #12345
Bill To: Acme Corporation
Invoice Date: March 1, 2026
Payment Due: March 31, 2026

Item           Qty    Unit Price    Total
Widget A        10      $25.00     $250.00
Widget B         5      $50.00     $250.00

Subtotal: $500.00
Tax (8%):  $40.00
Total Amount Due: $540.00
"""

SAMPLE_RESEARCH_TEXT = """
Abstract
This paper presents a novel approach to machine learning optimization.

1. Introduction
Recent advances in deep learning have shown...

2. Methodology
We propose a new algorithm based on...

3. Conclusion
Our results demonstrate significant improvements.

References
[1] LeCun et al. (1998). Gradient-based learning...
DOI: 10.1109/5.726791
"""

# Text that has some resume signals but also non-resume noise — forces NVIDIA NIM
AMBIGUOUS_TEXT = """
Summary of Technical Report

Skills: Python, Java, SQL (used in the project)
Education: Master's in Data Science

Invoice #001
Bill To: Research Department

This document covers methodology for the quarterly analysis.
"""


# ---------------------------------------------------------------------------
# Tests: Stage 1 — heuristic
# ---------------------------------------------------------------------------

class TestResumeClassifierHeuristic(unittest.TestCase):

    def setUp(self) -> None:
        self.classifier = ResumeClassifier()

    def test_heuristic_classifies_resume_correctly(self) -> None:
        result = self.classifier.classify_heuristic(SAMPLE_RESUME_TEXT)
        self.assertEqual(result["verdict"], "resume")
        self.assertGreaterEqual(result["confidence"], 0.70)
        self.assertEqual(result["method"], "heuristic")

    def test_heuristic_classifies_invoice_as_not_resume(self) -> None:
        result = self.classifier.classify_heuristic(SAMPLE_INVOICE_TEXT)
        self.assertEqual(result["verdict"], "not_resume")
        self.assertLess(result["confidence"], 0.70)

    def test_heuristic_classifies_research_paper_as_not_resume(self) -> None:
        result = self.classifier.classify_heuristic(SAMPLE_RESEARCH_TEXT)
        self.assertEqual(result["verdict"], "not_resume")

    def test_heuristic_returns_dict_with_required_keys(self) -> None:
        result = self.classifier.classify_heuristic(SAMPLE_RESUME_TEXT)
        for key in ("verdict", "confidence", "signals_found", "method"):
            self.assertIn(key, result, f"Missing key: {key}")

    def test_heuristic_detects_email_signal(self) -> None:
        text = "Jane Doe\njane@example.com\nSkills: Python"
        result = self.classifier.classify_heuristic(text)
        signals_str = " ".join(result["signals_found"]).lower()
        # Either the email signal string is present OR confidence is boosted
        self.assertTrue(
            "email" in signals_str or result["confidence"] > 0.4,
            f"Expected email signal or boosted confidence, got: {result}",
        )

    def test_heuristic_detects_phone_signal(self) -> None:
        text = "Jane Doe\n(555) 987-6543\nExperience: 3 years Python\nSkills: Java"
        result = self.classifier.classify_heuristic(text)
        self.assertGreater(result["confidence"], 0.3)

    def test_heuristic_confidence_range(self) -> None:
        for text in (SAMPLE_RESUME_TEXT, SAMPLE_INVOICE_TEXT, SAMPLE_RESEARCH_TEXT):
            result = self.classifier.classify_heuristic(text)
            self.assertGreaterEqual(result["confidence"], 0.0)
            self.assertLessEqual(result["confidence"], 1.0)

    def test_heuristic_two_non_resume_signals_forces_not_resume(self) -> None:
        # Bill To + Invoice: two definite non-resume signals
        text = "Invoice\nBill To: Client\nPayment Due: April 1\nTotal Amount: 500"
        result = self.classifier.classify_heuristic(text)
        self.assertEqual(result["verdict"], "not_resume")

    def test_heuristic_empty_text_not_a_resume(self) -> None:
        result = self.classifier.classify_heuristic("")
        self.assertIn(result["verdict"], ("not_resume", "inconclusive"))

    def test_heuristic_signals_found_is_list(self) -> None:
        result = self.classifier.classify_heuristic(SAMPLE_RESUME_TEXT)
        self.assertIsInstance(result["signals_found"], list)

    def test_heuristic_resume_has_multiple_header_signals(self) -> None:
        result = self.classifier.classify_heuristic(SAMPLE_RESUME_TEXT)
        header_signals = [
            s for s in result["signals_found"] if s.startswith("resume_header:")
        ]
        self.assertGreater(len(header_signals), 0)


# ---------------------------------------------------------------------------
# Tests: Stage 2 — NVIDIA NIM (mocked)
# ---------------------------------------------------------------------------

class TestResumeClassifierNvidia(unittest.TestCase):

    def setUp(self) -> None:
        self.classifier = ResumeClassifier()

    def _mock_nvidia_response(self, json_str: str):
        """Return a mock openai.OpenAI client that yields json_str as response content."""
        mock_choice = MagicMock()
        mock_choice.message.content = json_str
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response
        return mock_client

    def test_classify_with_nvidia_mocked_resume(self) -> None:
        payload = json.dumps({
            "is_resume": True,
            "confidence": 0.92,
            "document_type": "resume",
            "reason": "Contains work experience and contact info",
        })
        mock_client = self._mock_nvidia_response(payload)
        with patch("openai.OpenAI", return_value=mock_client):
            with patch.dict("os.environ", {"NVIDIA_API_KEY": "test-key"}):
                result = self.classifier.classify_with_nvidia(SAMPLE_RESUME_TEXT)

        self.assertEqual(result["verdict"], "resume")
        self.assertAlmostEqual(result["confidence"], 0.92, places=2)
        self.assertEqual(result["document_type"], "resume")
        self.assertEqual(result["method"], "nvidia")

    def test_classify_with_nvidia_mocked_not_resume(self) -> None:
        payload = json.dumps({
            "is_resume": False,
            "confidence": 0.88,
            "document_type": "invoice",
            "reason": "Contains billing info and payment amounts",
        })
        mock_client = self._mock_nvidia_response(payload)
        with patch("openai.OpenAI", return_value=mock_client):
            with patch.dict("os.environ", {"NVIDIA_API_KEY": "test-key"}):
                result = self.classifier.classify_with_nvidia(SAMPLE_INVOICE_TEXT)

        self.assertEqual(result["verdict"], "not_resume")
        self.assertEqual(result["document_type"], "invoice")

    def test_classify_with_nvidia_handles_bad_json(self) -> None:
        mock_client = self._mock_nvidia_response("Sorry, I cannot classify this.")
        with patch("openai.OpenAI", return_value=mock_client):
            with patch.dict("os.environ", {"NVIDIA_API_KEY": "test-key"}):
                result = self.classifier.classify_with_nvidia("some text")

        self.assertEqual(result["verdict"], "inconclusive")
        self.assertEqual(result["method"], "nvidia_failed")

    def test_classify_with_nvidia_handles_empty_response(self) -> None:
        mock_client = self._mock_nvidia_response("")
        with patch("openai.OpenAI", return_value=mock_client):
            with patch.dict("os.environ", {"NVIDIA_API_KEY": "test-key"}):
                result = self.classifier.classify_with_nvidia("some text")

        # Should not raise, should return inconclusive
        self.assertIn(result["verdict"], ("inconclusive", "not_resume", "resume"))

    def test_classify_with_nvidia_strips_markdown_fences(self) -> None:
        payload = (
            "```json\n"
            + json.dumps({"is_resume": True, "confidence": 0.85,
                          "document_type": "resume", "reason": "Has skills"})
            + "\n```"
        )
        mock_client = self._mock_nvidia_response(payload)
        with patch("openai.OpenAI", return_value=mock_client):
            with patch.dict("os.environ", {"NVIDIA_API_KEY": "test-key"}):
                result = self.classifier.classify_with_nvidia(SAMPLE_RESUME_TEXT)

        self.assertEqual(result["verdict"], "resume")

    def test_classify_with_nvidia_no_api_key_returns_inconclusive(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            import os; os.environ.pop("NVIDIA_API_KEY", None)
            result = self.classifier.classify_with_nvidia("text")

        self.assertEqual(result["verdict"], "inconclusive")
        self.assertEqual(result["method"], "nvidia_failed")

    def test_classify_with_nvidia_low_confidence_returns_inconclusive(self) -> None:
        # Threshold for is_resume=True is now 0.50; use 0.45 to stay below it
        payload = json.dumps({
            "is_resume": True,
            "confidence": 0.45,  # below the 0.50 resume threshold
            "document_type": "resume",
            "reason": "Uncertain",
        })
        mock_client = self._mock_nvidia_response(payload)
        with patch("openai.OpenAI", return_value=mock_client):
            with patch.dict("os.environ", {"NVIDIA_API_KEY": "test-key"}):
                result = self.classifier.classify_with_nvidia("text")

        self.assertEqual(result["verdict"], "inconclusive")


# ---------------------------------------------------------------------------
# Tests: Full pipeline — classify()
# ---------------------------------------------------------------------------

class TestResumeClassifierPipeline(unittest.TestCase):

    def setUp(self) -> None:
        self.classifier = ResumeClassifier()

    def test_classify_invoice_skips_nvidia(self) -> None:
        """Obvious invoice → heuristic catches it, NVIDIA NIM should NOT be called."""
        with patch.object(self.classifier, "classify_with_nvidia") as mock_nvidia:
            result = self.classifier.classify(SAMPLE_INVOICE_TEXT)
            mock_nvidia.assert_not_called()
        self.assertEqual(result["verdict"], "not_resume")

    def test_classify_research_paper_skips_nvidia(self) -> None:
        with patch.object(self.classifier, "classify_with_nvidia") as mock_nvidia:
            result = self.classifier.classify(SAMPLE_RESEARCH_TEXT)
            mock_nvidia.assert_not_called()
        self.assertEqual(result["verdict"], "not_resume")

    def test_classify_resume_skips_nvidia(self) -> None:
        """Clear resume → heuristic catches it, NVIDIA NIM should NOT be called."""
        with patch.object(self.classifier, "classify_with_nvidia") as mock_nvidia:
            result = self.classifier.classify(SAMPLE_RESUME_TEXT)
            mock_nvidia.assert_not_called()
        self.assertEqual(result["verdict"], "resume")

    def test_classify_calls_nvidia_for_inconclusive(self) -> None:
        """Ambiguous doc → heuristic returns inconclusive → NVIDIA NIM called."""
        with patch.object(
            self.classifier,
            "classify_heuristic",
            return_value={
                "verdict": "inconclusive", "confidence": 0.5,
                "signals_found": [], "document_type": "unknown",
                "reason": "", "method": "heuristic",
            },
        ):
            with patch.object(
                self.classifier,
                "classify_with_nvidia",
                return_value={
                    "verdict": "resume", "confidence": 0.85,
                    "document_type": "resume", "signals_found": [],
                    "reason": "Has experience section", "method": "nvidia",
                },
            ) as mock_nvidia:
                result = self.classifier.classify(AMBIGUOUS_TEXT)
                mock_nvidia.assert_called_once()

        self.assertEqual(result["verdict"], "resume")

    def test_both_inconclusive_defaults_to_not_resume(self) -> None:
        inconclusive = {
            "verdict": "inconclusive", "confidence": 0.5,
            "signals_found": [], "document_type": "unknown",
            "reason": "", "method": "heuristic",
        }
        with patch.object(self.classifier, "classify_heuristic", return_value=inconclusive):
            with patch.object(self.classifier, "classify_with_nvidia", return_value=inconclusive):
                result = self.classifier.classify("ambiguous text")

        self.assertEqual(result["verdict"], "not_resume")
        self.assertIn("Could not confidently", result["reason"])

    def test_classify_returns_required_keys(self) -> None:
        result = self.classifier.classify(SAMPLE_RESUME_TEXT)
        for key in ("verdict", "confidence", "document_type", "signals_found",
                    "reason", "method"):
            self.assertIn(key, result, f"Missing key: {key}")

    def test_classify_verdict_is_valid(self) -> None:
        for text in (SAMPLE_RESUME_TEXT, SAMPLE_INVOICE_TEXT):
            result = self.classifier.classify(text)
            self.assertIn(
                result["verdict"], ("resume", "not_resume", "inconclusive")
            )


# ---------------------------------------------------------------------------
# Tests: ResumePDFParser gate
# ---------------------------------------------------------------------------

class TestResumePDFParserGate(unittest.TestCase):

    def setUp(self) -> None:
        self.parser = ResumePDFParser()

    def test_not_a_resume_error_raised_on_invoice_upload(self) -> None:
        with patch.object(ResumePDFParser, "_extract_text", return_value=SAMPLE_INVOICE_TEXT):
            with patch.object(ResumePDFParser, "_parse_with_nvidia", return_value=None):
                with self.assertRaises(NotAResumeError) as ctx:
                    self.parser.parse(b"%PDF fake invoice content")

        exc = ctx.exception
        self.assertIsInstance(exc, NotAResumeError)

    def test_not_a_resume_error_has_document_type(self) -> None:
        with patch.object(ResumePDFParser, "_extract_text", return_value=SAMPLE_INVOICE_TEXT):
            with patch.object(ResumePDFParser, "_parse_with_nvidia", return_value=None):
                try:
                    self.parser.parse(b"%PDF fake")
                    self.fail("Expected NotAResumeError")
                except NotAResumeError as exc:
                    self.assertIsInstance(exc.document_type, str)
                    self.assertIsInstance(exc.confidence, float)
                    self.assertIsInstance(exc.reason, str)

    def test_resume_passes_through_successfully(self) -> None:
        fake_parsed = {
            "personal_info": {"name": "John Smith", "email": "john@test.com",
                              "phone": "", "location": ""},
            "professional_summary": "Engineer",
            "skills": ["Python", "Django"],
            "work_experience": [{"title": "Engineer", "company": "Acme",
                                  "location": "", "start_date": "2021",
                                  "end_date": "Present", "bullets": []}],
            "education": [], "projects": [],
        }
        with patch.object(ResumePDFParser, "_extract_text", return_value=SAMPLE_RESUME_TEXT):
            with patch.object(ResumePDFParser, "_parse_with_nvidia", return_value=fake_parsed):
                result = self.parser.parse(b"%PDF fake resume")

        self.assertIn("personal_info", result)
        self.assertIn("skills", result)
        self.assertEqual(result["skills"], ["Python", "Django"])

    def test_not_a_resume_error_message_is_str(self) -> None:
        exc = NotAResumeError(
            document_type="invoice",
            confidence=0.95,
            reason="Contains billing information",
        )
        self.assertEqual(str(exc), "Contains billing information")
        self.assertEqual(exc.document_type, "invoice")
        self.assertAlmostEqual(exc.confidence, 0.95)

    def test_empty_pdf_raises_value_error_not_not_a_resume_error(self) -> None:
        """Empty/scanned PDFs should raise ValueError, not NotAResumeError."""
        with patch.object(ResumePDFParser, "_extract_text", return_value="   "):
            with self.assertRaises(ValueError):
                self.parser.parse(b"%PDF")

    def test_inconclusive_raises_not_a_resume_error(self) -> None:
        """Inconclusive classification should also raise NotAResumeError."""
        inconclusive = {
            "verdict": "inconclusive", "confidence": 0.5,
            "document_type": "unknown", "signals_found": [],
            "reason": "ambiguous", "method": "heuristic",
        }
        with patch.object(ResumePDFParser, "_extract_text", return_value="some text"):
            with patch.object(ResumeClassifier, "classify", return_value=inconclusive):
                with self.assertRaises(NotAResumeError) as ctx:
                    self.parser.parse(b"%PDF")

        self.assertIn("confidently", ctx.exception.reason)

    def test_unsupported_extension_raises_value_error(self) -> None:
        """Unsupported file type should raise ValueError before any processing."""
        with self.assertRaises(ValueError) as ctx:
            self.parser.parse(b"some data", file_extension="rtf")
        self.assertIn("Unsupported file type", str(ctx.exception))


# ---------------------------------------------------------------------------
# Tests: Multi-format file support
# ---------------------------------------------------------------------------

class TestResumePDFParserMultiFormat(unittest.TestCase):

    def setUp(self) -> None:
        self.parser = ResumePDFParser()

    def _fake_parsed(self):
        return {
            "personal_info": {"name": "Jane Doe", "email": "jane@x.com",
                              "phone": "", "location": ""},
            "professional_summary": "Engineer",
            "skills": ["Python", "SQL"],
            "work_experience": [{"title": "Dev", "company": "Corp",
                                  "location": "", "start_date": "2020",
                                  "end_date": "Present", "bullets": []}],
            "education": [], "projects": [],
        }

    def test_parse_docx_routes_to_docx_extractor(self) -> None:
        """DOCX files route through _extract_text_from_docx, not _extract_text."""
        with patch.object(ResumePDFParser, "_extract_text_from_docx",
                          return_value=SAMPLE_RESUME_TEXT) as mock_docx:
            with patch.object(ResumePDFParser, "_parse_with_nvidia",
                              return_value=self._fake_parsed()):
                result = self.parser.parse(b"PK fake docx bytes", file_extension="docx")
                mock_docx.assert_called_once()
        self.assertEqual(result["skills"], ["Python", "SQL"])

    def test_parse_txt_routes_to_txt_extractor(self) -> None:
        """TXT files route through _extract_text_from_txt."""
        with patch.object(ResumePDFParser, "_extract_text_from_txt",
                          return_value=SAMPLE_RESUME_TEXT) as mock_txt:
            with patch.object(ResumePDFParser, "_parse_with_nvidia",
                              return_value=self._fake_parsed()):
                result = self.parser.parse(b"plain text bytes", file_extension="txt")
                mock_txt.assert_called_once()
        self.assertIn("personal_info", result)

    def test_parse_pdf_routes_to_pdf_extractor(self) -> None:
        """PDF files (default) route through _extract_text."""
        with patch.object(ResumePDFParser, "_extract_text",
                          return_value=SAMPLE_RESUME_TEXT) as mock_pdf:
            with patch.object(ResumePDFParser, "_parse_with_nvidia",
                              return_value=self._fake_parsed()):
                self.parser.parse(b"%PDF bytes", file_extension="pdf")
                mock_pdf.assert_called_once()

    def test_parse_unsupported_extension_raises_before_extraction(self) -> None:
        """Unsupported extension raises ValueError immediately."""
        with self.assertRaises(ValueError) as ctx:
            self.parser.parse(b"data", file_extension="rtf")
        self.assertIn("Unsupported file type", str(ctx.exception))

    def test_parse_txt_extension_with_leading_dot(self) -> None:
        """file_extension='.txt' (with dot) should work the same as 'txt'."""
        with patch.object(ResumePDFParser, "_extract_text_from_txt",
                          return_value=SAMPLE_RESUME_TEXT):
            with patch.object(ResumePDFParser, "_parse_with_nvidia",
                              return_value=self._fake_parsed()):
                result = self.parser.parse(b"text", file_extension=".txt")
        self.assertIn("skills", result)


# ---------------------------------------------------------------------------
# Tests: NVIDIA NIM structured parsing
# ---------------------------------------------------------------------------

class TestNIMStructuredParsing(unittest.TestCase):

    def setUp(self) -> None:
        self.parser = ResumePDFParser()

    def _mock_nim_client(self, response_json: str):
        mock_choice = MagicMock()
        mock_choice.message.content = response_json
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response
        return mock_client

    def test_nim_returns_structured_data(self) -> None:
        payload = json.dumps({
            "personal_info": {"name": "Alex Rivera", "email": "alex@test.com",
                              "phone": "555-1234", "location": "NYC"},
            "professional_summary": "Senior engineer with 8 years experience.",
            "skills": ["Python", "Django", "PostgreSQL", "AWS"],
            "work_experience": [
                {"title": "Senior Engineer", "company": "TechCorp",
                 "location": "NYC", "start_date": "2020", "end_date": "Present",
                 "bullets": ["Built REST APIs", "Led 5-person team"]}
            ],
            "education": [
                {"degree": "B.S. Computer Science", "institution": "MIT",
                 "graduation_year": "2016", "gpa": "3.8"}
            ],
            "projects": [],
        })
        mock_client = self._mock_nim_client(payload)
        with patch("openai.OpenAI", return_value=mock_client):
            with patch.dict("os.environ", {"NVIDIA_API_KEY": "test-key"}):
                result = self.parser._parse_with_nvidia(SAMPLE_RESUME_TEXT)

        self.assertIsNotNone(result)
        self.assertEqual(result["skills"], ["Python", "Django", "PostgreSQL", "AWS"])
        self.assertEqual(len(result["work_experience"]), 1)
        self.assertEqual(result["work_experience"][0]["title"], "Senior Engineer")
        self.assertEqual(len(result["work_experience"][0]["bullets"]), 2)

    def test_nim_failure_returns_none(self) -> None:
        """When NIM raises an exception, _parse_with_nvidia returns None."""
        with patch.dict("os.environ", {"NVIDIA_API_KEY": "test-key"}):
            with patch("openai.OpenAI", side_effect=Exception("Connection error")):
                result = self.parser._parse_with_nvidia("some resume text")
        self.assertIsNone(result)

    def test_nim_bad_json_returns_none(self) -> None:
        """Unparseable JSON response returns None."""
        mock_client = self._mock_nim_client("Sorry, I cannot parse this.")
        with patch("openai.OpenAI", return_value=mock_client):
            with patch.dict("os.environ", {"NVIDIA_API_KEY": "test-key"}):
                result = self.parser._parse_with_nvidia("some text")
        self.assertIsNone(result)

    def test_nim_missing_api_key_returns_none(self) -> None:
        """No NVIDIA_API_KEY means _parse_with_nvidia returns None immediately."""
        import os
        with patch.dict("os.environ", {}, clear=True):
            os.environ.pop("NVIDIA_API_KEY", None)
            result = self.parser._parse_with_nvidia("some text")
        self.assertIsNone(result)

    def test_nim_empty_skills_and_experience_returns_none(self) -> None:
        """NIM response with no skills and no work_experience is rejected."""
        payload = json.dumps({
            "personal_info": {"name": "Test", "email": "", "phone": "", "location": ""},
            "professional_summary": "",
            "skills": [],
            "work_experience": [],
            "education": [],
            "projects": [],
        })
        mock_client = self._mock_nim_client(payload)
        with patch("openai.OpenAI", return_value=mock_client):
            with patch.dict("os.environ", {"NVIDIA_API_KEY": "test-key"}):
                result = self.parser._parse_with_nvidia("minimal text")
        self.assertIsNone(result)

    def test_nim_fallback_to_heuristic_on_failure(self) -> None:
        """When NIM returns None, parse() falls back to heuristic parsers."""
        with patch.object(ResumePDFParser, "_extract_text", return_value=SAMPLE_RESUME_TEXT):
            with patch.object(ResumePDFParser, "_parse_with_nvidia", return_value=None):
                result = self.parser.parse(b"%PDF fake resume")
        # Heuristic fallback should still find something from SAMPLE_RESUME_TEXT
        self.assertIn("personal_info", result)
        self.assertIn("skills", result)
        # Heuristic should find at least one skill from the sample text
        self.assertGreater(len(result["skills"]), 0)

    def test_nim_result_used_when_available(self) -> None:
        """When NIM succeeds, its result is used (not heuristic)."""
        nim_result = {
            "personal_info": {"name": "NIM Name", "email": "", "phone": "", "location": ""},
            "professional_summary": "NIM summary",
            "skills": ["NIM Skill A", "NIM Skill B", "NIM Skill C"],
            "work_experience": [{"title": "NIM Title", "company": "NIM Corp",
                                  "location": "", "start_date": "2022",
                                  "end_date": "Present", "bullets": ["Did NIM things"]}],
            "education": [], "projects": [],
        }
        with patch.object(ResumePDFParser, "_extract_text", return_value=SAMPLE_RESUME_TEXT):
            with patch.object(ResumePDFParser, "_parse_with_nvidia", return_value=nim_result):
                result = self.parser.parse(b"%PDF fake resume")
        self.assertEqual(result["skills"], ["NIM Skill A", "NIM Skill B", "NIM Skill C"])
        self.assertEqual(result["personal_info"]["name"], "NIM Name")

    def test_parse_sets_defaults_for_missing_keys(self) -> None:
        """NIM result missing some keys gets filled with safe defaults."""
        partial_nim = {
            "skills": ["Python"],
            "work_experience": [{"title": "Dev", "company": "Co",
                                  "location": "", "start_date": "", "end_date": "", "bullets": []}],
            # missing: personal_info, professional_summary, education, projects
        }
        with patch.object(ResumePDFParser, "_extract_text", return_value=SAMPLE_RESUME_TEXT):
            with patch.object(ResumePDFParser, "_parse_with_nvidia", return_value=partial_nim):
                result = self.parser.parse(b"%PDF")
        self.assertIn("personal_info", result)
        self.assertIn("professional_summary", result)
        self.assertIn("education", result)
        self.assertIn("projects", result)


# ---------------------------------------------------------------------------
# Tests: Flask API integration
# ---------------------------------------------------------------------------

IN_MEMORY = "sqlite:///:memory:"

def _make_sm(tmp_dir):
    from web.settings_manager import SettingsManager
    sm = SettingsManager()
    sm.SETTINGS_PATH = str(Path(tmp_dir) / "settings.json")
    return sm


class TestUploadAPIClassification(unittest.TestCase):

    def setUp(self) -> None:
        from database.database import reset_manager, create_tables
        reset_manager(IN_MEMORY)
        create_tables()

        import tempfile as _tmp
        self.tmp = _tmp.mkdtemp()

        import web.app as app_module
        self._orig_sm = app_module.settings_manager
        app_module.settings_manager = _make_sm(self.tmp)

        from web.app import app
        app.config["TESTING"] = True
        self.client = app.test_client()

    def tearDown(self) -> None:
        import web.app as app_module
        app_module.settings_manager = self._orig_sm
        from database.database import drop_tables
        drop_tables()

    def test_upload_api_returns_422_for_non_resume(self) -> None:
        with patch(
            "pdf_generator.pdf_parser.ResumePDFParser.parse",
            side_effect=NotAResumeError(
                document_type="invoice",
                confidence=0.9,
                reason="Contains billing information",
            ),
        ):
            r = self.client.post(
                "/api/resume/upload",
                data={"file": (io.BytesIO(b"%PDF fake invoice"), "invoice.pdf")},
                content_type="multipart/form-data",
            )

        self.assertEqual(r.status_code, 422)
        data = json.loads(r.data)
        self.assertEqual(data["error"], "not_a_resume")
        self.assertEqual(data["document_type"], "invoice")
        self.assertIn("confidence", data)

    def test_upload_api_returns_422_with_document_type_in_body(self) -> None:
        with patch(
            "pdf_generator.pdf_parser.ResumePDFParser.parse",
            side_effect=NotAResumeError(
                document_type="research_paper",
                confidence=0.88,
                reason="Contains abstract and bibliography",
            ),
        ):
            r = self.client.post(
                "/api/resume/upload",
                data={"file": (io.BytesIO(b"%PDF"), "paper.pdf")},
                content_type="multipart/form-data",
            )

        data = json.loads(r.data)
        self.assertEqual(data["document_type"], "research_paper")

    def test_upload_api_still_accepts_valid_resume(self) -> None:
        fake_content = {
            "personal_info": {"name": "Test User", "email": "t@t.com",
                              "phone": "", "location": ""},
            "professional_summary": "Engineer",
            "skills": ["Python", "Django"],
            "work_experience": [],
            "education": [],
            "projects": [],
        }
        with patch(
            "pdf_generator.pdf_parser.ResumePDFParser.parse",
            return_value=fake_content,
        ):
            r = self.client.post(
                "/api/resume/upload",
                data={"file": (io.BytesIO(b"%PDF resume"), "resume.pdf"),
                      "name": "Test Resume"},
                content_type="multipart/form-data",
            )

        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertTrue(data["ok"])

    def test_upload_api_422_does_not_create_db_row(self) -> None:
        from database.database import get_db
        from database.models import MasterResume

        with patch(
            "pdf_generator.pdf_parser.ResumePDFParser.parse",
            side_effect=NotAResumeError("invoice", 0.9, "billing"),
        ):
            self.client.post(
                "/api/resume/upload",
                data={"file": (io.BytesIO(b"%PDF"), "inv.pdf")},
                content_type="multipart/form-data",
            )

        with get_db() as db:
            count = db.query(MasterResume).filter(
                MasterResume.is_sample == False
            ).count()
        self.assertEqual(count, 0)

    def test_upload_api_error_field_is_not_a_resume_string(self) -> None:
        with patch(
            "pdf_generator.pdf_parser.ResumePDFParser.parse",
            side_effect=NotAResumeError("contract", 0.85, "Legal agreement text"),
        ):
            r = self.client.post(
                "/api/resume/upload",
                data={"file": (io.BytesIO(b"%PDF"), "contract.pdf")},
                content_type="multipart/form-data",
            )

        data = json.loads(r.data)
        self.assertEqual(data["error"], "not_a_resume")


if __name__ == "__main__":
    unittest.main()
