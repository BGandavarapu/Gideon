"""
NVIDIA NIM API wrapper for AI-powered resume content rewriting.

Uses the ``openai`` SDK with the OpenAI-compatible NVIDIA NIM endpoint:
    - **Model**: ``nvidia/llama-3.3-nemotron-super-49b-v1.5``
    - **Base URL**: ``https://integrate.api.nvidia.com/v1``
    - **Quota**: tracked in ``data/nvidia_usage.json`` under the ``"nvidia"`` key.

Key design decisions
---------------------
- A single model handles all tasks (bullet rewrites and professional summaries).
- Rate limiting is delegated to :class:`~resume_engine.rate_limiter.RateLimiter`
  so all quota logic lives in one place.
- Every public method has a hard fallback: if the API call fails for any
  reason the *original* text is returned unchanged and the error is logged.
- Prompts include explicit "return ONLY the rewritten text" instructions to
  prevent the model from wrapping output in markdown or explanatory prose.
- Response cleaning strips asterisks, quote characters, and common markdown
  artefacts that the model occasionally injects despite instructions.
- Temperature is fixed at 0.5 (creative but consistent) to reduce hallucination risk.
"""

import logging
import os
import time
from typing import List, Optional

from resume_engine.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# NVIDIA NIM — sole model for all rewriting tasks
_NVIDIA_MODEL_ID = "nvidia/llama-3.3-nemotron-super-49b-v1.5"
_NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"

# Hard cap: truncate prompts at this many characters to stay within TPM limits
_MAX_PROMPT_CHARS = 8_000


def _clean_response(text: str) -> str:
    """Strip common formatting artefacts from a model response.

    Args:
        text: Raw response text from the API.

    Returns:
        Cleaned plain-text string.
    """
    # Remove markdown bold/italic markers
    text = text.replace("**", "").replace("__", "").replace("*", "")
    # Strip surrounding quotes that the model sometimes adds
    text = text.strip("\"'`")
    # Collapse multiple blank lines
    import re
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class Rewriter:
    """AI-powered resume content rewriter using NVIDIA NIM (Nemotron).

    Sends prompts to ``nvidia/llama-3.3-nemotron-super-49b-v1.5`` via the
    OpenAI-compatible NVIDIA NIM endpoint.

    Args:
        api_key: NVIDIA API key.  Defaults to the ``NVIDIA_API_KEY``
            environment variable.
        max_retries: Number of retry attempts on transient API errors.

    Attributes:
        api_call_count: Total successful API calls made by this instance.
        model_id: The NVIDIA model string.

    Raises:
        ValueError: If no API key can be located.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        max_retries: int = 3,
    ) -> None:
        nvidia_key = api_key or os.getenv("NVIDIA_API_KEY")
        if not nvidia_key:
            raise ValueError(
                "NVIDIA_API_KEY not found. Set it in your .env file or pass "
                "it explicitly to Rewriter()."
            )

        from openai import OpenAI as _OpenAI
        self._nvidia_client = _OpenAI(
            base_url=_NVIDIA_BASE_URL,
            api_key=nvidia_key,
        )
        self._nvidia_limiter: RateLimiter = RateLimiter(
            rpm=60,
            rpd=5_000,
            model_key="nvidia",
        )

        # Single limiter exposed for backwards-compatible callers
        self._limiter = self._nvidia_limiter

        self.model_id: str = _NVIDIA_MODEL_ID
        self._max_retries: int = max_retries
        self.api_call_count: int = 0

        logger.info(
            "Rewriter initialised — model=%s (%d RPM / %d RPD)",
            _NVIDIA_MODEL_ID,
            self._nvidia_limiter.rpm,
            self._nvidia_limiter.rpd,
        )

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def rewrite_bullet_point(
        self,
        original_bullet: str,
        job_keywords: List[str],
        job_context: str,
        job_description: str = "",
        style_fingerprint: Optional[dict] = None,
    ) -> str:
        """Rewrite a single resume bullet point to align with job requirements.

        Args:
            original_bullet: Original achievement text (one sentence).
            job_keywords: Relevant keywords from the job description (top 5–8
                are used to keep the prompt focused).
            job_context: Brief job context, e.g. ``"Senior Python Developer
                at Acme Corp"``.
            job_description: Excerpt from the job posting so the AI can
                understand what the employer is looking for.
            style_fingerprint: Optional style fingerprint dict from
                :class:`~resume_engine.style_extractor.StyleExtractor`.
                When provided, voice, length, metric, and format
                constraints are appended to the prompt as hard rules.

        Returns:
            Rewritten bullet point string, ATS-friendly.
        """
        if not original_bullet or not original_bullet.strip():
            return original_bullet

        kw_sample = ", ".join(job_keywords[:8]) if job_keywords else "general software engineering"

        # Build job requirements excerpt (first 500 chars of description)
        jd_block = ""
        if job_description:
            jd_block = (
                f"\nJOB REQUIREMENTS EXCERPT:\n{job_description[:500]}\n"
            )

        prompt = (
            "You are an expert resume writer customizing a resume for a specific job. "
            "Rewrite the achievement below to align with what this employer is looking for. "
            "Follow ALL rules strictly.\n\n"
            f"ORIGINAL ACHIEVEMENT:\n{original_bullet}\n\n"
            f"TARGET JOB: {job_context}\n"
            f"{jd_block}"
            f"KEY SKILLS THE EMPLOYER WANTS: {kw_sample}\n\n"
            "RULES:\n"
            "1. TRUTHFUL ONLY - rephrase using the employer's language, never fabricate new experience or skills.\n"
            "2. PRESERVE all numbers, percentages, and measurable results exactly.\n"
            "3. Reframe the experience to emphasize aspects most relevant to THIS specific job.\n"
            "4. Use terminology and keywords from the job description where they honestly apply.\n"
            "5. Start with a strong action verb (developed, implemented, optimised, led, built, engineered).\n"
            "6. LENGTH: 12-25 words.\n"
            "7. ATS-friendly: plain text, no bullets, no markdown, no punctuation at end.\n"
            "8. Return ONLY the rewritten bullet. No explanation, no preamble."
        )

        style_block = self._build_style_constraints(style_fingerprint)
        if style_block:
            prompt += (
                "\n\nSTYLE CONSTRAINTS (these are HARD RULES, "
                "not suggestions — violating any of these is wrong):\n"
                + style_block
            )

        result = self._call_nvidia(prompt)
        if result:
            logger.info("Bullet rewritten via NVIDIA NIM (%d->%d chars).",
                        len(original_bullet), len(result))
            return result

        logger.warning("Bullet rewrite failed (NVIDIA NIM unavailable) - returning original.")
        return original_bullet

    def generate_professional_summary(
        self,
        original_summary: str,
        job_title: str,
        keywords: List[str],
        years_experience: int = 0,
        job_description: str = "",
        style_fingerprint: Optional[dict] = None,
    ) -> str:
        """Generate a tailored professional summary for a specific job.

        Args:
            original_summary: Existing summary from the master resume.
            job_title: Target job title (e.g. ``"Senior Python Developer"``).
            keywords: Key skills and technologies required by the job.
            years_experience: Total years of professional experience (used
                to keep the summary accurate).
            job_description: Excerpt from the job posting for context.
            style_fingerprint: Optional style fingerprint dict. When provided,
                voice constraints are explicitly enforced in the summary.

        Returns:
            Rewritten 2–3 sentence professional summary, or the original
            summary on API failure.
        """
        if not original_summary:
            return original_summary

        exp_phrase = f"{years_experience}+ years of experience" if years_experience else "proven experience"
        kw_sample = ", ".join(keywords[:8]) if keywords else "software engineering"

        jd_block = ""
        if job_description:
            jd_block = f"JOB REQUIREMENTS EXCERPT:\n{job_description[:500]}\n\n"

        prompt = (
            "You are an expert resume writer customizing a summary for a specific job. "
            "Rewrite the professional summary below to directly address what this employer "
            "is looking for.\n\n"
            f"ORIGINAL SUMMARY:\n{original_summary}\n\n"
            f"TARGET ROLE: {job_title}\n"
            f"CANDIDATE EXPERIENCE: {exp_phrase}\n"
            f"{jd_block}"
            f"KEY SKILLS TO HIGHLIGHT: {kw_sample}\n\n"
            "RULES:\n"
            "1. TRUTHFUL ONLY - base everything on the original summary content.\n"
            "2. 2-3 sentences, 40-60 words total.\n"
            "3. Open with the job title or a senior professional label.\n"
            "4. Incorporate 3-5 keywords from the job description naturally.\n"
            "5. End with a value proposition statement relevant to this specific role.\n"
            "6. Plain text only - no markdown, no bullet points.\n"
            "7. Return ONLY the summary. No preamble, no explanation."
        )

        style_block = self._build_style_constraints(style_fingerprint)
        if style_block:
            prompt += (
                "\n\nSTYLE CONSTRAINTS (these are HARD RULES, "
                "not suggestions — violating any of these is wrong):\n"
                + style_block
            )
            # Extra summary-specific voice instruction
            voice = (style_fingerprint or {}).get("voice", "no_pronouns")
            if voice == "first_person":
                prompt += "\n  • The summary must be written in first person throughout."
            elif voice == "no_pronouns":
                prompt += (
                    "\n  • The summary must omit first-person pronouns. Use implied "
                    "third-person style: 'Experienced engineer with...' not 'I am an...'"
                )

        result = self._call_nvidia(prompt)
        if result:
            logger.info("Generated professional summary via NVIDIA NIM (%d chars).", len(result))
            return result

        logger.warning("Summary generation failed (NVIDIA NIM unavailable) - returning original.")
        return original_summary

    # ------------------------------------------------------------------
    # Style helpers
    # ------------------------------------------------------------------

    def _build_style_constraints(self, style: Optional[dict]) -> str:
        """Convert a style fingerprint into hard constraint instructions.

        Args:
            style: Style fingerprint dict from
                :class:`~resume_engine.style_extractor.StyleExtractor`,
                or ``None`` / empty dict.

        Returns:
            Formatted constraint string to inject into a prompt,
            or ``""`` if *style* is falsy.
        """
        if not style:
            return ""

        constraints = []

        # Voice constraint
        voice = style.get("voice", "no_pronouns")
        if voice == "first_person":
            constraints.append(
                "VOICE: Use first-person pronouns (I, my, me). "
                "e.g. 'I led a team' not 'Led a team'"
            )
        elif voice == "third_person":
            constraints.append(
                "VOICE: Use third-person. No first-person pronouns."
            )
        else:
            constraints.append(
                "VOICE: Omit all pronouns. Start bullets with action "
                "verbs. e.g. 'Led team of 5' not 'I led team of 5'"
            )

        # Sentence structure constraint
        structure = style.get("sentence_structure", {})
        style_type = structure.get("style", "moderate")
        avg_wc = structure.get("avg_word_count", 15)
        if style_type == "punchy":
            constraints.append(
                f"LENGTH: Keep bullets SHORT and punchy. "
                f"Target {int(avg_wc)} words or fewer. "
                f"No filler words. Cut ruthlessly."
            )
        elif style_type == "detailed":
            constraints.append(
                f"LENGTH: Write detailed bullets. "
                f"Target {int(avg_wc)} words. Include context and impact."
            )
        else:
            constraints.append(
                f"LENGTH: Moderate bullet length. "
                f"Target {int(avg_wc)} words."
            )

        # Metric density constraint
        metrics = style.get("metric_usage", {})
        density = metrics.get("density", "moderate")
        if density == "heavy":
            constraints.append(
                "METRICS: This resume is data-driven. "
                "PRESERVE all numbers, percentages, and dollar amounts "
                "from the original bullet EXACTLY. "
                "Do NOT remove or replace metrics with vague language. "
                "If original has '40% reduction', rewritten must too."
            )
        elif density == "light":
            constraints.append(
                "METRICS: Do NOT inject metrics or numbers that were not "
                "in the original bullet. Keep language qualitative."
            )
        else:
            constraints.append(
                "METRICS: Preserve existing metrics exactly. "
                "Do not add or remove numbers."
            )

        # Format constraint
        fmt = style.get("format", {})
        cap = fmt.get("capitalization", "upper")
        period = fmt.get("trailing_period", False)

        format_rules = []
        if cap == "upper":
            format_rules.append("Start with a capital letter")
        else:
            format_rules.append("Start with a lowercase letter")
        if period:
            format_rules.append("End with a period")
        else:
            format_rules.append("Do NOT end with a period")
        constraints.append(
            f"FORMAT: {'. '.join(format_rules)}. "
            "Do not include the bullet character itself in your response "
            "— return only the text content of the bullet."
        )

        return "\n".join(f"  • {c}" for c in constraints)

    def suggest_skills_reorder(
        self,
        current_skills: List[str],
        job_keywords: List[str],
    ) -> List[str]:
        """Reorder skills list to put job-relevant skills first.

        This method does NOT call the API — it performs a deterministic
        sort so no quota is consumed and the result is reproducible.

        Args:
            current_skills: Skills list from the master resume.
            job_keywords: Required/preferred keywords from the job.

        Returns:
            Reordered list with job-matching skills at the top.
        """
        if not current_skills:
            return current_skills

        keywords_lower = {kw.lower() for kw in job_keywords}

        def _rank(skill: str) -> int:
            return 0 if skill.lower() in keywords_lower else 1

        reordered = sorted(current_skills, key=_rank)
        logger.debug(
            "Skills reordered: %d job-relevant first out of %d total.",
            sum(1 for s in current_skills if s.lower() in keywords_lower),
            len(current_skills),
        )
        return reordered

    def batch_rewrite_bullets(
        self,
        bullets: List[str],
        job_keywords: List[str],
        job_context: str,
        job_description: str = "",
        max_rewrites: int = 10,
        style_fingerprint: Optional[dict] = None,
    ) -> List[str]:
        """Rewrite up to *max_rewrites* bullets from a list.

        Bullets with the most keyword overlap are prioritised, but ALL
        selected bullets are rewritten regardless of overlap score — the
        AI uses the full job description to reframe each bullet.

        Args:
            bullets: All bullet points for a resume section.
            job_keywords: Job description keywords.
            job_context: Brief job context string.
            job_description: Excerpt from the job posting for context.
            max_rewrites: Maximum number of bullets to rewrite (default 10).
            style_fingerprint: Optional style fingerprint passed through to
                :meth:`rewrite_bullet_point`.

        Returns:
            List of bullet strings with selected bullets rewritten.
        """
        if not bullets:
            return bullets

        # Score each bullet by keyword overlap to prioritise order
        kw_lower = {kw.lower() for kw in job_keywords}
        scores = [
            sum(1 for kw in kw_lower if kw in bullet.lower())
            for bullet in bullets
        ]

        # Select top bullets by score, but rewrite ALL of them (no score>0 gate)
        top_indices = set(
            sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:max_rewrites]
        )

        result: List[str] = []
        for idx, bullet in enumerate(bullets):
            if idx in top_indices:
                rewritten = self.rewrite_bullet_point(
                    bullet, job_keywords, job_context,
                    job_description=job_description,
                    style_fingerprint=style_fingerprint,
                )
                result.append(rewritten)
            else:
                result.append(bullet)

        return result

    def usage_stats(self) -> dict:
        """Return usage statistics for the NVIDIA NIM model.

        Returns:
            Dictionary with ``nvidia`` stats sub-dict plus the instance-level
            API call count.
        """
        nvidia_stats = self._nvidia_limiter.stats()
        return {
            "nvidia": nvidia_stats,
            "calls_today": nvidia_stats["calls_today"],
            "calls_remaining_today": nvidia_stats["calls_remaining_today"],
            "instance_calls": self.api_call_count,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _call_nvidia(self, prompt: str) -> Optional[str]:
        """Send a prompt to NVIDIA NIM (Nemotron).

        Args:
            prompt: Full prompt string to send.

        Returns:
            Cleaned response text, or ``None`` if the call fails for any
            reason (caller returns original text).
        """
        if self._nvidia_client is None:
            return None

        if len(prompt) > _MAX_PROMPT_CHARS:
            prompt = prompt[:_MAX_PROMPT_CHARS]
            logger.debug("NVIDIA prompt truncated to %d chars.", _MAX_PROMPT_CHARS)

        try:
            self._nvidia_limiter.acquire()
            response = self._nvidia_client.chat.completions.create(
                model=_NVIDIA_MODEL_ID,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.5,
                max_tokens=1024,
            )
            raw_text = (response.choices[0].message.content or "").strip()
            cleaned = _clean_response(raw_text)
            if not cleaned:
                logger.warning("Empty response from NVIDIA NIM.")
                return None
            self.api_call_count += 1
            self._nvidia_limiter.record_tokens(len(prompt), len(raw_text))
            logger.debug("NVIDIA NIM call #%d succeeded (%d chars).",
                         self.api_call_count, len(cleaned))
            return cleaned
        except Exception as exc:
            logger.warning("NVIDIA NIM error: %s", exc)
            return None
