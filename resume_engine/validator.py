"""
Content validator for AI-generated resume modifications.

Ensures that the NIM model's output is:

1. **Truthful** – no numbers fabricated; metrics from original are preserved.
2. **Professional** – no slang, no unprofessional superlatives.
3. **Appropriate length** – bullet points 5–30 words; summaries 30–80 words.
4. **Free of hallucinated skills** – no technical terms introduced that
   weren't present in either the original resume or the job description.

All validation functions return a ``(is_valid, warnings)`` tuple so callers
can choose whether to accept a flagged modification or fall back to the
original.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_UNPROFESSIONAL_WORDS: Set[str] = {
    "amazing", "awesome", "crushed", "killed", "smashed", "slayed",
    "rockstar", "ninja", "guru", "wizard", "badass", "epic", "insane",
    "literally", "basically", "totally", "super", "mega",
}

_FILLER_PHRASES: Set[str] = {
    "responsible for", "duties included", "worked on", "helped with",
    "assisted in", "participated in",
}

# Common English words and resume action verbs that are NOT skill names.
# These are excluded from the hallucination check so the validator doesn't
# flag ordinary sentence words as fabricated technical skills.
_COMMON_WORDS: Set[str] = {
    # Action verbs (resume staples)
    "developed", "implemented", "designed", "built", "created", "managed",
    "led", "architected", "optimised", "optimized", "improved", "delivered",
    "deployed", "maintained", "integrated", "automated", "reduced", "increased",
    "collaborated", "leveraged", "utilized", "utilised", "contributed",
    "supported", "established", "streamlined", "migrated", "refactored",
    "ensured", "monitored", "diagnosed", "resolved", "documented", "tested",
    "reviewed", "analysed", "analyzed", "enhanced", "expanded", "launched",
    # Common adjectives / nouns in resume writing
    "senior", "junior", "lead", "principal", "engineer", "developer",
    "application", "applications", "system", "systems", "service", "services",
    "solution", "solutions", "platform", "infrastructure", "performance",
    "scalable", "robust", "reliable", "efficient", "modern", "best",
    "practices", "patterns", "standards", "processes", "workflow", "workflows",
    # Prepositions / connectives that get capitalised at sentence start
    "the", "and", "for", "with", "using", "via", "across", "within",
    "including", "through", "resulting", "enabling", "achieving",
    # Very common abbreviations that are NOT standalone skills
    "rest", "api", "apis", "sdk", "sdks", "oop", "tdd", "bdd", "cli",
    "ide", "saas", "paas", "iaas", "http", "https", "json", "xml", "csv",
    "sql", "nosql", "orm", "mvc", "mvp", "mvvm", "spa", "ssr",
}

# Minimum word counts
_BULLET_MIN_WORDS = 5
_BULLET_MAX_WORDS = 30
_SUMMARY_MIN_WORDS = 15
_SUMMARY_MAX_WORDS = 100


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """Result of a single validation pass.

    Attributes:
        is_valid: ``True`` if no warnings were raised.
        warnings: Human-readable list of issues found.
        score: Rough quality score 0–100 (100 = no issues, deducted per warning).
    """

    is_valid: bool
    warnings: List[str] = field(default_factory=list)
    score: float = 100.0

    def to_dict(self) -> dict:
        """Serialise to JSON-compatible dictionary."""
        return {
            "is_valid": self.is_valid,
            "warnings": self.warnings,
            "score": round(self.score, 1),
        }


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class ContentValidator:
    """Validate AI-generated resume modifications for truthfulness and quality.

    All public methods return :class:`ValidationResult` objects.  Warnings
    are non-fatal by default; callers decide whether to reject the modification.
    """

    # ------------------------------------------------------------------
    # Bullet-point validation
    # ------------------------------------------------------------------

    def validate_bullet(
        self,
        original: str,
        modified: str,
        known_skills: Optional[List[str]] = None,
    ) -> ValidationResult:
        """Validate a single modified bullet point.

        Checks performed:
        - Metrics preservation (numbers / percentages)
        - Word count in range [5, 30]
        - No unprofessional language
        - No weak filler phrases
        - No fabricated technical skills (if *known_skills* supplied)

        Args:
            original: The original bullet text.
            modified: The AI-modified bullet text.
            known_skills: Optional combined list of resume + job skills;
                any technical term in *modified* not in this list triggers
                a warning.

        Returns:
            :class:`ValidationResult` instance.
        """
        warnings: List[str] = []

        # --- Metrics preservation ---
        orig_numbers = set(re.findall(r"\d+\.?\d*\s*%?", original))
        mod_numbers = set(re.findall(r"\d+\.?\d*\s*%?", modified))
        missing_metrics = orig_numbers - mod_numbers
        if missing_metrics:
            warnings.append(
                f"Quantifiable metrics removed: {', '.join(sorted(missing_metrics))}"
            )

        # --- Word count ---
        word_count = len(modified.split())
        if word_count < _BULLET_MIN_WORDS:
            warnings.append(
                f"Bullet too short: {word_count} words (min {_BULLET_MIN_WORDS})"
            )
        if word_count > _BULLET_MAX_WORDS:
            warnings.append(
                f"Bullet too long: {word_count} words (max {_BULLET_MAX_WORDS})"
            )

        # --- Unprofessional language ---
        mod_lower = modified.lower()
        found_unprofessional = [w for w in _UNPROFESSIONAL_WORDS if w in mod_lower]
        if found_unprofessional:
            warnings.append(
                f"Unprofessional language detected: {', '.join(found_unprofessional)}"
            )

        # --- Weak filler phrases ---
        found_filler = [p for p in _FILLER_PHRASES if p in mod_lower]
        if found_filler:
            warnings.append(
                f"Weak filler phrase detected: '{found_filler[0]}'"
            )

        # --- Hallucinated skills ---
        if known_skills is not None:
            _warn = self._check_new_skills(original, modified, known_skills)
            if _warn:
                warnings.append(_warn)

        # --- Empty / near-empty ---
        if not modified.strip():
            warnings.append("Modified bullet is empty.")

        score = max(0.0, 100.0 - len(warnings) * 20.0)
        return ValidationResult(
            is_valid=len(warnings) == 0,
            warnings=warnings,
            score=score,
        )

    # ------------------------------------------------------------------
    # Professional summary validation
    # ------------------------------------------------------------------

    def validate_summary(
        self,
        original: str,
        modified: str,
        known_skills: Optional[List[str]] = None,
    ) -> ValidationResult:
        """Validate an AI-modified professional summary.

        Args:
            original: Original summary text.
            modified: AI-modified summary text.
            known_skills: Optional combined skill list for hallucination check.

        Returns:
            :class:`ValidationResult` instance.
        """
        warnings: List[str] = []

        word_count = len(modified.split())
        if word_count < _SUMMARY_MIN_WORDS:
            warnings.append(
                f"Summary too short: {word_count} words (min {_SUMMARY_MIN_WORDS})"
            )
        if word_count > _SUMMARY_MAX_WORDS:
            warnings.append(
                f"Summary too long: {word_count} words (max {_SUMMARY_MAX_WORDS})"
            )

        mod_lower = modified.lower()
        found_unprofessional = [w for w in _UNPROFESSIONAL_WORDS if w in mod_lower]
        if found_unprofessional:
            warnings.append(
                f"Unprofessional language: {', '.join(found_unprofessional)}"
            )

        if known_skills is not None:
            _warn = self._check_new_skills(original, modified, known_skills)
            if _warn:
                warnings.append(_warn)

        if not modified.strip():
            warnings.append("Modified summary is empty.")

        score = max(0.0, 100.0 - len(warnings) * 20.0)
        return ValidationResult(
            is_valid=len(warnings) == 0,
            warnings=warnings,
            score=score,
        )

    # ------------------------------------------------------------------
    # Full-resume validation
    # ------------------------------------------------------------------

    def validate_full_resume(
        self,
        original_data: dict,
        modified_data: dict,
        known_skills: Optional[List[str]] = None,
    ) -> dict:
        """Validate every modified section of a tailored resume.

        Args:
            original_data: Master resume content dictionary.
            modified_data: Tailored resume content dictionary.
            known_skills: Combined resume + job skills for hallucination checks.

        Returns:
            Dictionary with per-section validation results and an
            ``overall_valid`` boolean.
        """
        results: Dict[str, object] = {}
        all_valid = True

        # --- Professional summary ---
        orig_summary = original_data.get("professional_summary", "")
        mod_summary = modified_data.get("professional_summary", "")
        if orig_summary and mod_summary and orig_summary != mod_summary:
            vr = self.validate_summary(orig_summary, mod_summary, known_skills)
            results["professional_summary"] = vr.to_dict()
            if not vr.is_valid:
                all_valid = False

        # --- Work experience bullets ---
        orig_exp = original_data.get("work_experience", [])
        mod_exp = modified_data.get("work_experience", [])
        bullet_results: List[dict] = []
        for orig_pos, mod_pos in zip(orig_exp, mod_exp):
            orig_bullets = orig_pos.get("bullets", [])
            mod_bullets = mod_pos.get("bullets", [])
            pos_title = orig_pos.get("title", "Unknown")
            for orig_b, mod_b in zip(orig_bullets, mod_bullets):
                if orig_b != mod_b:
                    vr = self.validate_bullet(orig_b, mod_b, known_skills)
                    bullet_results.append({
                        "position": pos_title,
                        "original": orig_b,
                        "modified": mod_b,
                        **vr.to_dict(),
                    })
                    if not vr.is_valid:
                        all_valid = False

        results["work_experience_bullets"] = bullet_results

        # --- Skills completeness ---
        orig_skills_count = len(original_data.get("skills", []))
        mod_skills_count = len(modified_data.get("skills", []))
        if mod_skills_count < orig_skills_count:
            results["skills_warning"] = (
                f"Skills count reduced: {orig_skills_count} -> {mod_skills_count}"
            )
            all_valid = False

        results["overall_valid"] = all_valid
        total_bullets = len(bullet_results)
        valid_bullets = sum(1 for b in bullet_results if b.get("is_valid", False))
        results["summary_stats"] = {
            "total_bullets_checked": total_bullets,
            "valid_bullets": valid_bullets,
            "invalid_bullets": total_bullets - valid_bullets,
        }

        logger.info(
            "Resume validation: %s  bullets=%d/%d valid",
            "PASS" if all_valid else "WARNINGS",
            valid_bullets, total_bullets,
        )
        return results

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    @staticmethod
    def _check_new_skills(original: str, modified: str, known_skills: List[str]) -> Optional[str]:
        """Check if the modified text introduces skill terms not in *known_skills*.

        Only raises a warning for multi-character tokens that look like
        technology names (capitalized, contain digits, or match known patterns).

        Args:
            original: Original text.
            modified: Modified text.
            known_skills: Allowed skill terms (lowercased matching).

        Returns:
            Warning string if new skill tokens detected, else ``None``.
        """
        known_lower = {s.lower() for s in known_skills}
        orig_tokens = {t.lower() for t in re.findall(r"[A-Za-z][A-Za-z0-9+#./\-]{2,}", original)}
        mod_tokens = {t.lower() for t in re.findall(r"[A-Za-z][A-Za-z0-9+#./\-]{2,}", modified)}

        truly_new = mod_tokens - orig_tokens - known_lower - _COMMON_WORDS
        # Only flag tokens that look like tech terms (starts with uppercase in
        # the modified text, or contains a digit, or is an acronym).
        suspicious = []
        for token in truly_new:
            # Find original casing in modified text
            match = re.search(rf"\b{re.escape(token)}\b", modified, re.IGNORECASE)
            raw = match.group(0) if match else token
            # Skip single-capital words at sentence start (likely just capitalisation)
            if raw[0].isupper() and len(raw) <= 4 and not any(c.isdigit() for c in raw):
                continue
            if raw[0].isupper() or any(c.isdigit() for c in raw):
                suspicious.append(raw)

        if suspicious:
            return f"Possible hallucinated skill(s): {', '.join(sorted(suspicious)[:5])}"
        return None
