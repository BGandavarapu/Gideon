"""StyleExtractor — analyse a master resume and produce a style fingerprint.

The fingerprint captures five orthogonal style dimensions so that the
Gemini rewriter can reproduce the original author's voice and format in
every tailored variant:

    1. Voice           – first_person / third_person / no_pronouns
    2. Sentence length – punchy / moderate / detailed
    3. Metric density  – heavy / moderate / light
    4. Section order   – ordered list of non-empty top-level keys
    5. Format          – bullet character, capitalisation, trailing period

Usage::

    extractor = StyleExtractor()
    fingerprint = extractor.extract(master_resume.content)
    # Pass fingerprint to GeminiRewriter.rewrite_bullet_point(
    #     ..., style_fingerprint=fingerprint)
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Regex: any sequence that looks like a quantifiable metric
_METRIC_RE = re.compile(r"\d+[%x]?|\$[\d,]+|\d+\.\d+")

# Leading bullet characters to look for
_BULLET_CHARS = ("•", "-", "*", "—")


class StyleExtractor:
    """Analyse a MasterResume content dict and return a style fingerprint.

    All methods are designed to be robust to missing or malformed content —
    they always return safe defaults rather than raising exceptions.
    """

    # Canonical section key order (used to detect section order)
    _SECTION_KEYS = [
        "personal_info",
        "professional_summary",
        "work_experience",
        "education",
        "certifications",
        "skills",
        "projects",
        "awards",
        "publications",
        "volunteer",
        "languages",
    ]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract(self, resume_content: dict) -> dict:
        """Analyse *resume_content* and return a style fingerprint dict.

        Args:
            resume_content: JSON dict stored in
                :attr:`~database.models.MasterResume.content`.

        Returns:
            Style fingerprint dict — see module docstring for schema.
            Never raises; returns safe defaults on malformed input.
        """
        try:
            return self._extract_unsafe(resume_content)
        except Exception as exc:  # pragma: no cover
            logger.warning("StyleExtractor.extract() failed (%s) — returning defaults.", exc)
            return self._default_fingerprint()

    # ------------------------------------------------------------------
    # Private orchestrator
    # ------------------------------------------------------------------

    def _extract_unsafe(self, resume_content: dict) -> dict:
        if not isinstance(resume_content, dict):
            return self._default_fingerprint()

        bullets = self._collect_all_bullets(resume_content)
        all_text = self._collect_all_text(resume_content)

        voice = self._detect_voice(all_text)
        sentence_structure = self._detect_sentence_structure(bullets)
        metric_usage = self._detect_metric_usage(bullets)
        structure = self._detect_structure(resume_content)
        fmt = self._detect_format(bullets)

        return {
            "voice": voice,
            "sentence_structure": sentence_structure,
            "metric_usage": metric_usage,
            "structure": structure,
            "format": fmt,
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "bullet_count": len(bullets),
        }

    # ------------------------------------------------------------------
    # Dimension detectors
    # ------------------------------------------------------------------

    def _detect_voice(self, all_text: str) -> str:
        """Detect the narrative voice used across summary and bullets.

        Args:
            all_text: Concatenated summary + all bullet text.

        Returns:
            ``"first_person"``, ``"third_person"``, or ``"no_pronouns"``.
        """
        if not all_text:
            return "no_pronouns"

        text_lower = all_text.lower()

        # First-person indicators
        fp_pattern = re.compile(r"\b(i|my|me|i've|i'm|i've|i am|i have)\b")
        first_person_count = len(fp_pattern.findall(text_lower))

        # Third-person indicators (pronouns only — proper nouns are ambiguous)
        tp_pattern = re.compile(r"\b(he|she|they|his|her|their|him|them)\b")
        third_person_count = len(tp_pattern.findall(text_lower))

        if first_person_count > 2:
            return "first_person"
        if third_person_count > 2:
            return "third_person"
        return "no_pronouns"

    def _detect_sentence_structure(self, bullets: List[str]) -> dict:
        """Analyse bullet-point lengths to classify sentence structure.

        Args:
            bullets: Flat list of bullet strings.

        Returns:
            Dict with ``style``, ``avg_word_count``, ``min_word_count``,
            ``max_word_count``.
        """
        if not bullets:
            return {
                "style": "moderate",
                "avg_word_count": 0.0,
                "min_word_count": 0,
                "max_word_count": 0,
            }

        word_counts = [len(b.split()) for b in bullets if b.strip()]
        if not word_counts:
            return {
                "style": "moderate",
                "avg_word_count": 0.0,
                "min_word_count": 0,
                "max_word_count": 0,
            }

        avg = sum(word_counts) / len(word_counts)

        if avg <= 12:
            style = "punchy"
        elif avg <= 20:
            style = "moderate"
        else:
            style = "detailed"

        return {
            "style": style,
            "avg_word_count": round(avg, 1),
            "min_word_count": min(word_counts),
            "max_word_count": max(word_counts),
        }

    def _detect_metric_usage(self, bullets: List[str]) -> dict:
        """Measure how data-driven the resume's bullets are.

        Args:
            bullets: Flat list of bullet strings.

        Returns:
            Dict with ``density``, ``ratio``, ``bullets_with_metrics``,
            ``total_bullets``.
        """
        total = len(bullets)
        if total == 0:
            return {
                "density": "light",
                "ratio": 0.0,
                "bullets_with_metrics": 0,
                "total_bullets": 0,
            }

        with_metrics = sum(1 for b in bullets if _METRIC_RE.search(b))
        ratio = with_metrics / total

        if ratio > 0.40:
            density = "heavy"
        elif ratio >= 0.20:
            density = "moderate"
        else:
            density = "light"

        return {
            "density": density,
            "ratio": round(ratio, 4),
            "bullets_with_metrics": with_metrics,
            "total_bullets": total,
        }

    def _detect_structure(self, resume_content: dict) -> List[str]:
        """Return an ordered list of non-empty section keys.

        The order follows :attr:`_SECTION_KEYS` for keys that are in the
        known list; unknown keys are appended in their original iteration
        order.

        Args:
            resume_content: Resume content dict.

        Returns:
            Ordered list of non-empty section key strings.
        """
        def _is_non_empty(val: Any) -> bool:
            if val is None:
                return False
            if isinstance(val, (list, dict, str)):
                return bool(val)
            return True

        non_empty_keys = {k for k, v in resume_content.items() if _is_non_empty(v)}

        ordered: List[str] = []
        for key in self._SECTION_KEYS:
            if key in non_empty_keys:
                ordered.append(key)

        # Append any keys not in the canonical list
        for key in resume_content:
            if key not in ordered and key in non_empty_keys:
                ordered.append(key)

        return ordered

    def _detect_format(self, bullets: List[str]) -> dict:
        """Detect bullet formatting conventions.

        Args:
            bullets: Flat list of bullet strings.

        Returns:
            Dict with ``bullet_char``, ``capitalization``,
            ``trailing_period``.
        """
        if not bullets:
            return {
                "bullet_char": "none",
                "capitalization": "upper",
                "trailing_period": False,
            }

        # Bullet character detection
        char_counts: Dict[str, int] = {c: 0 for c in _BULLET_CHARS}
        for bullet in bullets:
            stripped = bullet.strip()
            if not stripped:
                continue
            for ch in _BULLET_CHARS:
                if stripped.startswith(ch):
                    char_counts[ch] += 1
                    break

        most_common_char = max(char_counts, key=lambda c: char_counts[c])
        bullet_char = (
            most_common_char
            if char_counts[most_common_char] > 0
            else "none"
        )

        # Capitalisation: strip leading bullet char first
        def _first_letter(bullet: str) -> str:
            s = bullet.strip()
            for ch in _BULLET_CHARS:
                if s.startswith(ch):
                    s = s[len(ch):].strip()
                    break
            return s[0] if s else ""

        first_letters = [_first_letter(b) for b in bullets if _first_letter(b)]
        upper_count = sum(1 for c in first_letters if c.isupper())
        capitalization = (
            "upper"
            if (upper_count / len(first_letters) > 0.80 if first_letters else True)
            else "lower"
        )

        # Trailing period
        period_count = sum(1 for b in bullets if b.rstrip().endswith("."))
        trailing_period = period_count / len(bullets) > 0.60

        return {
            "bullet_char": bullet_char,
            "capitalization": capitalization,
            "trailing_period": trailing_period,
        }

    # ------------------------------------------------------------------
    # Content collectors
    # ------------------------------------------------------------------

    def _collect_all_bullets(self, resume_content: dict) -> List[str]:
        """Flatten all bullets from work_experience and projects.

        Args:
            resume_content: Resume content dict.

        Returns:
            Flat list of bullet strings (empty list on missing keys).
        """
        bullets: List[str] = []

        for section_key in ("work_experience", "projects"):
            section = resume_content.get(section_key, [])
            if not isinstance(section, list):
                continue
            for entry in section:
                if not isinstance(entry, dict):
                    continue
                entry_bullets = entry.get("bullets", [])
                if isinstance(entry_bullets, list):
                    bullets.extend(str(b) for b in entry_bullets if b)

        return bullets

    def _collect_all_text(self, resume_content: dict) -> str:
        """Concatenate summary and all bullets into one string for voice detection.

        Args:
            resume_content: Resume content dict.

        Returns:
            Combined text string.
        """
        parts: List[str] = []

        summary = resume_content.get("professional_summary", "")
        if isinstance(summary, str) and summary:
            parts.append(summary)

        parts.extend(self._collect_all_bullets(resume_content))
        return " ".join(parts)

    # ------------------------------------------------------------------
    # Defaults
    # ------------------------------------------------------------------

    @staticmethod
    def _default_fingerprint() -> dict:
        """Return a safe all-defaults fingerprint."""
        return {
            "voice": "no_pronouns",
            "sentence_structure": {
                "style": "moderate",
                "avg_word_count": 0.0,
                "min_word_count": 0,
                "max_word_count": 0,
            },
            "metric_usage": {
                "density": "light",
                "ratio": 0.0,
                "bullets_with_metrics": 0,
                "total_bullets": 0,
            },
            "structure": [],
            "format": {
                "bullet_char": "none",
                "capitalization": "upper",
                "trailing_period": False,
            },
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "bullet_count": 0,
        }
