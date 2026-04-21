"""
Resume modification orchestrator for Phase 4.

:class:`ResumeModifier` ties together the NIM rewriter, content
validator, and keyword analyser to produce a fully tailored resume from a
master resume + job description pair.

Modification strategy
---------------------
1. **Professional summary** – always rewritten to open with the target
   job title and emphasise the most relevant skills.
2. **Work experience** – top 5 bullets per position (by keyword overlap)
   are rewritten; others kept unchanged.
3. **Skills** – keywords found in the rewritten bullets/summary are
   promoted into the skills list; all skills then reordered so
   job-matching ones appear first.
4. **Education / certifications** – copied verbatim (no AI needed).
5. **Projects** – ranked by keyword overlap; top 3 included in the
   tailored resume.

Every modification is logged in :attr:`ResumeModifier.modification_log`
for full transparency, and the final resume is validated by
:class:`~resume_engine.validator.ContentValidator` before being returned.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from database.models import Job, MasterResume
from resume_engine.rewriter import Rewriter
from resume_engine.validator import ContentValidator

logger = logging.getLogger(__name__)

# Maximum bullets to rewrite per work-experience position
_MAX_BULLETS_PER_POSITION = 10

# Maximum projects to include in the tailored resume
_MAX_PROJECTS = 3


# ---------------------------------------------------------------------------
# Modification log entry
# ---------------------------------------------------------------------------


@dataclass
class ModificationEntry:
    """A single recorded change made during tailoring.

    Attributes:
        section: Which resume section was changed (e.g. ``"work_experience"``).
        field: Sub-field changed (e.g. ``"bullet"`` or ``"professional_summary"``).
        position_title: Job title of the work experience entry (if applicable).
        original: Text before modification.
        modified: Text after modification.
        accepted: Whether the modification passed validation.
        warnings: Validation warnings attached to this change.
    """

    section: str
    field: str
    original: str
    modified: str
    position_title: str = ""
    accepted: bool = True
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialise to a JSON-compatible dictionary."""
        return {
            "section": self.section,
            "field": self.field,
            "position_title": self.position_title,
            "original": self.original,
            "modified": self.modified,
            "accepted": self.accepted,
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# Modification result
# ---------------------------------------------------------------------------


@dataclass
class ModificationResult:
    """Full output of a :meth:`ResumeModifier.modify_resume` call.

    Attributes:
        content: Tailored resume content dict (same schema as
            :attr:`~database.models.MasterResume.content`).
        metrics: Keyword coverage statistics (before / after / improvement).
        modification_log: List of all changes with validation status.
        validation_report: Output of the full-resume validator.
        api_calls_used: Number of NIM API calls consumed.
    """

    content: dict
    metrics: dict
    modification_log: List[ModificationEntry]
    validation_report: dict
    api_calls_used: int = 0

    def to_dict(self) -> dict:
        """Serialise to a JSON-compatible dictionary."""
        return {
            "content": self.content,
            "metrics": self.metrics,
            "modification_log": [e.to_dict() for e in self.modification_log],
            "validation_report": self.validation_report,
            "api_calls_used": self.api_calls_used,
        }


# ---------------------------------------------------------------------------
# ResumeModifier
# ---------------------------------------------------------------------------


class ResumeModifier:
    """Orchestrate AI-powered resume tailoring for a specific job.

    Args:
        rewriter: Optional pre-built :class:`~resume_engine.rewriter.Rewriter`.
            If omitted, a new instance is created (requires ``NVIDIA_API_KEY``).
        on_progress: Optional callback ``fn(step: str, current: int, total: int)``
            invoked as each section is processed.

    Attributes:
        modification_log: Accumulated :class:`ModificationEntry` objects from
            the most recent :meth:`modify_resume` call.
    """

    def __init__(
        self,
        rewriter: Optional[Rewriter] = None,
        on_progress: Optional[Callable[[str, int, int], None]] = None,
    ) -> None:
        self._rewriter: Rewriter = rewriter or Rewriter()
        self._validator: ContentValidator = ContentValidator()
        self._on_progress = on_progress
        self.modification_log: List[ModificationEntry] = []

    # ------------------------------------------------------------------
    # Primary entry point
    # ------------------------------------------------------------------

    def modify_resume(
        self,
        master_resume: MasterResume,
        job: Job,
        match_analysis: Optional[Dict[str, Any]] = None,
        style_fingerprint: Optional[dict] = None,
    ) -> ModificationResult:
        """Create a fully tailored resume for *job*.

        Args:
            master_resume: Source :class:`~database.models.MasterResume`.
            job: Target :class:`~database.models.Job`.
            match_analysis: Optional pre-computed match dict (from
                :class:`~analyzer.scoring.ScoringEngine`).  If omitted the
                modifier works from raw keyword lists only.
            style_fingerprint: Optional style fingerprint dict from
                :class:`~resume_engine.style_extractor.StyleExtractor`.
                When provided it is passed to every NIM rewriting call
                so the tailored resume preserves the original voice, length,
                metric density, and formatting conventions.

        Returns:
            :class:`ModificationResult` with all tailored content and metadata.
        """
        self.modification_log = []
        resume_data: dict = master_resume.content if isinstance(master_resume.content, dict) else {}
        job_keywords: List[str] = job.required_skills or []
        preferred_kw: List[str] = job.preferred_skills or []
        all_keywords = list(dict.fromkeys(job_keywords + preferred_kw))
        job_context = f"{job.job_title} at {job.company_name}"

        # Prefer style from the master_resume record; caller kwarg is a fallback.
        effective_style = style_fingerprint
        if effective_style is None and hasattr(master_resume, "style_fingerprint"):
            effective_style = master_resume.style_fingerprint

        if effective_style:
            logger.info(
                "Tailoring with style constraints: voice=%s, length=%s, metrics=%s",
                effective_style.get("voice"),
                effective_style.get("sentence_structure", {}).get("style"),
                effective_style.get("metric_usage", {}).get("density"),
            )
        else:
            logger.info("No style fingerprint — tailoring without style constraints.")

        logger.info(
            "Tailoring resume '%s' for '%s'.",
            master_resume.name, job_context,
        )

        total_steps = 4
        self._progress("summary", 0, total_steps)

        # 1. Professional summary
        tailored_summary = self.tailor_professional_summary(
            summary=resume_data.get("professional_summary", ""),
            job=job,
            job_keywords=all_keywords,
            resume_data=resume_data,
            style_fingerprint=effective_style,
        )
        self._progress("experience", 1, total_steps)

        # 2. Work experience
        tailored_experience = self.tailor_work_experience(
            experience_list=resume_data.get("work_experience", []),
            job=job,
            job_keywords=all_keywords,
            style_fingerprint=effective_style,
        )
        self._progress("skills", 2, total_steps)

        # 3. Skills reorder + promote keywords found in rewritten content
        tailored_skills = self.reorder_skills(
            skills=self._promote_keywords_to_skills(
                base_skills=resume_data.get("skills", []),
                job_keywords=all_keywords,
                tailored_summary=tailored_summary,
                tailored_experience=tailored_experience,
            ),
            job_keywords=all_keywords,
        )
        self._progress("projects", 3, total_steps)

        # 4. Project selection
        tailored_projects = self.select_relevant_projects(
            projects=resume_data.get("projects", []),
            job_keywords=all_keywords,
        )
        self._progress("done", 4, total_steps)

        raw_tailored_content = {
            "personal_info": resume_data.get("personal_info", {}),
            "professional_summary": tailored_summary,
            "work_experience": tailored_experience,
            "skills": tailored_skills,
            "education": resume_data.get("education", []),
            "certifications": resume_data.get("certifications", []),
            "projects": tailored_projects,
        }

        # Preserve style-rendering hints so the PDF generator can honour
        # the original font + section header style.
        if "font_family" in resume_data:
            raw_tailored_content["font_family"] = resume_data["font_family"]
        if "section_header_style" in resume_data:
            raw_tailored_content["section_header_style"] = resume_data["section_header_style"]
        if "name_alignment" in resume_data:
            raw_tailored_content["name_alignment"] = resume_data["name_alignment"]

        # Enforce original section order from style fingerprint
        section_order = (effective_style or {}).get("structure", [])
        tailored_content = (
            self._enforce_structure_order(raw_tailored_content, section_order)
            if section_order
            else raw_tailored_content
        )

        # Build known-skills list for hallucination checks
        known_skills = list(all_keywords) + (
            resume_data.get("skills", []) if isinstance(resume_data.get("skills"), list) else []
        )

        validation_report = self._validator.validate_full_resume(
            resume_data, tailored_content, known_skills
        )

        metrics = self._calculate_metrics(resume_data, tailored_content, all_keywords)

        api_calls = self._rewriter.api_call_count

        logger.info(
            "Tailoring complete. API calls used: %d. "
            "Keyword coverage: %.1f%% -> %.1f%%.",
            api_calls,
            metrics["keyword_coverage_before"],
            metrics["keyword_coverage_after"],
        )

        return ModificationResult(
            content=tailored_content,
            metrics=metrics,
            modification_log=self.modification_log,
            validation_report=validation_report,
            api_calls_used=api_calls,
        )

    # ------------------------------------------------------------------
    # Section tailors
    # ------------------------------------------------------------------

    def tailor_professional_summary(
        self,
        summary: str,
        job: Job,
        job_keywords: List[str],
        resume_data: Optional[dict] = None,
        style_fingerprint: Optional[dict] = None,
    ) -> str:
        """Rewrite the professional summary targeting *job*.

        Args:
            summary: Current summary from the master resume.
            job: Target job.
            job_keywords: Combined required + preferred keywords.
            resume_data: Full resume dict (used to infer years of experience).
            style_fingerprint: Optional style fingerprint dict.

        Returns:
            AI-rewritten summary, or original on failure.
        """
        if not summary:
            return summary

        years_exp = self._infer_years_experience(resume_data or {})
        rewritten = self._rewriter.generate_professional_summary(
            original_summary=summary,
            job_title=job.job_title,
            keywords=job_keywords,
            years_experience=years_exp,
            job_description=job.job_description or "",
            style_fingerprint=style_fingerprint,
        )

        vr = self._validator.validate_summary(
            summary, rewritten,
            known_skills=job_keywords + (resume_data or {}).get("skills", []),
        )

        accepted = vr.is_valid or not vr.warnings
        final = rewritten if rewritten != summary else summary

        self.modification_log.append(ModificationEntry(
            section="professional_summary",
            field="summary",
            original=summary,
            modified=final,
            accepted=accepted,
            warnings=vr.warnings,
        ))

        if not accepted:
            logger.warning(
                "Summary validation warnings: %s – keeping modification anyway.",
                "; ".join(vr.warnings),
            )

        return final

    def tailor_work_experience(
        self,
        experience_list: List[dict],
        job: Job,
        job_keywords: List[str],
        style_fingerprint: Optional[dict] = None,
    ) -> List[dict]:
        """Rewrite the most relevant bullets in each work experience entry.

        Args:
            experience_list: List of position dicts, each with a ``"bullets"``
                key containing a list of achievement strings.
            job: Target job.
            job_keywords: Combined keyword list.
            style_fingerprint: Optional style fingerprint dict.

        Returns:
            List of position dicts with top bullets rewritten.
        """
        if not experience_list:
            return experience_list

        job_context = f"{job.job_title} at {job.company_name}"
        tailored: List[dict] = []

        for position in experience_list:
            bullets: List[str] = position.get("bullets", [])
            pos_title: str = position.get("title", "Unknown Position")

            if not bullets:
                tailored.append(position)
                continue

            rewritten_bullets = self._rewriter.batch_rewrite_bullets(
                bullets=bullets,
                job_keywords=job_keywords,
                job_context=job_context,
                job_description=job.job_description or "",
                max_rewrites=_MAX_BULLETS_PER_POSITION,
                style_fingerprint=style_fingerprint,
            )

            # Validate and log each changed bullet
            final_bullets: List[str] = []
            for orig, mod in zip(bullets, rewritten_bullets):
                if orig == mod:
                    final_bullets.append(orig)
                    continue

                vr = self._validator.validate_bullet(orig, mod, job_keywords)
                accepted = True
                chosen = mod

                if not vr.is_valid:
                    logger.warning(
                        "Bullet validation failed for '%s': %s",
                        pos_title, "; ".join(vr.warnings),
                    )
                    # Reject if metrics were removed (hard rule)
                    if any("metrics" in w.lower() for w in vr.warnings):
                        chosen = orig
                        accepted = False

                final_bullets.append(chosen)
                self.modification_log.append(ModificationEntry(
                    section="work_experience",
                    field="bullet",
                    position_title=pos_title,
                    original=orig,
                    modified=chosen,
                    accepted=accepted,
                    warnings=vr.warnings,
                ))

            tailored.append({**position, "bullets": final_bullets})

        return tailored

    def reorder_skills(
        self,
        skills,
        job_keywords: List[str],
    ):
        """Reorder *skills* so job-relevant ones appear first.

        Accepts either a flat list of skill strings OR a categorised dict
        ``{category_name: [skill, ...]}``. Dict inputs are reordered
        *within* each category, preserving the category structure so the
        PDF generator can render them in the original layout.

        No API call is made — this is a deterministic sort.

        Args:
            skills: Skills from the master resume. List or dict.
            job_keywords: Job description keywords.

        Returns:
            Reordered list or dict, same shape as input.
        """
        if isinstance(skills, dict):
            return self._reorder_categorised_skills(skills, job_keywords)
        return self._rewriter.suggest_skills_reorder(skills, job_keywords)

    @staticmethod
    def _reorder_categorised_skills(
        skills: Dict[str, List[str]],
        job_keywords: List[str],
    ) -> Dict[str, List[str]]:
        """Sort each category's items so job-matching skills come first."""
        kw_lower = {kw.lower() for kw in job_keywords}

        def _rank(item: str) -> int:
            return 0 if item.lower() in kw_lower else 1

        reordered: Dict[str, List[str]] = {}
        for category, items in skills.items():
            if not isinstance(items, list):
                reordered[category] = items
                continue
            reordered[category] = sorted(items, key=_rank)
        return reordered

    def select_relevant_projects(
        self,
        projects: List[dict],
        job_keywords: List[str],
        max_projects: int = _MAX_PROJECTS,
    ) -> List[dict]:
        """Select and rank the most relevant projects for the target job.

        Each project dict is expected to contain a ``"description"`` or
        ``"technologies"`` key.  Projects are scored by keyword overlap and
        the top *max_projects* are returned.

        Args:
            projects: List of project dicts from the master resume.
            job_keywords: Job description keywords.
            max_projects: Maximum number of projects to include.

        Returns:
            Top-ranked project dicts (at most *max_projects*).
        """
        if not projects:
            return projects

        kw_lower = {kw.lower() for kw in job_keywords}

        def _score(project: dict) -> int:
            text = " ".join([
                project.get("name", ""),
                project.get("description", ""),
                " ".join(project.get("technologies", [])),
            ]).lower()
            return sum(1 for kw in kw_lower if kw in text)

        ranked = sorted(projects, key=_score, reverse=True)
        selected = ranked[:max_projects]

        logger.debug(
            "Selected %d/%d projects (max %d).", len(selected), len(projects), max_projects
        )
        return selected

    def _enforce_structure_order(
        self,
        tailored_content: dict,
        structure_order: List[str],
    ) -> dict:
        """Return *tailored_content* with keys ordered to match *structure_order*.

        Keys listed in *structure_order* come first (in that order); any
        remaining keys not present in *structure_order* are appended at the end
        so no data is lost.

        Args:
            tailored_content: The tailored resume content dict.
            structure_order: Ordered list of section keys from the style
                fingerprint (e.g. ``["personal_info", "professional_summary",
                "work_experience", "skills", "education"]``).

        Returns:
            New dict with keys in the requested order.
        """
        ordered: dict = {}
        for key in structure_order:
            if key in tailored_content:
                ordered[key] = tailored_content[key]
        for key, val in tailored_content.items():
            if key not in ordered:
                ordered[key] = val
        return ordered

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _progress(self, step: str, current: int, total: int) -> None:
        if self._on_progress:
            try:
                self._on_progress(step, current, total)
            except Exception:
                pass

    @staticmethod
    def _promote_keywords_to_skills(
        base_skills,
        job_keywords: List[str],
        tailored_summary: str,
        tailored_experience: List[dict],
    ):
        """Add job keywords to the skills collection when they appear in tailored content.

        After NIM rewrites bullets and the summary, some preferred-skill
        keywords may have been woven into the text. This method promotes those
        keywords so the scoring engine can count them.

        Only keywords that actually appear (case-insensitively) in the rewritten
        content are added, preserving the no-fabrication guarantee.

        Shape-preserving:
          - If *base_skills* is a list, returns a list with promoted keywords appended.
          - If *base_skills* is a dict (categorised), returns a dict with promoted
            keywords appended to an existing "Frameworks and Libraries" / "Tools" /
            "Additional Skills" category (created if none exists).

        Args:
            base_skills: Existing skills from the master resume (list or dict).
            job_keywords: All required + preferred keywords for the job.
            tailored_summary: Rewritten professional summary text.
            tailored_experience: List of tailored work-experience dicts.

        Returns:
            Extended skills collection with promoted keywords appended (deduped).
        """
        # Build full text corpus from rewritten content
        corpus_parts = [tailored_summary]
        for pos in tailored_experience:
            for bullet in pos.get("bullets", []):
                corpus_parts.append(bullet)
        corpus = " ".join(corpus_parts).lower()

        # Collect existing skills (flattened for dedup lookup)
        if isinstance(base_skills, dict):
            existing_lower = set()
            for items in base_skills.values():
                if isinstance(items, list):
                    existing_lower.update(s.lower() for s in items if isinstance(s, str))
        elif isinstance(base_skills, list):
            existing_lower = {s.lower() for s in base_skills if isinstance(s, str)}
        else:
            existing_lower = set()

        promoted: List[str] = []
        for kw in job_keywords:
            kw_lower = kw.lower()
            if kw_lower not in existing_lower and kw_lower in corpus:
                promoted.append(kw)
                existing_lower.add(kw_lower)
                logger.debug("Promoted keyword to skills list: %s", kw)

        if promoted:
            logger.info(
                "Promoted %d keyword(s) to skills list from rewritten content: %s",
                len(promoted), ", ".join(promoted),
            )

        if isinstance(base_skills, dict):
            # Deep-copy the dict so we don't mutate the master resume
            result_dict: Dict[str, List[str]] = {
                k: list(v) if isinstance(v, list) else v
                for k, v in base_skills.items()
            }
            if promoted:
                # Pick an existing category that best fits, else create one
                target_category = None
                for preferred in ("Frameworks and Libraries", "Frameworks", "Tools",
                                  "Technologies", "Libraries"):
                    if preferred in result_dict and isinstance(result_dict[preferred], list):
                        target_category = preferred
                        break
                if target_category is None:
                    target_category = "Additional Skills"
                    result_dict.setdefault(target_category, [])
                result_dict[target_category] = list(result_dict[target_category]) + promoted
            return result_dict

        return list(base_skills or []) + promoted

    @staticmethod
    def _infer_years_experience(resume_data: dict) -> int:
        """Estimate total years of experience from work history.

        Args:
            resume_data: Master resume content dict.

        Returns:
            Approximate years of experience as an integer.
        """
        import re
        exp = resume_data.get("work_experience", [])
        years: List[int] = []
        for pos in exp:
            duration = pos.get("duration", "") or pos.get("dates", "")
            found = re.findall(r"\d{4}", str(duration))
            if len(found) >= 2:
                try:
                    years.append(abs(int(found[-1]) - int(found[0])))
                except ValueError:
                    pass
        return sum(years) if years else 0

    @staticmethod
    def _calculate_metrics(
        original: dict,
        tailored: dict,
        job_keywords: List[str],
    ) -> dict:
        """Calculate keyword coverage before and after tailoring.

        Args:
            original: Master resume content dict.
            tailored: Tailored resume content dict.
            job_keywords: Job description keywords.

        Returns:
            Metrics dict with ``keyword_coverage_before``,
            ``keyword_coverage_after``, and ``keyword_coverage_improvement``.
        """
        if not job_keywords:
            return {
                "keyword_coverage_before": 0.0,
                "keyword_coverage_after": 0.0,
                "keyword_coverage_improvement": 0.0,
                "total_keywords": 0,
            }

        def _text_from_resume(data: dict) -> str:
            parts = [
                data.get("professional_summary", ""),
                " ".join(
                    " ".join(b) if isinstance(b, list) else str(b)
                    for pos in data.get("work_experience", [])
                    for b in [pos.get("bullets", [])]
                ),
                " ".join(str(s) for s in data.get("skills", [])),
            ]
            return " ".join(parts).lower()

        def _coverage(text: str, keywords: List[str]) -> float:
            matched = sum(1 for kw in keywords if kw.lower() in text)
            return matched / len(keywords) * 100.0

        before_text = _text_from_resume(original)
        after_text = _text_from_resume(tailored)

        before = _coverage(before_text, job_keywords)
        after = _coverage(after_text, job_keywords)

        return {
            "keyword_coverage_before": round(before, 1),
            "keyword_coverage_after": round(after, 1),
            "keyword_coverage_improvement": round(after - before, 1),
            "total_keywords": len(job_keywords),
        }
