"""
Comprehensive job–resume match scoring engine.

Combines keyword extraction, requirement parsing, and skill matching into a
single weighted score that the CLI and Phase 4 resume engine can consume.

Score composition (weights from PROJECT_PLAN.md Phase 3)
---------------------------------------------------------
    Required skills match   40 %
    Preferred skills match  30 %
    Experience match        20 %
    Education match         10 %

Each component is scored 0–100 independently, then the weighted average is
taken.  The final score is clamped to [0, 100].

Bonus points
~~~~~~~~~~~~
Extra skills on the resume that are not in the job description earn a small
bonus (capped at +5 points) so that over-qualified candidates are not
penalised.

Usage::

    engine = ScoringEngine()
    result = engine.score(job, master_resume)
    print(result.total_score)           # e.g. 78.5
    print(result.breakdown)             # component scores dict
    print(result.missing_skills)        # prioritised gap list
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from analyzer.keyword_extractor import KeywordExtractor
from analyzer.requirement_parser import ParsedRequirements, RequirementParser
from analyzer.skill_matcher import MatchResult, SkillMatcher
from database.models import Job, MasterResume

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Component weights (must sum to 1.0)
# ---------------------------------------------------------------------------

_WEIGHT_REQUIRED = 0.40
_WEIGHT_PREFERRED = 0.30
_WEIGHT_EXPERIENCE = 0.20
_WEIGHT_EDUCATION = 0.10

# Bonus cap for extra resume skills
_BONUS_CAP = 5.0

# Education level → numeric rank for comparison
_EDU_RANK: Dict[str, int] = {
    "high_school": 0,
    "associate": 1,
    "bachelor": 2,
    "master": 3,
    "mba": 3,
    "phd": 4,
}


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class ScoreResult:
    """Complete output of :class:`ScoringEngine.score`.

    Attributes:
        total_score: Weighted composite score, 0.0–100.0.
        breakdown: Per-component scores keyed by component name.
        score_breakdown: Rich breakdown dict with matched/total counts per
            component, suitable for display in the dashboard.
        matched_skills: Skills present in both job and resume.
        missing_skills: Skills required by the job but absent from the resume.
        extra_skills: Resume skills not mentioned in the job.
        preferred_skills_found: Count of preferred skills matched.
        preferred_skills_total: Total preferred skills in the job.
        job_id: Database ID of the scored job (if available).
        resume_id: Database ID of the scored resume (if available).
        job_requirements: Parsed structured requirements for the job.
    """

    total_score: float = 0.0
    breakdown: Dict[str, float] = field(default_factory=dict)
    score_breakdown: Dict = field(default_factory=dict)
    matched_skills: List[str] = field(default_factory=list)
    missing_skills: List[str] = field(default_factory=list)
    extra_skills: List[str] = field(default_factory=list)
    preferred_skills_found: int = 0
    preferred_skills_total: int = 0
    job_id: Optional[int] = None
    resume_id: Optional[int] = None
    job_requirements: Optional[ParsedRequirements] = None

    # Alias so callers using `.score` still work
    @property
    def score(self) -> float:
        return self.total_score

    def to_dict(self) -> dict:
        """Serialise to a JSON-compatible dictionary."""
        result = {
            "total_score": round(self.total_score, 2),
            "breakdown": {k: round(v, 2) for k, v in self.breakdown.items()},
            "score_breakdown": self.score_breakdown,
            "matched_skills": self.matched_skills,
            "missing_skills": self.missing_skills,
            "extra_skills": self.extra_skills,
            "preferred_skills_found": self.preferred_skills_found,
            "preferred_skills_total": self.preferred_skills_total,
            "job_id": self.job_id,
            "resume_id": self.resume_id,
        }
        if self.job_requirements:
            result["job_requirements"] = self.job_requirements.to_dict()
        return result


# ---------------------------------------------------------------------------
# Scoring Engine
# ---------------------------------------------------------------------------


class ScoringEngine:
    """Orchestrate keyword extraction, parsing, and weighted scoring.

    Instances are expensive to construct (spaCy model load).  Create once
    and reuse across multiple score() calls.

    Args:
        keyword_extractor: Optional pre-built extractor (avoids double loading).
        requirement_parser: Optional pre-built parser.
        skill_matcher: Optional pre-built matcher.
    """

    def __init__(
        self,
        keyword_extractor: Optional[KeywordExtractor] = None,
        requirement_parser: Optional[RequirementParser] = None,
        skill_matcher: Optional[SkillMatcher] = None,
    ) -> None:
        self._extractor = keyword_extractor or KeywordExtractor()
        self._parser = requirement_parser or RequirementParser()
        self._matcher = skill_matcher or SkillMatcher()

    # ------------------------------------------------------------------
    # Primary entry points
    # ------------------------------------------------------------------

    def calculate_score(self, job, resume) -> ScoreResult:
        """Alias for :meth:`score` that accepts duck-typed job/resume objects.

        Supports both ORM instances and lightweight mock objects that expose
        ``required_skills``, ``preferred_skills``, ``job_description``, and
        ``content`` (for resume) as attributes.
        """
        return self.score(job, resume)

    def score(self, job, resume) -> ScoreResult:
        """Calculate the match score between a Job and a MasterResume.

        Args:
            job: SQLAlchemy :class:`~database.models.Job` instance (or duck-typed
                object with ``required_skills``, ``preferred_skills``,
                ``job_description`` attributes).
            resume: SQLAlchemy :class:`~database.models.MasterResume` instance (or
                duck-typed object with ``content`` attribute).

        Returns:
            :class:`ScoreResult` with all match data.
        """
        job_desc = getattr(job, "job_description", "") or ""
        resume_content = getattr(resume, "content", {}) or {}
        job_id   = getattr(job,    "id", None)
        resume_id = getattr(resume, "id", None)

        resume_skills = self._extract_resume_skills(resume_content)
        return self.score_from_text(
            job_description=job_desc,
            resume_skills=resume_skills,
            job_required_skills=getattr(job, "required_skills", None),
            job_preferred_skills=getattr(job, "preferred_skills", None),
            job_id=job_id,
            resume_id=resume_id,
        )

    # ------------------------------------------------------------------
    # Lower-level entry point: plain text / lists
    # ------------------------------------------------------------------

    def score_from_text(
        self,
        job_description: str,
        resume_skills: List[str],
        job_required_skills: Optional[List[str]] = None,
        job_preferred_skills: Optional[List[str]] = None,
        job_id: Optional[int] = None,
        resume_id: Optional[int] = None,
    ) -> ScoreResult:
        """Calculate a match score from plain-text inputs.

        This is the main computation method.  The ORM-level :meth:`score`
        method delegates here after extracting the text fields.

        Args:
            job_description: Full job posting text.
            resume_skills: Flat list of skills from the resume.
            job_required_skills: Pre-extracted required skills (from DB).
                If ``None`` the extractor runs on *job_description*.
            job_preferred_skills: Pre-extracted preferred skills (from DB).
            job_id: Optional job DB ID (stored on result for traceability).
            resume_id: Optional resume DB ID.

        Returns:
            :class:`ScoreResult` with weighted total and per-component breakdown.
        """
        # ---- Extract job keywords if not already done ----
        if job_required_skills is None:
            extracted = self._extractor.extract(job_description)
            job_required_skills = extracted["required_skills"]
            if job_preferred_skills is None:
                job_preferred_skills = extracted["preferred_skills"]
        if job_preferred_skills is None:
            job_preferred_skills = []

        # ---- Parse structured requirements ----
        requirements = self._parser.parse(job_description)

        # ---- Required skills component ----
        req_match = self._matcher.match(job_required_skills, resume_skills)
        required_score = req_match.overall_score

        # ---- Preferred skills component ----
        pref_match = self._matcher.match(job_preferred_skills, resume_skills)
        preferred_score = pref_match.overall_score

        # ---- Experience component (20%) ----
        experience_score = self._score_experience(requirements, resume_skills)

        # ---- Education component (10%) ----
        education_score = self._score_education(requirements, resume_skills)

        # ---- Weight redistribution when preferred_skills is empty ----
        # If the job has no preferred section, the 30% preferred weight would
        # silently score 0.  Redistribute it to required so scores are fair.
        has_preferred = bool(job_preferred_skills)
        if has_preferred:
            effective_req_w  = _WEIGHT_REQUIRED
            effective_pref_w = _WEIGHT_PREFERRED
            weight_note = ""
        else:
            effective_req_w  = _WEIGHT_REQUIRED + _WEIGHT_PREFERRED  # 0.70
            effective_pref_w = 0.0
            weight_note = "preferred weight redistributed to required"

        # ---- Weighted total ----
        raw_score = (
            required_score  * effective_req_w
            + preferred_score * effective_pref_w
            + experience_score * _WEIGHT_EXPERIENCE
            + education_score  * _WEIGHT_EDUCATION
        )

        # ---- Extra-skills bonus (capped at +5 pts) ----
        bonus = min(len(req_match.extra_skills) * 0.5, _BONUS_CAP)
        total = min(raw_score + bonus, 100.0)

        logger.info(
            "Score for job_id=%s / resume_id=%s: %.1f "
            "(req=%.1f pref=%.1f exp=%.1f edu=%.1f bonus=%.1f%s)",
            job_id, resume_id, total,
            required_score, preferred_score, experience_score, education_score, bonus,
            " [pref redistributed]" if not has_preferred else "",
        )

        # ---- Rich score_breakdown for dashboard display ----
        score_breakdown: Dict = {
            "required_skills": {
                "matched": len(req_match.matched_skills),
                "total":   req_match.total_job_skills,
                "score":   round(required_score, 2),
            },
            "preferred_skills": {
                "matched": len(pref_match.matched_skills),
                "total":   pref_match.total_job_skills,
                "score":   round(preferred_score, 2),
            },
            "experience": {
                "matched": experience_score >= 75.0,
                "score":   round(experience_score, 2),
            },
            "education": {
                "matched": education_score >= 100.0,
                "score":   round(education_score, 2),
            },
            "bonus":       round(bonus, 2),
            "weight_note": weight_note,
        }

        return ScoreResult(
            total_score=round(total, 2),
            breakdown={
                "required_skills": round(required_score, 2),
                "preferred_skills": round(preferred_score, 2),
                "experience": round(experience_score, 2),
                "education": round(education_score, 2),
                "bonus": round(bonus, 2),
            },
            score_breakdown=score_breakdown,
            matched_skills=req_match.matched_skills + pref_match.matched_skills,
            missing_skills=req_match.missing_skills,
            extra_skills=req_match.extra_skills,
            preferred_skills_found=len(pref_match.matched_skills),
            preferred_skills_total=pref_match.total_job_skills,
            job_id=job_id,
            resume_id=resume_id,
            job_requirements=requirements,
        )

    # ------------------------------------------------------------------
    # Component scorers
    # ------------------------------------------------------------------

    def _score_experience(
        self, requirements: ParsedRequirements, resume_skills: List[str]
    ) -> float:
        """Score the experience component.

        Strategy:
        - If no experience requirements, award full marks (100).
        - For each named-skill requirement, check if that skill is in the
          resume; award 100 for a match, 0 for a miss.
        - For year-count-only requirements, assume a match (we cannot parse
          years from a resume at this phase).
        - Return average of all component scores.

        Args:
            requirements: Parsed job requirements.
            resume_skills: Normalised resume skill list.

        Returns:
            Score 0.0–100.0.
        """
        if not requirements.experience:
            return 100.0

        resume_norm = {self._matcher.normalise(s) for s in resume_skills}
        scores: List[float] = []

        for exp in requirements.experience:
            if not exp.skill:
                # General experience requirement – assume partial match
                scores.append(75.0)
                continue
            skill_norm = self._matcher.normalise(exp.skill)
            scores.append(100.0 if skill_norm in resume_norm else 0.0)

        return sum(scores) / len(scores) if scores else 100.0

    def _score_education(
        self, requirements: ParsedRequirements, resume_skills: List[str]
    ) -> float:
        """Score the education component.

        Strategy:
        - If no education requirement, award full marks.
        - Map education keywords in the resume to a level and compare with
          the required level.
        - Award 100 if resume level >= required, 50 if one level below,
          0 if two or more levels below.

        Args:
            requirements: Parsed job requirements.
            resume_skills: Resume skills list (education keywords may appear).

        Returns:
            Score 0.0–100.0.
        """
        if not requirements.education:
            return 100.0

        required_edu = requirements.education_level
        if not required_edu:
            return 100.0

        required_rank = _EDU_RANK.get(required_edu, 2)

        # Check resume skills for education keywords
        resume_edu_rank = self._detect_resume_education_rank(resume_skills)

        if resume_edu_rank >= required_rank:
            return 100.0
        if resume_edu_rank == required_rank - 1:
            return 50.0
        return 0.0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_resume_skills(resume_content: dict) -> List[str]:
        """Pull all skills from a resume content dictionary.

        Handles both flat lists and nested section structures.

        Args:
            resume_content: JSON dict from :attr:`~database.models.MasterResume.content`.

        Returns:
            Flat list of skill strings.
        """
        skills: List[str] = []

        # Top-level "skills" key is a list
        top_skills = resume_content.get("skills", [])
        if isinstance(top_skills, list):
            skills.extend(str(s) for s in top_skills if s)

        # Nested categories e.g. {"skills": {"technical": [...], "soft": [...]}}
        elif isinstance(top_skills, dict):
            for items in top_skills.values():
                if isinstance(items, list):
                    skills.extend(str(s) for s in items if s)

        # Certifications are also matchable skills
        for cert in resume_content.get("certifications", []):
            if isinstance(cert, str):
                skills.append(cert)
            elif isinstance(cert, dict):
                name = cert.get("name") or cert.get("title", "")
                if name:
                    skills.append(str(name))

        return skills

    @staticmethod
    def _flatten_technical(keywords_by_cat: Dict[str, List[str]]) -> List[str]:
        """Flatten all non-soft-skill categories into a single list.

        Args:
            keywords_by_cat: Category → keyword list mapping.

        Returns:
            Deduplicated flat list.
        """
        non_technical = {"soft_skills", "education_keywords"}
        seen: set = set()
        result: List[str] = []
        for cat, kws in keywords_by_cat.items():
            if cat in non_technical:
                continue
            for kw in kws:
                if kw not in seen:
                    result.append(kw)
                    seen.add(kw)
        return result

    @staticmethod
    def _detect_resume_education_rank(resume_skills: List[str]) -> int:
        """Estimate education level from skill strings.

        Args:
            resume_skills: All skills/keywords from the resume.

        Returns:
            Numeric rank (0 = high-school, 4 = PhD).
        """
        skills_lower = {s.lower() for s in resume_skills}
        if any(k in skills_lower for k in ("phd", "ph.d", "doctorate")):
            return 4
        if any(k in skills_lower for k in ("master", "masters", "m.s.", "ms", "mba")):
            return 3
        if any(k in skills_lower for k in ("bachelor", "bachelors", "b.s.", "bs", "undergraduate")):
            return 2
        if any(k in skills_lower for k in ("associate",)):
            return 1
        return 0
