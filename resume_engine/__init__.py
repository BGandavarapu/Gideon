"""
resume_engine – AI-powered resume tailoring for Gideon (Phase 4).

Public surface
--------------
GeminiRewriter      Gemini API wrapper: rewrite bullets, generate summaries,
                    reorder skills, batch-process content.
RateLimiter         Thread-safe token-bucket limiter with daily quota tracking.
QuotaExceededError  Raised when the daily Gemini quota would be exceeded.
ContentValidator    Validate AI output for truthfulness, metrics, and quality.
ValidationResult    Single validation result with warnings and score.
ResumeModifier      Orchestrate full resume tailoring against a job posting.
ModificationEntry   Single logged change with before/after text.
ModificationResult  Complete output of a modify_resume() call.
"""

from resume_engine.gemini_rewriter import GeminiRewriter
from resume_engine.modifier import ModificationEntry, ModificationResult, ResumeModifier
from resume_engine.rate_limiter import QuotaExceededError, RateLimiter
from resume_engine.validator import ContentValidator, ValidationResult

__all__ = [
    "GeminiRewriter",
    "RateLimiter",
    "QuotaExceededError",
    "ContentValidator",
    "ValidationResult",
    "ResumeModifier",
    "ModificationEntry",
    "ModificationResult",
]
