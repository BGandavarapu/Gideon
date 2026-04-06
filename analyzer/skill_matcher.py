"""
Skill matching engine – compares job requirements against resume skills.

The matcher solves three sub-problems:

1. **Normalisation** – lowercasing, punctuation stripping, whitespace
   collapsing so ``"Node.JS"`` and ``"nodejs"`` resolve to the same token.

2. **Synonym expansion** – a built-in synonym map maps abbreviations and
   alternate spellings to canonical forms before comparison, so ``"js"``
   matches ``"javascript"`` and ``"k8s"`` matches ``"kubernetes"``.

3. **Weighted scoring** – caller-supplied weights let the scoring engine
   emphasise required skills more heavily than preferred ones.

Return value
------------
All public methods return a :class:`MatchResult` dataclass which contains
every piece of data the Phase 4 resume engine will need: the overlap
list, gap list, extra skills, and per-category scores.
"""

import logging
import re
import string
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Synonym map  (abbreviation / variant → canonical form)
# ---------------------------------------------------------------------------

_SYNONYMS: Dict[str, str] = {
    # Languages
    "js": "javascript",
    "ts": "typescript",
    "py": "python",
    "rb": "ruby",
    "golang": "go",
    "cplusplus": "c++",
    "csharp": "c#",
    # Frameworks
    "reactjs": "react",
    "react.js": "react",
    "vuejs": "vue",
    "vue.js": "vue",
    "angularjs": "angular",
    "nodejs": "node.js",
    "node js": "node.js",
    "fastapi": "fastapi",
    "rubyonrails": "rails",
    "ruby on rails": "rails",
    "springboot": "spring boot",
    # Cloud
    "amazon web services": "aws",
    "microsoft azure": "azure",
    "google cloud platform": "gcp",
    "google cloud": "gcp",
    # Databases
    "postgresql": "postgres",
    "microsoft sql server": "sql server",
    "mssql": "sql server",
    "mongo": "mongodb",
    "elastic": "elasticsearch",
    # Tools
    "kubernetes": "k8s",  # canonical = k8s (shorter; both forms kept in index)
    "k8s": "kubernetes",  # allow either direction
    "docker swarm": "docker",
    "gitlab ci/cd": "gitlab ci",
    "github action": "github actions",
    # ML/AI
    "scikit learn": "scikit-learn",
    "sklearn": "scikit-learn",
    "tensor flow": "tensorflow",
    "pytorch": "torch",
    "torch": "pytorch",
    "natural language processing": "nlp",
    "machine learning": "ml",
    "ml": "machine learning",
    "deep learning": "dl",
    "ai": "artificial intelligence",
    # Methodologies
    "ci/cd": "cicd",
    "ci cd": "cicd",
    "continuous integration": "cicd",
    "continuous deployment": "cicd",
    "agile/scrum": "agile",
    # Misc
    "rest api": "rest",
    "restful api": "rest",
    "restful": "rest",
    "nosql": "no-sql",
    "no sql": "no-sql",
    # Certs
    "aws sa": "aws solutions architect",
    "cka": "certified kubernetes administrator",
    "ckad": "certified kubernetes application developer",
}


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class MatchResult:
    """Complete output of a :func:`SkillMatcher.match` call.

    Attributes:
        overall_score: Weighted match percentage (0.0–100.0).
        matched_skills: Skills present in both job requirements and resume.
        missing_skills: Job-required skills absent from the resume.
        extra_skills: Resume skills not mentioned in the job (positive signal).
        category_scores: Per-category match percentages.
        total_job_skills: Number of unique job skills evaluated.
        total_resume_skills: Number of unique resume skills evaluated.
    """

    overall_score: float = 0.0
    matched_skills: List[str] = field(default_factory=list)
    missing_skills: List[str] = field(default_factory=list)
    extra_skills: List[str] = field(default_factory=list)
    category_scores: Dict[str, float] = field(default_factory=dict)
    total_job_skills: int = 0
    total_resume_skills: int = 0

    def to_dict(self) -> dict:
        """Serialise to a JSON-compatible dictionary."""
        return {
            "overall_score": round(self.overall_score, 2),
            "matched_skills": self.matched_skills,
            "missing_skills": self.missing_skills,
            "extra_skills": self.extra_skills,
            "category_scores": {
                cat: round(score, 2) for cat, score in self.category_scores.items()
            },
            "total_job_skills": self.total_job_skills,
            "total_resume_skills": self.total_resume_skills,
        }


# ---------------------------------------------------------------------------
# Skill Matcher
# ---------------------------------------------------------------------------


class SkillMatcher:
    """Compare job requirement skills against resume skills.

    Args:
        extra_synonyms: Optional additional synonym mappings to merge with
            the built-in :data:`_SYNONYMS` dictionary.

    Attributes:
        synonym_map: Active synonym mapping (normalised key → canonical form).
    """

    def __init__(self, extra_synonyms: Optional[Dict[str, str]] = None) -> None:
        self.synonym_map: Dict[str, str] = dict(_SYNONYMS)
        if extra_synonyms:
            self.synonym_map.update(
                {self._normalise(k): self._normalise(v) for k, v in extra_synonyms.items()}
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def match(
        self,
        job_skills: List[str],
        resume_skills: List[str],
        weights: Optional[Dict[str, float]] = None,
    ) -> MatchResult:
        """Calculate how well a resume's skill set covers a job's requirements.

        Args:
            job_skills: Skills required/preferred by the job (combined list).
            resume_skills: Skills declared in the resume.
            weights: Optional per-skill weights (normalised key → float).
                When provided, matched skills earn proportionally more.

        Returns:
            :class:`MatchResult` with all comparison data.
        """
        if not job_skills:
            logger.debug("No job skills supplied – returning zero-score result.")
            return MatchResult(total_resume_skills=len(resume_skills))

        # Normalise both lists
        job_norm = self._expand_set(job_skills)
        resume_norm = self._expand_set(resume_skills)

        matched = sorted(job_norm & resume_norm)
        missing = sorted(job_norm - resume_norm)
        extra = sorted(resume_norm - job_norm)

        score = self._weighted_score(job_norm, resume_norm, weights)

        logger.debug(
            "Match: %.1f%% – matched=%d, missing=%d, extra=%d",
            score, len(matched), len(missing), len(extra),
        )

        return MatchResult(
            overall_score=score,
            matched_skills=matched,
            missing_skills=missing,
            extra_skills=extra,
            total_job_skills=len(job_norm),
            total_resume_skills=len(resume_norm),
        )

    def match_by_category(
        self,
        job_skills_by_cat: Dict[str, List[str]],
        resume_skills: List[str],
        category_weights: Optional[Dict[str, float]] = None,
    ) -> MatchResult:
        """Calculate match scores broken down by skill category.

        Args:
            job_skills_by_cat: Dictionary mapping category names to skill lists
                (e.g. ``{"programming_languages": ["python", "java"], ...}``).
            resume_skills: Flat list of resume skills.
            category_weights: Optional per-category weights.  Defaults to equal
                weighting across all categories.

        Returns:
            :class:`MatchResult` with ``category_scores`` populated.
        """
        resume_norm = self._expand_set(resume_skills)
        all_job_skills: Set[str] = set()
        category_scores: Dict[str, float] = {}

        for category, skills in job_skills_by_cat.items():
            if not skills:
                continue
            cat_job = self._expand_set(skills)
            all_job_skills |= cat_job
            if not cat_job:
                continue
            cat_matched = cat_job & resume_norm
            category_scores[category] = len(cat_matched) / len(cat_job) * 100.0

        matched = sorted(all_job_skills & resume_norm)
        missing = sorted(all_job_skills - resume_norm)
        extra = sorted(resume_norm - all_job_skills)

        # Compute overall as weighted or simple average of category scores
        if category_weights and category_scores:
            total_weight = sum(
                category_weights.get(cat, 1.0) for cat in category_scores
            )
            overall = sum(
                score * category_weights.get(cat, 1.0)
                for cat, score in category_scores.items()
            ) / total_weight if total_weight else 0.0
        elif category_scores:
            overall = sum(category_scores.values()) / len(category_scores)
        else:
            overall = 0.0

        return MatchResult(
            overall_score=min(overall, 100.0),
            matched_skills=matched,
            missing_skills=missing,
            extra_skills=extra,
            category_scores=category_scores,
            total_job_skills=len(all_job_skills),
            total_resume_skills=len(resume_norm),
        )

    def calculate_match_score(
        self,
        job_skills: List[str],
        resume_skills: List[str],
        weights: Optional[Dict[str, float]] = None,
    ) -> dict:
        """Spec-compatible alias for :meth:`match` that returns a plain dict.

        Returns the same data as :meth:`match` but as a JSON-serialisable
        dictionary so callers don't need to import :class:`MatchResult`.

        Args:
            job_skills: Skills required/preferred by the job.
            resume_skills: Skills declared in the resume.
            weights: Optional per-skill weights.

        Returns:
            Dictionary with ``overall_score``, ``matched_skills``,
            ``missing_skills``, ``extra_skills``, and ``category_scores``.
        """
        result = self.match(job_skills, resume_skills, weights)
        return result.to_dict()

    # ------------------------------------------------------------------
    # Normalisation + synonym expansion
    # ------------------------------------------------------------------

    def normalise(self, skill: str) -> str:
        """Return the canonical, synonym-resolved form of *skill*.

        This is the public single-item version useful for ad-hoc lookups.

        Args:
            skill: Raw skill string.

        Returns:
            Normalised canonical string.
        """
        return self._resolve_synonym(self._normalise(skill))

    def _normalise(self, text: str) -> str:
        """Lowercase, strip punctuation noise, collapse whitespace.

        Preserves meaningful punctuation (``+``, ``#``, ``.``, ``/``)
        that are part of technology names (``c++``, ``c#``, ``node.js``).

        Args:
            text: Raw skill or synonym string.

        Returns:
            Normalised string.
        """
        text = text.strip().lower()
        # Remove trailing punctuation that is unlikely to be part of a name
        text = text.rstrip(".,;:!?'\"")
        # Collapse multiple spaces
        text = re.sub(r"\s+", " ", text)
        return text

    def _resolve_synonym(self, normalised: str) -> str:
        """Resolve one synonym hop from the synonym map.

        Does not follow chains (A→B→C) to avoid infinite loops.

        Args:
            normalised: Already-normalised skill text.

        Returns:
            Canonical form if a synonym exists, otherwise the input.
        """
        return self.synonym_map.get(normalised, normalised)

    def _expand_set(self, skills: List[str]) -> Set[str]:
        """Normalise + synonym-resolve a list of skills into a set.

        Both the normalised form and the synonym-resolved form are added
        so that partial matches from either direction are captured.

        Args:
            skills: Raw skill strings.

        Returns:
            Set of canonical, lowercased skill strings.
        """
        result: Set[str] = set()
        for skill in skills:
            norm = self._normalise(skill)
            canon = self._resolve_synonym(norm)
            result.add(norm)
            result.add(canon)
            # Also add without hyphens / slashes for fuzzy matching
            simplified = re.sub(r"[-/]", " ", canon).strip()
            if simplified != canon:
                result.add(simplified)
        return result

    # ------------------------------------------------------------------
    # Weighted scoring
    # ------------------------------------------------------------------

    def _weighted_score(
        self,
        job_skills: Set[str],
        resume_skills: Set[str],
        weights: Optional[Dict[str, float]],
    ) -> float:
        """Compute a weighted coverage percentage.

        If no weights are supplied, every job skill contributes equally.

        Args:
            job_skills: Normalised job skill set.
            resume_skills: Normalised resume skill set.
            weights: Optional mapping of skill → weight float.

        Returns:
            Score in the range 0.0–100.0.
        """
        if not job_skills:
            return 0.0

        if not weights:
            matched_count = len(job_skills & resume_skills)
            return matched_count / len(job_skills) * 100.0

        total_weight = sum(weights.get(skill, 1.0) for skill in job_skills)
        if total_weight == 0:
            return 0.0

        matched_weight = sum(
            weights.get(skill, 1.0)
            for skill in job_skills
            if skill in resume_skills
        )
        return min(matched_weight / total_weight * 100.0, 100.0)
