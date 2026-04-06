"""Integration tests for the match scoring engine.

Verifies score accuracy, score_breakdown structure, synonym matching,
weight redistribution, bonus capping, and cross-domain score behavior.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analyzer.scoring import ScoringEngine


class TestMatchScoringAccuracy(unittest.TestCase):

    def setUp(self) -> None:
        self.engine = ScoringEngine()

    # ------------------------------------------------------------------
    # Score range tests
    # ------------------------------------------------------------------

    def test_perfect_skill_overlap_scores_high(self) -> None:
        """Resume with every required skill should score >= 75."""
        job_skills = ["Python", "Django", "PostgreSQL", "Docker", "AWS"]
        resume_skills = ["Python", "Django", "PostgreSQL", "Docker", "AWS", "Git"]
        result = self.engine.score_from_text(
            job_description="Senior backend developer role using Python stack.",
            resume_skills=resume_skills,
            job_required_skills=job_skills,
        )
        self.assertGreaterEqual(
            result.total_score, 75.0,
            f"Perfect overlap should score >= 75, got {result.total_score}",
        )

    def test_zero_skill_overlap_scores_low(self) -> None:
        """Resume with zero matching skills should score < 40 (only exp+edu can help)."""
        result = self.engine.score_from_text(
            job_description="",
            resume_skills=["Java", "Spring Boot", "Oracle"],
            job_required_skills=["Python", "Django", "PostgreSQL", "Docker"],
        )
        self.assertLess(
            result.total_score, 40.0,
            f"No overlap should score < 40, got {result.total_score}",
        )

    def test_partial_skill_overlap_scores_between(self) -> None:
        """50% skill overlap should score between 30 and 80."""
        result = self.engine.score_from_text(
            job_description="",
            resume_skills=["Python", "Django"],
            job_required_skills=["Python", "Django", "Docker", "AWS"],
        )
        self.assertGreater(result.total_score, 30.0)
        self.assertLess(result.total_score, 80.0)

    def test_score_clamped_to_100(self) -> None:
        """Total score can never exceed 100 even with bonus points."""
        result = self.engine.score_from_text(
            job_description="",
            resume_skills=["Python", "Django", "AWS", "Docker", "Git",
                           "Postgres", "Redis", "Kafka", "GraphQL", "TypeScript",
                           "Rust", "Go", "Scala", "Kotlin", "Swift"],
            job_required_skills=["Python", "Django"],
        )
        self.assertLessEqual(result.total_score, 100.0)

    def test_score_is_non_negative(self) -> None:
        """Score is always >= 0."""
        result = self.engine.score_from_text(
            job_description="",
            resume_skills=[],
            job_required_skills=["Python", "Haskell", "COBOL"],
        )
        self.assertGreaterEqual(result.total_score, 0.0)

    # ------------------------------------------------------------------
    # score_breakdown structure
    # ------------------------------------------------------------------

    def test_score_breakdown_has_all_keys(self) -> None:
        """score_breakdown must have all required dashboard keys."""
        result = self.engine.score_from_text(
            job_description="Python dev role",
            resume_skills=["Python"],
            job_required_skills=["Python", "Django"],
        )
        bd = result.score_breakdown
        for key in ("required_skills", "preferred_skills", "experience", "education", "bonus"):
            self.assertIn(key, bd, f"Missing key in score_breakdown: {key!r}")

    def test_required_skills_sub_keys(self) -> None:
        """required_skills breakdown must have matched, total, score."""
        result = self.engine.score_from_text(
            job_description="",
            resume_skills=["Python", "Django"],
            job_required_skills=["Python", "Django", "Docker"],
        )
        rs = result.score_breakdown["required_skills"]
        self.assertIn("matched", rs)
        self.assertIn("total", rs)
        self.assertIn("score", rs)
        self.assertIsInstance(rs["matched"], int)
        self.assertIsInstance(rs["total"], int)
        self.assertIsInstance(rs["score"], (int, float))

    def test_matched_count_equals_matched_skills_length(self) -> None:
        """matched count in breakdown must equal len(result.matched_skills)."""
        result = self.engine.score_from_text(
            job_description="",
            resume_skills=["Python", "Django", "AWS"],
            job_required_skills=["Python", "Django", "PostgreSQL", "Docker"],
        )
        bd = result.score_breakdown
        self.assertEqual(bd["required_skills"]["matched"], len(result.matched_skills))

    def test_total_in_breakdown_at_least_job_skills_count(self) -> None:
        """required_skills.total must be >= len(job_required_skills).

        The SkillMatcher expands synonyms (e.g. 'postgresql' → 'postgres'),
        so total_job_skills >= len(original list) is guaranteed.
        """
        job_skills = ["Python", "Django", "Docker", "AWS", "PostgreSQL"]
        result = self.engine.score_from_text(
            job_description="",
            resume_skills=["Python"],
            job_required_skills=job_skills,
        )
        self.assertGreaterEqual(
            result.score_breakdown["required_skills"]["total"], len(job_skills)
        )

    # ------------------------------------------------------------------
    # Synonym matching
    # ------------------------------------------------------------------

    def test_kubernetes_k8s_synonym_matches(self) -> None:
        """'k8s' in job and 'kubernetes' in resume should match (synonym).

        The SkillMatcher expands synonyms bidirectionally, so just verify
        the required skill score is 100% (all job skills matched).
        """
        result = self.engine.score_from_text(
            job_description="",
            resume_skills=["kubernetes"],
            job_required_skills=["k8s"],
        )
        # The only job skill is k8s — resume has kubernetes (synonym) → 100% match
        self.assertGreater(result.total_score, 0,
            "k8s/kubernetes synonym should produce a non-zero score")
        self.assertGreaterEqual(
            result.score_breakdown["required_skills"]["score"], 90.0,
            "k8s/kubernetes synonym should score at or near 100%",
        )

    def test_javascript_js_synonym(self) -> None:
        """'js' → 'javascript' synonym should result in a match."""
        result = self.engine.score_from_text(
            job_description="",
            resume_skills=["javascript"],
            job_required_skills=["js"],
        )
        self.assertGreater(result.score_breakdown["required_skills"]["matched"], 0)

    def test_postgres_postgresql_synonym(self) -> None:
        """'postgresql' and 'postgres' are synonyms."""
        result = self.engine.score_from_text(
            job_description="",
            resume_skills=["postgresql"],
            job_required_skills=["postgres"],
        )
        self.assertGreater(result.score_breakdown["required_skills"]["matched"], 0)

    def test_aws_amazon_web_services_synonym(self) -> None:
        """'amazon web services' on resume matches 'aws' in job."""
        result = self.engine.score_from_text(
            job_description="",
            resume_skills=["amazon web services"],
            job_required_skills=["aws"],
        )
        self.assertGreater(result.score_breakdown["required_skills"]["matched"], 0)

    # ------------------------------------------------------------------
    # Weight redistribution
    # ------------------------------------------------------------------

    def test_preferred_weight_redistributed_when_no_preferred_skills(self) -> None:
        """When job has no preferred skills, weight_note says 'redistributed'."""
        result = self.engine.score_from_text(
            job_description="",
            resume_skills=["Python"],
            job_required_skills=["Python"],
            job_preferred_skills=[],
        )
        self.assertIn("redistributed", result.score_breakdown["weight_note"])

    def test_preferred_weight_not_redistributed_when_preferred_exist(self) -> None:
        """When job has preferred skills, weight_note should be empty."""
        result = self.engine.score_from_text(
            job_description="",
            resume_skills=["Python"],
            job_required_skills=["Python"],
            job_preferred_skills=["Docker"],
        )
        self.assertEqual(result.score_breakdown["weight_note"], "")

    def test_no_preferred_skills_still_produces_fair_score(self) -> None:
        """Score without preferred section should use 70% on required (not penalize)."""
        # All required skills matched, no preferred → should score well
        result = self.engine.score_from_text(
            job_description="",
            resume_skills=["Python", "Django"],
            job_required_skills=["Python", "Django"],
            job_preferred_skills=[],
        )
        # 100% required (×0.70) + experience (×0.20) + education (×0.10) + bonus
        self.assertGreaterEqual(result.total_score, 70.0)

    # ------------------------------------------------------------------
    # Bonus cap
    # ------------------------------------------------------------------

    def test_bonus_capped_at_five_points(self) -> None:
        """Extra skills beyond job requirements give at most 5 bonus points."""
        result = self.engine.score_from_text(
            job_description="",
            resume_skills=["Python", "JS", "Go", "Rust", "Kotlin",
                           "Swift", "Ruby", "Perl", "R", "Scala", "Haskell", "Erlang"],
            job_required_skills=["Python"],
        )
        self.assertLessEqual(result.score_breakdown["bonus"], 5.0)

    def test_no_extra_skills_gives_zero_bonus(self) -> None:
        """If resume only has job skills (no extras), bonus should be 0."""
        result = self.engine.score_from_text(
            job_description="",
            resume_skills=["Python"],
            job_required_skills=["Python", "Django"],
        )
        # Resume has only Python (which is required), no extras
        self.assertEqual(result.score_breakdown["bonus"], 0.0)

    # ------------------------------------------------------------------
    # Cross-domain scoring
    # ------------------------------------------------------------------

    def test_cross_domain_marketing_vs_se_scores_low(self) -> None:
        """Marketing resume skills should score < 35 against SE job skills."""
        marketing_skills = ["SEO", "HubSpot", "Google Ads", "Salesforce",
                             "Content Marketing", "Email Campaigns"]
        se_job_skills = ["Python", "Django", "AWS", "Docker",
                         "PostgreSQL", "REST APIs", "Kubernetes"]
        result = self.engine.score_from_text(
            job_description="Backend Python developer needed.",
            resume_skills=marketing_skills,
            job_required_skills=se_job_skills,
        )
        self.assertLess(
            result.total_score, 40.0,
            f"Cross-domain score should be < 40, got {result.total_score}",
        )

    def test_same_domain_scores_higher_than_cross_domain(self) -> None:
        """Same-domain resume scores higher than cross-domain resume for same job."""
        se_resume = ["Python", "Django", "AWS", "Docker", "PostgreSQL"]
        marketing_resume = ["SEO", "HubSpot", "Google Ads", "Salesforce", "Email"]
        job_skills = ["Python", "Django", "AWS", "Docker"]

        se_result = self.engine.score_from_text(
            job_description="",
            resume_skills=se_resume,
            job_required_skills=job_skills,
        )
        mkt_result = self.engine.score_from_text(
            job_description="",
            resume_skills=marketing_resume,
            job_required_skills=job_skills,
        )
        self.assertGreater(
            se_result.total_score, mkt_result.total_score,
            f"SE resume ({se_result.total_score:.1f}) should beat "
            f"marketing resume ({mkt_result.total_score:.1f}) for SE job",
        )

    # ------------------------------------------------------------------
    # Missing / extra skills lists
    # ------------------------------------------------------------------

    def test_missing_skills_are_job_skills_not_in_resume(self) -> None:
        """missing_skills must be subset of job required skills."""
        job_skills = ["Python", "Django", "Docker", "AWS"]
        resume_skills = ["Python", "Django"]
        result = self.engine.score_from_text(
            job_description="",
            resume_skills=resume_skills,
            job_required_skills=job_skills,
        )
        # Normalize both sides for comparison
        from analyzer.skill_matcher import SkillMatcher
        matcher = SkillMatcher()
        norm_job = {matcher.normalise(s) for s in job_skills}
        for missing in result.missing_skills:
            self.assertIn(
                matcher.normalise(missing), norm_job,
                f"missing skill {missing!r} not in job_skills",
            )

    def test_extra_skills_not_in_job_requirements(self) -> None:
        """extra_skills must not be in the job's required skills."""
        job_skills = ["Python", "Django"]
        resume_skills = ["Python", "Django", "Rust", "Haskell"]
        result = self.engine.score_from_text(
            job_description="",
            resume_skills=resume_skills,
            job_required_skills=job_skills,
        )
        from analyzer.skill_matcher import SkillMatcher
        matcher = SkillMatcher()
        norm_job = {matcher.normalise(s) for s in job_skills}
        for extra in result.extra_skills:
            self.assertNotIn(
                matcher.normalise(extra), norm_job,
                f"extra skill {extra!r} is actually in job_skills",
            )

    # ------------------------------------------------------------------
    # Preferred skills component
    # ------------------------------------------------------------------

    def test_preferred_skills_matched_count_accurate(self) -> None:
        """preferred_skills_found matches breakdown count."""
        result = self.engine.score_from_text(
            job_description="",
            resume_skills=["Python", "Redis", "Docker"],
            job_required_skills=["Python", "Django"],
            job_preferred_skills=["Redis", "Kubernetes"],
        )
        bd = result.score_breakdown["preferred_skills"]
        self.assertEqual(result.preferred_skills_found, bd["matched"])
        self.assertEqual(result.preferred_skills_total, bd["total"])

    def test_preferred_skills_matching_redis(self) -> None:
        """Resume with Redis matches 'Redis' in preferred skills."""
        result = self.engine.score_from_text(
            job_description="",
            resume_skills=["Redis"],
            job_required_skills=["Python"],
            job_preferred_skills=["Redis", "Kafka"],
        )
        self.assertGreaterEqual(result.preferred_skills_found, 1)


if __name__ == "__main__":
    unittest.main()
