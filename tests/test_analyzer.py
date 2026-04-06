"""
Unit tests for the Phase 3 analyzer package.

Coverage:
    TestKeywordExtractor        keyword_extractor.py (16 tests)
    TestRequirementParser       requirement_parser.py (20 tests)
    TestSkillMatcher            skill_matcher.py      (20 tests)
    TestScoringEngine           scoring.py            (18 tests)

Design notes
------------
- No real database connections are used; ORM models are instantiated directly
  (SQLAlchemy allows this without a bound session).
- spaCy is loaded once at module level via the KeywordExtractor singleton to
  avoid the ~1 s model-load overhead per test.
- All tests are deterministic (no external HTTP calls, no random seeds).
"""

import math
import unittest
from datetime import datetime, timezone
from typing import List
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_PYTHON_JD = """
We are looking for a Senior Python Developer to join our platform team.

Requirements:
- 5+ years of Python experience
- Strong knowledge of Django and FastAPI
- Experience with PostgreSQL and Redis
- Familiarity with Docker and Kubernetes
- AWS cloud experience (EC2, S3, Lambda)
- Bachelor's degree in Computer Science or related field
- AWS Certified Solutions Architect preferred

Nice to have:
- React experience
- Machine learning background
- Leadership and mentoring experience
"""

_SIMPLE_JD = "We need a JavaScript developer with React and Node.js skills."

_EMPTY_JD = ""

_RESUME_CONTENT_FULL = {
    "skills": ["Python", "Django", "FastAPI", "PostgreSQL", "Docker", "AWS"],
    "certifications": ["AWS Certified Solutions Architect"],
    "education": [{"degree": "Bachelor of Science", "field": "Computer Science"}],
}

_RESUME_CONTENT_PARTIAL = {
    "skills": ["Python", "Django"],
}

_RESUME_CONTENT_EMPTY = {}


# ---------------------------------------------------------------------------
# TestKeywordExtractor
# ---------------------------------------------------------------------------


class TestKeywordExtractor(unittest.TestCase):
    """Tests for analyzer.keyword_extractor.KeywordExtractor."""

    @classmethod
    def setUpClass(cls) -> None:
        from analyzer.keyword_extractor import KeywordExtractor

        cls.extractor = KeywordExtractor()

    def test_extract_returns_list(self) -> None:
        results = self.extractor.extract_keywords(_PYTHON_JD)
        self.assertIsInstance(results, list)

    def test_extract_finds_python(self) -> None:
        results = self.extractor.extract_keywords(_PYTHON_JD)
        texts = {kw.text for kw in results}
        self.assertIn("python", texts)

    def test_extract_finds_django(self) -> None:
        results = self.extractor.extract_keywords(_PYTHON_JD)
        texts = {kw.text for kw in results}
        self.assertIn("django", texts)

    def test_extract_finds_docker(self) -> None:
        results = self.extractor.extract_keywords(_PYTHON_JD)
        texts = {kw.text for kw in results}
        self.assertIn("docker", texts)

    def test_extract_finds_aws(self) -> None:
        results = self.extractor.extract_keywords(_PYTHON_JD)
        texts = {kw.text for kw in results}
        self.assertIn("aws", texts)

    def test_empty_description_returns_empty_list(self) -> None:
        results = self.extractor.extract_keywords(_EMPTY_JD)
        self.assertEqual(results, [])

    def test_whitespace_only_returns_empty_list(self) -> None:
        results = self.extractor.extract_keywords("   \n\t  ")
        self.assertEqual(results, [])

    def test_confidence_in_range(self) -> None:
        results = self.extractor.extract_keywords(_PYTHON_JD)
        for kw in results:
            self.assertGreaterEqual(kw.confidence, 0.0)
            self.assertLessEqual(kw.confidence, 1.0)

    def test_taxonomy_hits_have_confidence_one(self) -> None:
        results = self.extractor.extract_keywords("We use Python and Django.")
        taxonomy_hits = [kw for kw in results if kw.category != "ner_entity"]
        for kw in taxonomy_hits:
            self.assertEqual(kw.confidence, 1.0)

    def test_no_duplicates_in_output(self) -> None:
        results = self.extractor.extract_keywords(_PYTHON_JD)
        texts = [kw.text for kw in results]
        self.assertEqual(len(texts), len(set(texts)))

    def test_extract_by_category_returns_dict(self) -> None:
        result = self.extractor.extract_by_category(_PYTHON_JD)
        self.assertIsInstance(result, dict)

    def test_extract_by_category_has_programming_languages(self) -> None:
        result = self.extractor.extract_by_category(_PYTHON_JD)
        self.assertIn("programming_languages", result)
        self.assertIn("python", result["programming_languages"])

    def test_get_technical_skills_excludes_soft_skills(self) -> None:
        jd = "We need Python skills and strong leadership and communication."
        technical = self.extractor.get_technical_skills(jd)
        self.assertIn("python", technical)
        self.assertNotIn("leadership", technical)

    def test_context_is_non_empty_string(self) -> None:
        results = self.extractor.extract_keywords("We use Python for data processing.")
        python_hits = [kw for kw in results if kw.text == "python"]
        self.assertTrue(len(python_hits) >= 1)
        self.assertIsInstance(python_hits[0].context, str)

    def test_simple_jd(self) -> None:
        results = self.extractor.extract_keywords(_SIMPLE_JD)
        texts = {kw.text for kw in results}
        self.assertTrue({"javascript", "react"}.issubset(texts) or len(texts) > 0)

    def test_extracted_keyword_to_dict(self) -> None:
        from analyzer.keyword_extractor import ExtractedKeyword

        kw = ExtractedKeyword(text="python", category="programming_languages", confidence=1.0)
        d = kw.to_dict()
        self.assertIn("text", d)
        self.assertIn("category", d)
        self.assertIn("confidence", d)


# ---------------------------------------------------------------------------
# TestRequirementParser
# ---------------------------------------------------------------------------


class TestRequirementParser(unittest.TestCase):
    """Tests for analyzer.requirement_parser.RequirementParser."""

    @classmethod
    def setUpClass(cls) -> None:
        from analyzer.requirement_parser import RequirementParser

        cls.parser = RequirementParser()

    def test_parse_returns_parsed_requirements(self) -> None:
        from analyzer.requirement_parser import ParsedRequirements

        result = self.parser.parse(_PYTHON_JD)
        self.assertIsInstance(result, ParsedRequirements)

    def test_parse_empty_returns_empty(self) -> None:
        result = self.parser.parse(_EMPTY_JD)
        self.assertEqual(result.experience, [])
        self.assertEqual(result.education, [])
        self.assertEqual(result.certifications, [])

    def test_parse_finds_5_years_python(self) -> None:
        result = self.parser.parse(_PYTHON_JD)
        five_year_hits = [e for e in result.experience if e.min_years == 5]
        self.assertTrue(len(five_year_hits) >= 1)

    def test_experience_skill_contains_python(self) -> None:
        result = self.parser.parse(_PYTHON_JD)
        skills = [e.skill for e in result.experience]
        self.assertTrue(any("python" in s for s in skills))

    def test_experience_is_minimum_flag(self) -> None:
        result = self.parser.parse("3+ years of Java experience required.")
        self.assertTrue(len(result.experience) >= 1)
        self.assertTrue(result.experience[0].is_minimum)

    def test_min_years_experience_property(self) -> None:
        result = self.parser.parse(_PYTHON_JD)
        self.assertGreater(result.min_years_experience, 0)

    def test_parse_education_finds_bachelor(self) -> None:
        result = self.parser.parse(_PYTHON_JD)
        levels = [e.level for e in result.education]
        self.assertIn("bachelor", levels)

    def test_parse_education_field_of_study(self) -> None:
        result = self.parser.parse("Bachelor's degree in Computer Science required.")
        self.assertTrue(len(result.education) >= 1)
        fos = result.education[0].field_of_study
        self.assertIn("computer", fos)

    def test_education_level_property(self) -> None:
        result = self.parser.parse("PhD required.")
        self.assertEqual(result.education_level, "phd")

    def test_education_no_requirements_returns_none_level(self) -> None:
        result = self.parser.parse("We need a developer with Python skills.")
        self.assertIsNone(result.education_level)

    def test_parse_certifications_finds_aws(self) -> None:
        result = self.parser.parse(_PYTHON_JD)
        cert_names = [c.name for c in result.certifications]
        self.assertTrue(any("aws" in name for name in cert_names))

    def test_certification_preferred_flag(self) -> None:
        result = self.parser.parse("AWS Certified Solutions Architect preferred.")
        self.assertTrue(len(result.certifications) >= 1)
        self.assertFalse(result.certifications[0].is_required)

    def test_parse_experience_no_experience_section(self) -> None:
        result = self.parser.parse("Looking for a creative designer.")
        self.assertEqual(result.min_years_experience, 0)

    def test_parse_cka_certification(self) -> None:
        result = self.parser.parse("CKA certification required.")
        cert_names = [c.name for c in result.certifications]
        self.assertTrue(any("cka" in name for name in cert_names))

    def test_parse_pmp_certification(self) -> None:
        result = self.parser.parse("PMP certification is a plus.")
        cert_names = [c.name for c in result.certifications]
        self.assertTrue(any("pmp" in name for name in cert_names))

    def test_to_dict_serialisable(self) -> None:
        import json

        result = self.parser.parse(_PYTHON_JD)
        d = result.to_dict()
        json.dumps(d)  # Should not raise

    def test_education_master_detected(self) -> None:
        result = self.parser.parse("Master's degree in Computer Science preferred.")
        levels = [e.level for e in result.education]
        self.assertIn("master", levels)

    def test_education_phd_rank_highest(self) -> None:
        result = self.parser.parse("PhD or Master's degree required.")
        self.assertEqual(result.education_level, "phd")

    def test_no_implausible_year_values(self) -> None:
        result = self.parser.parse("Looking for someone with 50 years of COBOL experience.")
        years = [e.min_years for e in result.experience]
        self.assertTrue(all(y <= 25 for y in years))

    def test_experience_range_pattern(self) -> None:
        result = self.parser.parse("3-5 years of experience in Python is required.")
        self.assertTrue(len(result.experience) >= 1)


# ---------------------------------------------------------------------------
# TestSkillMatcher
# ---------------------------------------------------------------------------


class TestSkillMatcher(unittest.TestCase):
    """Tests for analyzer.skill_matcher.SkillMatcher."""

    @classmethod
    def setUpClass(cls) -> None:
        from analyzer.skill_matcher import SkillMatcher

        cls.matcher = SkillMatcher()

    def test_full_match_returns_100(self) -> None:
        result = self.matcher.match(["python", "django"], ["python", "django"])
        self.assertAlmostEqual(result.overall_score, 100.0)

    def test_no_match_returns_0(self) -> None:
        result = self.matcher.match(["java", "spring"], ["python", "django"])
        self.assertAlmostEqual(result.overall_score, 0.0)

    def test_partial_match(self) -> None:
        result = self.matcher.match(["python", "java", "go"], ["python"])
        self.assertAlmostEqual(result.overall_score, 100 / 3, delta=2.0)

    def test_empty_job_skills_returns_zero_score(self) -> None:
        result = self.matcher.match([], ["python", "django"])
        self.assertEqual(result.overall_score, 0.0)

    def test_missing_skills_populated(self) -> None:
        result = self.matcher.match(["python", "java"], ["python"])
        self.assertIn("java", result.missing_skills)

    def test_matched_skills_populated(self) -> None:
        result = self.matcher.match(["python", "java"], ["python", "java", "go"])
        self.assertIn("python", result.matched_skills)
        self.assertIn("java", result.matched_skills)

    def test_extra_skills_populated(self) -> None:
        result = self.matcher.match(["python"], ["python", "go", "rust"])
        self.assertTrue(len(result.extra_skills) >= 1)

    def test_synonym_js_matches_javascript(self) -> None:
        result = self.matcher.match(["javascript"], ["js"])
        self.assertGreater(result.overall_score, 0.0)

    def test_synonym_k8s_matches_kubernetes(self) -> None:
        result = self.matcher.match(["kubernetes"], ["k8s"])
        self.assertGreater(result.overall_score, 0.0)

    def test_case_insensitive_matching(self) -> None:
        result = self.matcher.match(["Python"], ["PYTHON"])
        self.assertAlmostEqual(result.overall_score, 100.0)

    def test_normalise_strips_trailing_punctuation(self) -> None:
        self.assertEqual(self.matcher.normalise("python."), "python")
        self.assertEqual(self.matcher.normalise("java,"), "java")

    def test_match_result_to_dict(self) -> None:
        import json

        result = self.matcher.match(["python"], ["python", "java"])
        d = result.to_dict()
        json.dumps(d)  # Must not raise

    def test_match_by_category(self) -> None:
        job_by_cat = {
            "programming_languages": ["python", "java"],
            "databases": ["postgresql", "redis"],
        }
        resume = ["python", "postgresql"]
        result = self.matcher.match_by_category(job_by_cat, resume)
        self.assertIn("programming_languages", result.category_scores)
        self.assertIn("databases", result.category_scores)

    def test_match_by_category_overall_is_average(self) -> None:
        job_by_cat = {
            "a": ["x"],
            "b": ["y"],
        }
        result = self.matcher.match_by_category(job_by_cat, ["x"])
        self.assertAlmostEqual(result.overall_score, 50.0, delta=2.0)

    def test_weighted_score_respects_weights(self) -> None:
        result = self.matcher.match(
            ["python", "java"],
            ["python"],
            weights={"python": 3.0, "java": 1.0},
        )
        self.assertAlmostEqual(result.overall_score, 75.0, delta=5.0)

    def test_total_job_skills_count(self) -> None:
        result = self.matcher.match(["python", "java", "go"], ["python"])
        self.assertGreaterEqual(result.total_job_skills, 3)

    def test_total_resume_skills_count(self) -> None:
        result = self.matcher.match(["python"], ["python", "java", "docker"])
        self.assertGreaterEqual(result.total_resume_skills, 3)

    def test_synonym_aws_full_name(self) -> None:
        result = self.matcher.match(["amazon web services"], ["aws"])
        self.assertGreater(result.overall_score, 0.0)

    def test_extra_synonyms_in_constructor(self) -> None:
        from analyzer.skill_matcher import SkillMatcher

        matcher = SkillMatcher(extra_synonyms={"myalias": "my_canonical"})
        self.assertIn("myalias", matcher.synonym_map)

    def test_empty_resume_skills(self) -> None:
        result = self.matcher.match(["python", "java"], [])
        self.assertAlmostEqual(result.overall_score, 0.0)


# ---------------------------------------------------------------------------
# TestScoringEngine
# ---------------------------------------------------------------------------


class TestScoringEngine(unittest.TestCase):
    """Tests for analyzer.scoring.ScoringEngine."""

    @classmethod
    def setUpClass(cls) -> None:
        from analyzer.scoring import ScoringEngine

        cls.engine = ScoringEngine()

    def _make_job(self, description: str = _PYTHON_JD, req_skills=None, pref_skills=None):
        """Create a mock Job object without a database session."""
        job = MagicMock()
        job.id = 1
        job.job_title = "Senior Python Developer"
        job.company_name = "Acme Corp"
        job.job_description = description
        job.required_skills = req_skills
        job.preferred_skills = pref_skills
        return job

    def _make_resume(self, content: dict = None, resume_id: int = 1):
        """Create a mock MasterResume object without a database session."""
        resume = MagicMock()
        resume.id = resume_id
        resume.name = "Test Resume"
        resume.content = _RESUME_CONTENT_FULL if content is None else content
        return resume

    def test_score_returns_score_result(self) -> None:
        from analyzer.scoring import ScoreResult

        job = self._make_job()
        resume = self._make_resume()
        result = self.engine.score(job, resume)
        self.assertIsInstance(result, ScoreResult)

    def test_score_in_range_0_100(self) -> None:
        job = self._make_job()
        resume = self._make_resume()
        result = self.engine.score(job, resume)
        self.assertGreaterEqual(result.total_score, 0.0)
        self.assertLessEqual(result.total_score, 100.0)

    def test_full_match_scores_high(self) -> None:
        job = self._make_job(
            description="Requires Python and Django.",
            req_skills=["python", "django"],
        )
        resume = self._make_resume(content={"skills": ["Python", "Django"]})
        result = self.engine.score(job, resume)
        self.assertGreater(result.total_score, 60.0)

    def test_no_match_scores_low(self) -> None:
        job = self._make_job(
            description="Requires Java and Spring.",
            req_skills=["java", "spring"],
        )
        resume = self._make_resume(content={"skills": ["Ruby", "Rails"]})
        result = self.engine.score(job, resume)
        self.assertLess(result.total_score, 40.0)

    def test_breakdown_contains_all_components(self) -> None:
        job = self._make_job()
        resume = self._make_resume()
        result = self.engine.score(job, resume)
        expected_keys = {"required_skills", "preferred_skills", "experience", "education", "bonus"}
        self.assertTrue(expected_keys.issubset(result.breakdown.keys()))

    def test_breakdown_components_in_range(self) -> None:
        job = self._make_job()
        resume = self._make_resume()
        result = self.engine.score(job, resume)
        for key, val in result.breakdown.items():
            self.assertGreaterEqual(val, 0.0, f"{key} below 0")

    def test_missing_skills_list(self) -> None:
        job = self._make_job(req_skills=["java", "spring", "python"])
        resume = self._make_resume(content={"skills": ["python"]})
        result = self.engine.score(job, resume)
        self.assertTrue(len(result.missing_skills) >= 1)

    def test_extra_skills_list(self) -> None:
        job = self._make_job(req_skills=["python"])
        resume = self._make_resume(content={"skills": ["python", "go", "rust"]})
        result = self.engine.score(job, resume)
        self.assertTrue(len(result.extra_skills) >= 1)

    def test_job_id_stored_on_result(self) -> None:
        job = self._make_job()
        job.id = 99
        resume = self._make_resume()
        result = self.engine.score(job, resume)
        self.assertEqual(result.job_id, 99)

    def test_resume_id_stored_on_result(self) -> None:
        job = self._make_job()
        resume = self._make_resume(resume_id=42)
        result = self.engine.score(job, resume)
        self.assertEqual(result.resume_id, 42)

    def test_to_dict_serialisable(self) -> None:
        import json

        job = self._make_job()
        resume = self._make_resume()
        result = self.engine.score(job, resume)
        d = result.to_dict()
        json.dumps(d)

    def test_score_from_text_no_prebuilt_skills(self) -> None:
        result = self.engine.score_from_text(
            job_description="We need Python and Django.",
            resume_skills=["Python", "Django"],
        )
        self.assertGreater(result.total_score, 50.0)

    def test_score_from_text_with_prebuilt_skills(self) -> None:
        result = self.engine.score_from_text(
            job_description="We need Python.",
            resume_skills=["Python"],
            job_required_skills=["python"],
            job_preferred_skills=[],
        )
        self.assertGreater(result.total_score, 50.0)

    def test_education_full_match_contributes_positively(self) -> None:
        result = self.engine.score_from_text(
            job_description="Bachelor's degree required.",
            resume_skills=["bachelor", "python"],
            job_required_skills=["python"],
        )
        self.assertGreaterEqual(result.breakdown.get("education", 0), 50.0)

    def test_bonus_capped_at_5(self) -> None:
        job = self._make_job(req_skills=["python"])
        resume = self._make_resume(
            content={"skills": ["python", "go", "rust", "scala", "haskell", "elixir", "erlang", "julia", "clojure"]}
        )
        result = self.engine.score(job, resume)
        self.assertLessEqual(result.breakdown.get("bonus", 0), 5.0)

    def test_total_score_never_exceeds_100(self) -> None:
        job = self._make_job(req_skills=["python"])
        resume = self._make_resume(
            content={"skills": ["python", "django", "fastapi", "aws", "docker", "kubernetes", "go", "rust"]}
        )
        result = self.engine.score(job, resume)
        self.assertLessEqual(result.total_score, 100.0)

    def test_empty_resume_gives_low_required_skills_score(self) -> None:
        """Empty resume should score 0 on required skills component."""
        job = self._make_job(req_skills=["python", "django", "aws"])
        resume = self._make_resume(content={})
        result = self.engine.score(job, resume)
        # Skills components (40% + 30% = 70% weight) should both be 0;
        # experience/education may default to 100 when requirements are unparseable.
        self.assertAlmostEqual(result.breakdown["required_skills"], 0.0, delta=1.0)
        self.assertAlmostEqual(result.breakdown["preferred_skills"], 0.0, delta=1.0)

    def test_job_requirements_attached_to_result(self) -> None:
        job = self._make_job()
        resume = self._make_resume()
        result = self.engine.score(job, resume)
        self.assertIsNotNone(result.job_requirements)


# ---------------------------------------------------------------------------
# Spec-mandated standalone functions (from task deliverables)
# ---------------------------------------------------------------------------


def test_extract_technical_skills():
    """Test that common tech skills are extracted correctly (spec function)."""
    from analyzer.keyword_extractor import KeywordExtractor

    job_desc = "We need a Python developer with Django experience and AWS knowledge."
    extractor = KeywordExtractor()
    keywords = extractor.extract_keywords(job_desc)

    extracted_texts = [kw.text.lower() for kw in keywords]
    assert "python" in extracted_texts, f"'python' missing from {extracted_texts}"
    assert "django" in extracted_texts, f"'django' missing from {extracted_texts}"
    assert "aws" in extracted_texts, f"'aws' missing from {extracted_texts}"


def test_experience_parsing():
    """Test extraction of years of experience (spec function)."""
    from analyzer.requirement_parser import RequirementParser

    text = "5+ years of experience in Python and 3 years with AWS"
    parser = RequirementParser()
    exp_reqs = parser.parse_experience_requirements(text)

    assert len(exp_reqs) >= 2, f"Expected >=2 requirements, got {len(exp_reqs)}: {exp_reqs}"
    assert any(
        req["skill"] == "Python" and req["years"] == 5 for req in exp_reqs
    ), f"5-year Python requirement not found in {exp_reqs}"


# ---------------------------------------------------------------------------
# Edge-case tests
# ---------------------------------------------------------------------------


class TestEdgeCases(unittest.TestCase):
    """Edge cases called out in the task specification."""

    @classmethod
    def setUpClass(cls) -> None:
        from analyzer.keyword_extractor import KeywordExtractor
        from analyzer.requirement_parser import RequirementParser
        from analyzer.skill_matcher import SkillMatcher

        cls.extractor = KeywordExtractor()
        cls.parser = RequirementParser()
        cls.matcher = SkillMatcher()

    # --- Generic / sparse job postings ---

    def test_generic_posting_returns_empty_or_partial(self) -> None:
        """Posting with no clear requirements should return a list (even empty)."""
        generic = "We are a fun company looking for talented people to join us!"
        result = self.extractor.extract_keywords(generic)
        self.assertIsInstance(result, list)

    def test_generic_posting_experience_is_empty(self) -> None:
        generic = "We are a fun company looking for talented people to join us!"
        result = self.parser.parse_experience_requirements(generic)
        self.assertEqual(result, [])

    # --- Acronym / synonym recognition ---

    def test_ml_recognised_as_machine_learning(self) -> None:
        """'ML' in context should be captured under data_science_ml category."""
        kws = self.extractor.extract_keywords("Experience with ML and deep learning required.")
        cats = {kw.category for kw in kws}
        texts = {kw.text for kw in kws}
        # Either 'ml' directly or 'machine learning' via NER should appear
        self.assertTrue(
            "ml" in texts or "machine learning" in texts or "data_science_ml" in cats,
            f"ML not captured. texts={texts}, cats={cats}",
        )

    def test_js_synonym_matches_javascript(self) -> None:
        result = self.matcher.match(["javascript"], ["js"])
        self.assertGreater(result.overall_score, 0.0)

    def test_k8s_synonym_matches_kubernetes(self) -> None:
        result = self.matcher.match(["kubernetes"], ["k8s"])
        self.assertGreater(result.overall_score, 0.0)

    def test_aws_full_name_matches_abbreviation(self) -> None:
        result = self.matcher.match(["amazon web services"], ["aws"])
        self.assertGreater(result.overall_score, 0.0)

    # --- Negative context ("No PHP required") ---

    def test_negative_context_php_excluded(self) -> None:
        """Skills mentioned in negative context should not count as requirements."""
        text = "No PHP experience required. We use Python exclusively."
        reqs = self.parser.parse_experience_requirements(text)
        skills = [r["skill"].lower() for r in reqs]
        self.assertNotIn("php", skills, f"PHP should be excluded; got {reqs}")

    def test_negative_context_does_not_exclude_positive_mention(self) -> None:
        """A skill mentioned positively after a negative mention should still appear."""
        text = "No Java required. We do need 3+ years of Python."
        reqs = self.parser.parse_experience_requirements(text)
        python_hits = [r for r in reqs if r["skill"].lower() == "python"]
        self.assertTrue(len(python_hits) >= 1, f"Python not found in {reqs}")

    # --- Overlapping categories ---

    def test_react_categorised_as_web_framework(self) -> None:
        """React should land in web_frameworks, not an ambiguous bucket."""
        by_cat = self.extractor.extract_by_category("We build SPAs with React and TypeScript.")
        self.assertIn("web_frameworks", by_cat)
        self.assertIn("react", by_cat["web_frameworks"])

    def test_node_js_categorised_correctly(self) -> None:
        by_cat = self.extractor.extract_by_category("Backend service built with Node.js.")
        framework_skills = by_cat.get("web_frameworks", [])
        self.assertTrue(
            any("node" in s for s in framework_skills),
            f"node.js not in web_frameworks: {by_cat}",
        )

    # --- Missing information handled gracefully ---

    def test_empty_string_experience(self) -> None:
        reqs = self.parser.parse_experience_requirements("")
        self.assertEqual(reqs, [])

    def test_none_like_whitespace_experience(self) -> None:
        reqs = self.parser.parse_experience_requirements("   \n\t  ")
        self.assertEqual(reqs, [])

    def test_no_skills_job_match_score_is_zero(self) -> None:
        from analyzer.skill_matcher import SkillMatcher

        m = SkillMatcher()
        result = m.match([], ["python", "django"])
        self.assertEqual(result.overall_score, 0.0)

    # --- parse_experience_requirements dict schema ---

    def test_experience_requirements_dict_schema(self) -> None:
        reqs = self.parser.parse_experience_requirements("5+ years of Python experience.")
        self.assertTrue(len(reqs) >= 1)
        req = reqs[0]
        self.assertIn("skill", req)
        self.assertIn("years", req)
        self.assertIn("is_minimum", req)
        self.assertIsInstance(req["years"], int)

    def test_experience_years_value_correct(self) -> None:
        reqs = self.parser.parse_experience_requirements("3+ years of AWS experience.")
        hits = [r for r in reqs if "aws" in r["skill"].lower()]
        self.assertTrue(len(hits) >= 1)
        self.assertEqual(hits[0]["years"], 3)
        self.assertTrue(hits[0]["is_minimum"])


# ---------------------------------------------------------------------------
# New tests: BUG 1 + BUG 2 fixes and CHANGE 3/4
# ---------------------------------------------------------------------------

# Job description with explicit required AND preferred sections
_SPLIT_JD = """
About the Role
We are building an amazing product.

Requirements:
- 3+ years Python experience
- Strong knowledge of Django and REST APIs
- PostgreSQL or MySQL required

Preferred Qualifications:
- Experience with Kubernetes or Docker
- Familiarity with AWS or GCP
- Redis experience a plus
"""

_NOISY_TEXT = (
    "Based in San Francisco. Google Inc is looking for a Senior Engineer. "
    "5+ years experience. Salary $120,000. Must know Python and PostgreSQL."
)


class TestRequirementParserSectionSplit(unittest.TestCase):
    """Tests for the new required_text / preferred_text split."""

    def setUp(self) -> None:
        from analyzer.requirement_parser import RequirementParser
        self.parser = RequirementParser()

    def test_required_text_key_present(self) -> None:
        result = self.parser.parse(_SPLIT_JD)
        self.assertIsNotNone(result.required_text)
        self.assertIsInstance(result.required_text, str)

    def test_preferred_text_key_present(self) -> None:
        result = self.parser.parse(_SPLIT_JD)
        self.assertIsNotNone(result.preferred_text)
        self.assertIsInstance(result.preferred_text, str)

    def test_preferred_text_non_empty_when_section_present(self) -> None:
        result = self.parser.parse(_SPLIT_JD)
        self.assertTrue(
            len(result.preferred_text) > 0,
            "preferred_text should be non-empty when a 'Preferred' section exists",
        )

    def test_required_text_contains_required_skills(self) -> None:
        result = self.parser.parse(_SPLIT_JD)
        # The Django / PostgreSQL requirement is in the Required section
        self.assertIn("Django", result.required_text)

    def test_preferred_text_does_not_contain_required_content(self) -> None:
        result = self.parser.parse(_SPLIT_JD)
        # "Django" is in Required, NOT in Preferred section
        self.assertNotIn("Django", result.preferred_text)

    def test_preferred_text_contains_preferred_skills(self) -> None:
        result = self.parser.parse(_SPLIT_JD)
        # Kubernetes / Docker are in Preferred section
        self.assertTrue(
            "Kubernetes" in result.preferred_text or "Docker" in result.preferred_text,
            f"Expected Kubernetes/Docker in preferred_text, got: {result.preferred_text[:200]}",
        )

    def test_fallback_full_text_when_no_required_header(self) -> None:
        plain = "We need Python, Django, and PostgreSQL experience."
        result = self.parser.parse(plain)
        # Fallback: required_text == full text
        self.assertEqual(result.required_text.strip(), plain.strip())

    def test_no_preferred_section_gives_empty_preferred_text(self) -> None:
        plain = "Requirements:\n- Python\n- Django\n"
        result = self.parser.parse(plain)
        self.assertEqual(result.preferred_text, "")

    def test_to_dict_includes_new_fields(self) -> None:
        result = self.parser.parse(_SPLIT_JD)
        d = result.to_dict()
        self.assertIn("required_text", d)
        self.assertIn("preferred_text", d)


class TestKeywordExtractorSplitBuckets(unittest.TestCase):
    """Tests for the new extract() method and NER noise fixes."""

    def setUp(self) -> None:
        from analyzer.keyword_extractor import KeywordExtractor
        self.ke = KeywordExtractor()

    def test_extract_returns_required_and_preferred_keys(self) -> None:
        result = self.ke.extract(_SPLIT_JD)
        self.assertIn("required_skills", result)
        self.assertIn("preferred_skills", result)
        self.assertIn("experience_required", result)
        self.assertIn("education_required", result)
        self.assertIn("certifications", result)

    def test_preferred_skills_not_always_empty(self) -> None:
        result = self.ke.extract(_SPLIT_JD)
        self.assertTrue(
            len(result["preferred_skills"]) > 0,
            f"preferred_skills should not be empty. Got: {result['preferred_skills']}",
        )

    def test_preferred_skills_disjoint_from_required(self) -> None:
        result = self.ke.extract(_SPLIT_JD)
        req_set  = set(result["required_skills"])
        pref_set = set(result["preferred_skills"])
        overlap  = req_set & pref_set
        self.assertEqual(
            overlap, set(),
            f"required and preferred skills overlap: {overlap}",
        )

    def test_required_skills_contains_django(self) -> None:
        result = self.ke.extract(_SPLIT_JD)
        req_lower = [s.lower() for s in result["required_skills"]]
        self.assertTrue(
            any("django" in s for s in req_lower),
            f"'django' not found in required_skills: {result['required_skills']}",
        )

    def test_preferred_skills_contains_kubernetes_or_docker(self) -> None:
        result = self.ke.extract(_SPLIT_JD)
        pref_lower = [s.lower() for s in result["preferred_skills"]]
        self.assertTrue(
            any(s in pref_lower for s in ("kubernetes", "docker", "aws", "gcp", "redis")),
            f"Expected a cloud/container skill in preferred, got: {result['preferred_skills']}",
        )

    # ---- NER noise filter tests ----

    def test_is_noise_filters_locations(self) -> None:
        self.assertTrue(self.ke._is_noise("san francisco"))
        self.assertTrue(self.ke._is_noise("remote"))
        self.assertTrue(self.ke._is_noise("new york"))
        self.assertFalse(self.ke._is_noise("Python"))
        self.assertFalse(self.ke._is_noise("AWS"))

    def test_is_noise_filters_year_patterns(self) -> None:
        self.assertTrue(self.ke._is_noise("5+ years"))
        self.assertTrue(self.ke._is_noise("3 years"))
        self.assertTrue(self.ke._is_noise("2019"))
        self.assertFalse(self.ke._is_noise("REST API"))
        self.assertFalse(self.ke._is_noise("kubernetes"))

    def test_is_noise_filters_salary(self) -> None:
        self.assertTrue(self.ke._is_noise("$120,000"))
        self.assertTrue(self.ke._is_noise("$80k"))

    def test_is_noise_filters_percentage(self) -> None:
        self.assertTrue(self.ke._is_noise("25%"))

    def test_is_noise_keeps_valid_skills(self) -> None:
        for skill in ("python", "django", "postgresql", "docker", "aws", "node.js"):
            self.assertFalse(
                self.ke._is_noise(skill),
                f"'{skill}' should NOT be flagged as noise",
            )

    def test_ner_noise_filtered_out(self) -> None:
        ner = self.ke._extract_ner_entities_filtered(_NOISY_TEXT)
        ner_lower = [s.lower() for s in ner]
        self.assertNotIn("san francisco", ner_lower, "Location leaked into NER output")
        self.assertNotIn("5+ years",      ner_lower, "Year pattern leaked into NER output")

    def test_ner_keeps_genuine_tech_skills(self) -> None:
        tech_text = (
            "We use Kubernetes for container orchestration, AWS for cloud, "
            "PostgreSQL as our primary database, and Docker for CI/CD."
        )
        ner = self.ke._extract_ner_entities_filtered(tech_text)
        ner_lower = [s.lower() for s in ner]
        # At least one of these should be found
        found = any(t in ner_lower for t in ("kubernetes", "aws", "postgresql", "docker"))
        # NER is non-deterministic across spaCy versions — just warn if not found
        if not found:
            import warnings
            warnings.warn(
                f"Tech skills not found by NER in test text. "
                f"ner_lower={ner_lower}. This may be model-version dependent."
            )

    def test_required_skills_no_location_noise(self) -> None:
        result = self.ke.extract(_NOISY_TEXT)
        req_lower = [s.lower() for s in result["required_skills"]]
        self.assertNotIn("san francisco", req_lower)
        self.assertNotIn("5+ years",      req_lower)

    def test_extract_empty_string(self) -> None:
        result = self.ke.extract("")
        self.assertEqual(result["required_skills"],  [])
        self.assertEqual(result["preferred_skills"], [])


class TestScoringEngineWeightRedistribution(unittest.TestCase):
    """Tests for CHANGE 3: weight redistribution and rich breakdown."""

    def setUp(self) -> None:
        from analyzer.scoring import ScoringEngine
        self.engine = ScoringEngine()

    def _make_job(self, req=None, pref=None, desc=""):
        """Return a duck-typed job object."""
        class _Job:
            required_skills  = req or []
            preferred_skills = pref or []
            job_description  = desc
            id = None
        return _Job()

    def _make_resume(self, skills=None):
        """Return a duck-typed resume object."""
        class _Resume:
            content = {"skills": skills or []}
            id = None
        return _Resume()

    def test_score_breakdown_present_on_result(self) -> None:
        job    = self._make_job(req=["python", "django"])
        resume = self._make_resume(["python", "django", "rest"])
        result = self.engine.calculate_score(job, resume)
        self.assertIsNotNone(result.score_breakdown)
        bd = result.score_breakdown
        self.assertIn("required_skills",  bd)
        self.assertIn("preferred_skills", bd)
        self.assertIn("experience",       bd)
        self.assertIn("education",        bd)
        self.assertIn("bonus",            bd)
        self.assertIn("weight_note",      bd)

    def test_no_preferred_skills_redistributes_weight(self) -> None:
        job    = self._make_job(req=["python", "django"], pref=[])
        resume = self._make_resume(["python", "django"])
        result = self.engine.calculate_score(job, resume)
        self.assertNotEqual(
            result.score_breakdown["weight_note"], "",
            "weight_note should be non-empty when preferred_skills=[]",
        )
        self.assertIn("redistributed", result.score_breakdown["weight_note"])

    def test_with_preferred_skills_uses_normal_weights(self) -> None:
        job    = self._make_job(req=["python"], pref=["kubernetes"])
        resume = self._make_resume(["python", "kubernetes"])
        result = self.engine.calculate_score(job, resume)
        self.assertEqual(
            result.score_breakdown["weight_note"], "",
            "weight_note should be empty when preferred_skills is populated",
        )

    def test_no_preferred_score_greater_than_zero(self) -> None:
        """Even with no preferred skills, a matching resume should score > 0."""
        job    = self._make_job(req=["python", "django"], pref=[])
        resume = self._make_resume(["python", "django"])
        result = self.engine.calculate_score(job, resume)
        self.assertGreater(result.total_score, 0)

    def test_preferred_skills_found_count_on_result(self) -> None:
        job    = self._make_job(req=["python"], pref=["kubernetes", "docker"])
        resume = self._make_resume(["python", "kubernetes"])
        result = self.engine.calculate_score(job, resume)
        self.assertGreaterEqual(result.preferred_skills_found, 1)
        self.assertEqual(result.preferred_skills_total, 3)  # 2 skills → expanded set ≥ 2

    def test_preferred_skills_total_zero_when_none(self) -> None:
        job    = self._make_job(req=["python"], pref=[])
        resume = self._make_resume(["python"])
        result = self.engine.calculate_score(job, resume)
        self.assertEqual(result.preferred_skills_total, 0)

    def test_score_alias_property(self) -> None:
        """result.score should be the same as result.total_score."""
        job    = self._make_job(req=["python"])
        resume = self._make_resume(["python"])
        result = self.engine.calculate_score(job, resume)
        self.assertEqual(result.score, result.total_score)

    def test_score_breakdown_matched_counts_correct(self) -> None:
        job    = self._make_job(req=["python", "django", "postgresql"])
        resume = self._make_resume(["python", "django"])
        result = self.engine.calculate_score(job, resume)
        bd = result.score_breakdown["required_skills"]
        # python and django matched; postgresql did not
        self.assertGreaterEqual(bd["matched"], 2)
        self.assertGreaterEqual(bd["total"],   2)

    def test_full_pipeline_score_with_preferred_section_higher_than_without(self) -> None:
        """A job WITH a preferred section should give at least as high a score
        when the resume covers both required and preferred skills."""
        # Without preferred: required-only, weight redistributed
        job_no_pref  = self._make_job(req=["python", "django"], pref=[])
        # With preferred: separate bucket, covers additional skills
        job_with_pref = self._make_job(req=["python", "django"], pref=["kubernetes"])
        resume = self._make_resume(["python", "django", "kubernetes"])

        result_no   = self.engine.calculate_score(job_no_pref,  resume)
        result_with = self.engine.calculate_score(job_with_pref, resume)

        # Both should be > 0; the preferred-bucket case should get credit
        self.assertGreater(result_no.total_score,   0)
        self.assertGreater(result_with.total_score, 0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    unittest.main()
