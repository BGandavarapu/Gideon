"""Skill gap analysis — compares job requirements against resume skills."""

from __future__ import annotations

from typing import Any, Dict, List

from analyzer.skill_matcher import SkillMatcher


class SkillGapAnalyzer:

    def __init__(self) -> None:
        self.matcher = SkillMatcher()

    def analyze(self, job: Any, master_resume: Any) -> dict:
        resume_skills = self._extract_resume_skills(
            master_resume.content if isinstance(master_resume.content, dict) else {}
        )

        required = job.required_skills or []
        preferred = job.preferred_skills or []

        matched_skills: List[str] = []
        missing_required: List[str] = []
        missing_preferred: List[str] = []

        for skill in required:
            if self._skill_matches(skill, resume_skills):
                matched_skills.append(skill)
            else:
                missing_required.append(skill)

        for skill in preferred:
            if self._skill_matches(skill, resume_skills):
                if skill not in matched_skills:
                    matched_skills.append(skill)
            else:
                missing_preferred.append(skill)

        req_total = len(required)
        matched_req = req_total - len(missing_required)
        match_pct = (matched_req / req_total * 100) if req_total > 0 else 100.0

        return {
            "job_id": job.id,
            "job_title": job.job_title,
            "company": job.company_name,
            "matched_skills": matched_skills,
            "missing_required": missing_required,
            "missing_preferred": missing_preferred,
            "match_percentage": round(match_pct, 1),
            "required_total": req_total,
            "preferred_total": len(preferred),
            "matched_required_count": matched_req,
            "matched_preferred_count": len(preferred) - len(missing_preferred),
            "priority_gaps": missing_required[:5],
            "has_gaps": len(missing_required) > 0,
        }

    def _extract_resume_skills(self, content: dict) -> List[str]:
        skills: List[str] = []
        top = content.get("skills", [])
        if isinstance(top, list):
            skills.extend(str(s).lower().strip() for s in top if s)
        elif isinstance(top, dict):
            for items in top.values():
                if isinstance(items, list):
                    skills.extend(str(s).lower().strip() for s in items if s)

        certs = content.get("certifications", [])
        if isinstance(certs, list):
            for c in certs:
                if isinstance(c, str) and c.strip():
                    skills.append(c.lower().strip())
                elif isinstance(c, dict):
                    name = c.get("name") or c.get("title") or ""
                    if name.strip():
                        skills.append(name.lower().strip())

        return skills

    def _skill_matches(self, skill: str, resume_skills: List[str]) -> bool:
        skill_lower = skill.lower().strip()

        if skill_lower in resume_skills:
            return True

        try:
            normalised = self.matcher.normalise(skill).lower()
            if normalised in resume_skills:
                return True
            for rs in resume_skills:
                if self.matcher.normalise(rs).lower() == normalised:
                    return True
        except Exception:
            pass

        if len(skill_lower) >= 4:
            for rs in resume_skills:
                if skill_lower in rs or rs in skill_lower:
                    return True

        return False

    def format_for_chat(self, gap: dict) -> str:
        title = gap["job_title"]
        company = gap["company"]
        pct = gap["match_percentage"]
        matched = gap["matched_skills"]
        missing_req = gap["missing_required"]
        missing_pref = gap["missing_preferred"]
        priority = gap["priority_gaps"]

        lines = [f"**Skill Gap Analysis — {title} at {company}**\n"]
        lines.append(f"You match **{pct}%** of the required skills.\n")

        if matched:
            tags = ", ".join(matched[:8])
            suffix = "..." if len(matched) > 8 else ""
            lines.append(f"**Skills you have ({len(matched)}):** {tags}{suffix}")

        if missing_req:
            lines.append(f"\n**Missing required skills ({len(missing_req)}):** {', '.join(missing_req)}")

        if missing_pref:
            lines.append(f"\n**Missing preferred skills ({len(missing_pref)}):** {', '.join(missing_pref)}")

        if priority:
            lines.append(f"\n**Priority gaps to close:** {', '.join(priority)}")
            lines.append("\nWant me to help you learn any of these? I can suggest resources and create a learning plan.")
        else:
            lines.append("\nYou have all the required skills for this role!")

        return "\n".join(lines)
