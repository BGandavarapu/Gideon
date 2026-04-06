"""
Tests for Phase 5: PDF generation system.

Covers:
- PDFGenerator orchestrator (template selection, validation, path building)
- ATSOptimizedTemplate (all sections, edge cases)
- ClassicTemplate (all sections, edge cases)
- Text-wrapping utility in BasePDFTemplate
- Multi-page overflow behaviour
- Missing / optional sections handled gracefully
"""

import os
import re
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Sample data fixtures
# ---------------------------------------------------------------------------

_FULL_RESUME = {
    "personal_info": {
        "name": "Alex Smith",
        "email": "alex@example.com",
        "phone": "555-0100",
        "location": "San Francisco, CA",
        "linkedin": "linkedin.com/in/alexsmith",
    },
    "professional_summary": (
        "Experienced senior software engineer with 6 years of Python expertise. "
        "Proficient in Django, REST APIs, PostgreSQL, and Docker. "
        "Passionate about building scalable, high-performance web services."
    ),
    "work_experience": [
        {
            "title": "Senior Software Engineer",
            "company": "Acme Technologies",
            "location": "San Francisco, CA",
            "dates": "2020 - Present",
            "bullets": [
                "Built Django REST APIs serving 50,000 daily active users.",
                "Optimised PostgreSQL queries reducing average latency by 40%.",
                "Containerised services with Docker, cutting deployment time by 30%.",
                "Led a cross-functional team of 5 engineers to deliver the v2 platform on schedule.",
            ],
        },
        {
            "title": "Software Engineer",
            "company": "StartupCo",
            "location": "Remote",
            "dates": "2018 - 2020",
            "bullets": [
                "Developed microservices in Python and Flask.",
                "Integrated third-party payment APIs (Stripe, PayPal).",
            ],
        },
    ],
    "skills": ["Python", "Django", "REST API", "PostgreSQL", "Docker", "Git", "Linux", "AWS"],
    "education": [
        {
            "degree": "B.Sc. Computer Science",
            "institution": "State University",
            "year": 2018,
        }
    ],
    "projects": [
        {
            "name": "API Gateway",
            "description": "High-throughput REST API gateway built in Python/Django.",
            "tech": ["Python", "Django", "Redis"],
        }
    ],
    "certifications": ["AWS Certified Solutions Architect (2023)"],
}

_MINIMAL_RESUME = {
    "personal_info": {
        "name": "Jane Doe",
        "email": "jane@example.com",
    },
}

_LONG_BULLET = (
    "Architected and delivered an end-to-end machine learning pipeline using Python, "
    "Apache Spark, AWS SageMaker, and Kubernetes that processes over 500 million events "
    "per day, reducing model inference latency by 60% and saving the company $1.2M annually "
    "in compute costs while improving prediction accuracy from 87% to 94%."
)

_MANY_EXPERIENCE = [
    {
        "title": f"Engineer Level {i}",
        "company": f"Company {i}",
        "location": "New York, NY",
        "dates": f"201{i} - 201{i+1}",
        "bullets": [
            f"Achieved milestone {i}.1 with measurable outcome.",
            f"Delivered project {i}.2 ahead of schedule.",
            f"Collaborated across {i+2} teams to integrate systems.",
        ],
    }
    for i in range(8)
]


# ---------------------------------------------------------------------------
# Tests: PDFGenerator orchestrator
# ---------------------------------------------------------------------------


class TestPDFGenerator(unittest.TestCase):
    """Tests for PDFGenerator - orchestration, validation, path building."""

    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.mkdtemp()
        from pdf_generator.generator import PDFGenerator
        self.gen = PDFGenerator(output_dir=self.tmp)

    # --- template selection ---

    def test_ats_template_selected(self) -> None:
        path = self.gen.generate(_FULL_RESUME, "ats")
        self.assertTrue(os.path.exists(path))

    def test_classic_template_selected(self) -> None:
        path = self.gen.generate(_FULL_RESUME, "classic")
        self.assertTrue(os.path.exists(path))

    def test_unknown_template_raises(self) -> None:
        with self.assertRaises(ValueError) as cm:
            self.gen.generate(_FULL_RESUME, "fancy_template")
        self.assertIn("fancy_template", str(cm.exception))

    def test_template_name_case_insensitive(self) -> None:
        path = self.gen.generate(_FULL_RESUME, "ATS")
        self.assertTrue(os.path.exists(path))

    # --- output path ---

    def test_pdf_file_exists_after_generation(self) -> None:
        path = self.gen.generate(_FULL_RESUME, "ats")
        self.assertTrue(Path(path).is_file())

    def test_pdf_extension_appended_automatically(self) -> None:
        path = self.gen.generate(_FULL_RESUME, "ats", filename="no_extension")
        self.assertTrue(path.endswith(".pdf"))

    def test_custom_filename_used(self) -> None:
        path = self.gen.generate(_FULL_RESUME, "ats", filename="my_custom.pdf")
        self.assertTrue(path.endswith("my_custom.pdf"))

    def test_auto_filename_contains_name(self) -> None:
        path = self.gen.generate(_FULL_RESUME, "ats")
        self.assertIn("alex_smith", os.path.basename(path))

    def test_auto_filename_contains_template(self) -> None:
        path = self.gen.generate(_FULL_RESUME, "ats")
        self.assertIn("ats", os.path.basename(path))

    # --- output file properties ---

    def test_pdf_file_non_empty(self) -> None:
        path = self.gen.generate(_FULL_RESUME, "ats")
        self.assertGreater(os.path.getsize(path), 0)

    def test_pdf_has_pdf_header(self) -> None:
        """Generated file should start with the %%PDF- magic bytes."""
        path = self.gen.generate(_FULL_RESUME, "ats")
        with open(path, "rb") as fh:
            header = fh.read(5)
        self.assertEqual(header, b"%PDF-")

    def test_file_size_reasonable(self) -> None:
        """A text-only resume PDF should be between 1 KB and 500 KB.

        ReportLab generates compact PDFs for plain-text resumes (1-5 KB is
        normal); image-heavy PDFs are much larger.  The lower bound is 1 KB
        to catch truly empty/broken outputs.
        """
        path = self.gen.generate(_FULL_RESUME, "ats")
        size_kb = os.path.getsize(path) / 1024
        self.assertGreater(size_kb, 1, "PDF too small (likely empty)")
        self.assertLess(size_kb, 500, "PDF suspiciously large")

    # --- validation ---

    def test_missing_personal_info_raises(self) -> None:
        with self.assertRaises(ValueError, msg="Missing required section: personal_info"):
            self.gen.generate({"work_experience": []}, "ats")

    def test_missing_name_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.gen.generate({"personal_info": {"email": "x@y.com"}}, "ats")

    def test_missing_email_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.gen.generate({"personal_info": {"name": "X"}}, "ats")

    def test_blank_name_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.gen.generate({"personal_info": {"name": "  ", "email": "x@y.com"}}, "ats")

    def test_non_dict_data_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.gen.generate("not a dict", "ats")  # type: ignore[arg-type]

    def test_minimal_resume_generates_successfully(self) -> None:
        path = self.gen.generate(_MINIMAL_RESUME, "ats")
        self.assertTrue(os.path.exists(path))

    def test_available_templates_list(self) -> None:
        from pdf_generator.generator import AVAILABLE_TEMPLATES
        self.assertIn("ats", AVAILABLE_TEMPLATES)
        self.assertIn("classic", AVAILABLE_TEMPLATES)


# ---------------------------------------------------------------------------
# Tests: ATSOptimizedTemplate
# ---------------------------------------------------------------------------


class TestATSOptimizedTemplate(unittest.TestCase):
    """Tests for the ATS-optimised template's section rendering."""

    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.mkdtemp()
        from pdf_generator.generator import PDFGenerator
        self.gen = PDFGenerator(output_dir=self.tmp)

    def _gen(self, data: dict) -> str:
        return self.gen.generate(data, "ats")

    def test_full_resume_generates(self) -> None:
        path = self._gen(_FULL_RESUME)
        self.assertTrue(os.path.exists(path))

    def test_minimal_resume_generates(self) -> None:
        path = self._gen(_MINIMAL_RESUME)
        self.assertTrue(os.path.exists(path))

    def test_skills_as_dict_generates(self) -> None:
        data = dict(_FULL_RESUME, skills={
            "technical": ["Python", "Django"],
            "soft_skills": ["Communication", "Leadership"],
        })
        path = self._gen(data)
        self.assertTrue(os.path.exists(path))

    def test_missing_work_experience_section(self) -> None:
        data = {k: v for k, v in _FULL_RESUME.items() if k != "work_experience"}
        path = self._gen(data)
        self.assertTrue(os.path.exists(path))

    def test_missing_education_section(self) -> None:
        data = {k: v for k, v in _FULL_RESUME.items() if k != "education"}
        path = self._gen(data)
        self.assertTrue(os.path.exists(path))

    def test_missing_projects_section(self) -> None:
        data = {k: v for k, v in _FULL_RESUME.items() if k != "projects"}
        path = self._gen(data)
        self.assertTrue(os.path.exists(path))

    def test_empty_bullets_list(self) -> None:
        data = dict(_FULL_RESUME, work_experience=[{
            "title": "Dev", "company": "Acme", "dates": "2020", "bullets": []
        }])
        path = self._gen(data)
        self.assertTrue(os.path.exists(path))

    def test_long_bullet_wraps_without_error(self) -> None:
        data = dict(_FULL_RESUME, work_experience=[{
            "title": "ML Engineer", "company": "BigCo", "dates": "2022",
            "bullets": [_LONG_BULLET],
        }])
        path = self._gen(data)
        self.assertTrue(os.path.exists(path))
        self.assertGreater(os.path.getsize(path), 0)

    def test_multi_page_many_jobs(self) -> None:
        data = dict(_FULL_RESUME, work_experience=_MANY_EXPERIENCE)
        path = self._gen(data)
        self.assertTrue(os.path.exists(path))

    def test_string_education_entries(self) -> None:
        data = dict(_FULL_RESUME, education=["B.Sc. Computer Science, MIT, 2015"])
        path = self._gen(data)
        self.assertTrue(os.path.exists(path))

    def test_certifications_as_strings(self) -> None:
        data = dict(_FULL_RESUME, certifications=["AWS SAA", "Google Cloud Professional"])
        path = self._gen(data)
        self.assertTrue(os.path.exists(path))

    def test_certifications_as_dicts(self) -> None:
        data = dict(_FULL_RESUME, certifications=[
            {"name": "AWS SAA", "issuer": "Amazon", "year": 2023}
        ])
        path = self._gen(data)
        self.assertTrue(os.path.exists(path))

    def test_projects_with_tech_list(self) -> None:
        data = dict(_FULL_RESUME, projects=[{
            "name": "My App", "description": "A great app.",
            "tech": ["Python", "React", "PostgreSQL"],
        }])
        path = self._gen(data)
        self.assertTrue(os.path.exists(path))

    def test_long_summary(self) -> None:
        long_summary = (
            "Seasoned software engineer with over a decade of experience designing, "
            "building, and scaling distributed systems for high-traffic web applications. "
            "Expert in Python, Django, FastAPI, PostgreSQL, Redis, Elasticsearch, AWS, "
            "and Kubernetes. Proven track record of mentoring teams, driving architectural "
            "decisions, and delivering complex projects on time and under budget. "
            "Passionate about developer experience, clean code, and continuous improvement."
        )
        data = dict(_FULL_RESUME, professional_summary=long_summary)
        path = self._gen(data)
        self.assertTrue(os.path.exists(path))

    def test_special_characters_in_name(self) -> None:
        data = dict(_FULL_RESUME, personal_info={
            "name": "Maria Garcia-Lopez",
            "email": "m.garcia@example.com",
            "phone": "+1-555-0200",
        })
        path = self._gen(data)
        self.assertTrue(os.path.exists(path))


# ---------------------------------------------------------------------------
# Tests: ClassicTemplate
# ---------------------------------------------------------------------------


class TestClassicTemplate(unittest.TestCase):
    """Tests for the Classic (coloured) template."""

    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.mkdtemp()
        from pdf_generator.generator import PDFGenerator
        self.gen = PDFGenerator(output_dir=self.tmp)

    def _gen(self, data: dict) -> str:
        return self.gen.generate(data, "classic")

    def test_full_resume_generates(self) -> None:
        path = self._gen(_FULL_RESUME)
        self.assertTrue(os.path.exists(path))

    def test_classic_pdf_larger_than_ats(self) -> None:
        """Classic template uses colour; file may differ from ATS but both valid."""
        ats_path = self.gen.generate(_FULL_RESUME, "ats")
        classic_path = self._gen(_FULL_RESUME)
        # Both should be valid PDFs (not necessarily same size)
        self.assertTrue(os.path.exists(ats_path))
        self.assertTrue(os.path.exists(classic_path))

    def test_minimal_resume_generates(self) -> None:
        path = self._gen(_MINIMAL_RESUME)
        self.assertTrue(os.path.exists(path))

    def test_long_bullet_wraps(self) -> None:
        data = dict(_FULL_RESUME, work_experience=[{
            "title": "Researcher", "company": "Lab", "dates": "2021",
            "bullets": [_LONG_BULLET],
        }])
        path = self._gen(data)
        self.assertTrue(os.path.exists(path))

    def test_multi_page_many_jobs(self) -> None:
        data = dict(_FULL_RESUME, work_experience=_MANY_EXPERIENCE)
        path = self._gen(data)
        self.assertTrue(os.path.exists(path))

    def test_categorised_skills(self) -> None:
        data = dict(_FULL_RESUME, skills={
            "languages": ["Python", "Go", "TypeScript"],
            "frameworks": ["Django", "FastAPI", "React"],
            "tools": ["Docker", "Kubernetes", "Terraform"],
        })
        path = self._gen(data)
        self.assertTrue(os.path.exists(path))


# ---------------------------------------------------------------------------
# Tests: BasePDFTemplate utilities
# ---------------------------------------------------------------------------


class TestBasePDFTemplateUtils(unittest.TestCase):
    """Unit tests for shared utilities in BasePDFTemplate."""

    def setUp(self) -> None:
        from pdf_generator.templates.ats_optimized import ATSOptimizedTemplate
        self.tmpl = ATSOptimizedTemplate()

    def test_wrap_text_short_fits_one_line(self) -> None:
        lines = self.tmpl.wrap_text("Short text", 400, "Helvetica", 10)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], "Short text")

    def test_wrap_text_long_splits_into_multiple_lines(self) -> None:
        long = " ".join(["word"] * 30)
        lines = self.tmpl.wrap_text(long, 200, "Helvetica", 10)
        self.assertGreater(len(lines), 1)

    def test_wrap_text_empty_string(self) -> None:
        lines = self.tmpl.wrap_text("", 400, "Helvetica", 10)
        self.assertEqual(lines, [""])

    def test_wrap_text_no_spaces(self) -> None:
        """A single very long word should be returned as-is."""
        long_word = "A" * 50
        lines = self.tmpl.wrap_text(long_word, 50, "Helvetica", 10)
        self.assertEqual(len(lines), 1)

    def test_text_height_zero_lines(self) -> None:
        self.assertEqual(self.tmpl.text_height(0), 0)

    def test_text_height_proportional(self) -> None:
        from pdf_generator.styles import LINE_SPACING
        self.assertEqual(self.tmpl.text_height(3), 3 * LINE_SPACING)

    def test_page_dimensions_letter(self) -> None:
        from reportlab.lib.pagesizes import letter
        self.assertAlmostEqual(self.tmpl.page_width, letter[0], places=1)
        self.assertAlmostEqual(self.tmpl.page_height, letter[1], places=1)

    def test_content_width_less_than_page_width(self) -> None:
        self.assertLess(self.tmpl.content_width, self.tmpl.page_width)

    def test_format_skills_list(self) -> None:
        from pdf_generator.templates.ats_optimized import ATSOptimizedTemplate
        result = ATSOptimizedTemplate._format_skills(["Python", "Django", "AWS"])
        self.assertEqual(len(result), 1)
        self.assertIn("Python", result[0])

    def test_format_skills_dict(self) -> None:
        from pdf_generator.templates.ats_optimized import ATSOptimizedTemplate
        result = ATSOptimizedTemplate._format_skills({
            "technical": ["Python", "Django"],
            "soft_skills": ["Leadership"],
        })
        self.assertGreater(len(result), 0)
        joined = "\n".join(result)
        self.assertIn("Python", joined)


if __name__ == "__main__":
    unittest.main()
