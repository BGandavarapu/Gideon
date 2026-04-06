"""
analyzer – NLP-powered job description analysis for Gideon.

Public surface
--------------
KeywordExtractor    Extract and categorise skills/technologies from job text.
ExtractedKeyword    Single keyword result with category + confidence.
RequirementParser   Parse experience, education, and certification requirements.
ParsedRequirements  Aggregated parsing result (experience, education, certs).
ExperienceRequirement, EducationRequirement, CertificationRequirement
                    Individual requirement dataclasses.
SkillMatcher        Compare job skill lists against resume skill lists.
MatchResult         Skill-match result (scores, matched, missing, extra).
ScoringEngine       Orchestrated weighted score engine (0–100).
ScoreResult         Full scoring result with breakdown and gap lists.
"""

from analyzer.keyword_extractor import ExtractedKeyword, KeywordExtractor
from analyzer.requirement_parser import (
    CertificationRequirement,
    EducationRequirement,
    ExperienceRequirement,
    ParsedRequirements,
    RequirementParser,
)
from analyzer.scoring import ScoreResult, ScoringEngine
from analyzer.skill_matcher import MatchResult, SkillMatcher

__all__ = [
    # Keyword extraction
    "KeywordExtractor",
    "ExtractedKeyword",
    # Requirement parsing
    "RequirementParser",
    "ParsedRequirements",
    "ExperienceRequirement",
    "EducationRequirement",
    "CertificationRequirement",
    # Skill matching
    "SkillMatcher",
    "MatchResult",
    # Scoring
    "ScoringEngine",
    "ScoreResult",
]
