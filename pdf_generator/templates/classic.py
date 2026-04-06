"""
Classic resume template.

A polished single-column layout that uses a deep-blue accent colour for
section headers and a thin coloured rule to visually separate sections.
Still fully parseable by ATS systems - colours and rules do not impede
text extraction.

Visual hierarchy:
  - Candidate name: 17pt bold, dark navy
  - Section headers: 11pt bold, dark navy with blue underline rule
  - Job titles / degrees: 11pt bold
  - Company / school: 10pt regular, right-aligned dates
  - Body / bullets: 10pt regular
  - Contact line: 10pt, centred, grey

Layout order:
    Name (centred)
    Contact line (centred)
    ── PROFESSIONAL SUMMARY ────────────────
    ── WORK EXPERIENCE ──────────────────────
    ── EDUCATION ────────────────────────────
    ── SKILLS ───────────────────────────────
    ── CERTIFICATIONS ───────────────────────
    ── PROJECTS ─────────────────────────────
"""

import logging
from typing import Dict, List

from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas as rl_canvas

from pdf_generator.base_template import BasePDFTemplate
from pdf_generator.styles import (
    ClassicPalette,
    FontSizes,
    LINE_SPACING,
    PAGE_BOTTOM_MARGIN,
    SECTION_GAP,
    SECTION_ORDER,
    SECTION_TITLES,
)

logger = logging.getLogger(__name__)

_BULLET = "\u2022"   # proper bullet for classic template


class ClassicTemplate(BasePDFTemplate):
    """Traditional single-column resume with blue accent colours."""

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def generate_pdf(self, resume_data: Dict, output_path: str) -> None:
        """Render *resume_data* to a PDF at *output_path*."""
        c = rl_canvas.Canvas(output_path, pagesize=(self.page_width, self.page_height))
        y = self.page_height - self.margin

        y = self._draw_header(c, resume_data.get("personal_info", {}), y)

        for section_key in SECTION_ORDER:
            if section_key not in resume_data or not resume_data[section_key]:
                continue

            y = self.check_page_break(c, y, needed=40, bottom=PAGE_BOTTOM_MARGIN)
            y -= SECTION_GAP

            if section_key == "professional_summary":
                y = self._draw_summary(c, resume_data[section_key], y)
            elif section_key == "work_experience":
                y = self._draw_experience(c, resume_data[section_key], y)
            elif section_key == "education":
                y = self._draw_education(c, resume_data[section_key], y)
            elif section_key == "skills":
                y = self._draw_skills(c, resume_data[section_key], y)
            elif section_key == "certifications":
                y = self._draw_certifications(c, resume_data[section_key], y)
            elif section_key == "projects":
                y = self._draw_projects(c, resume_data[section_key], y)

        c.save()
        logger.info("Classic PDF saved: %s", output_path)

    # ------------------------------------------------------------------
    # Section renderers
    # ------------------------------------------------------------------

    def _draw_header(self, c: rl_canvas.Canvas, info: Dict, y: float) -> float:
        """Centred name + coloured contact line."""
        pal = ClassicPalette

        # Name (bold, centred)
        name = info.get("name", "").strip()
        c.setFont("Helvetica-Bold", FontSizes.name)
        c.setFillColor(pal.name_text)
        name_w = stringWidth(name, "Helvetica-Bold", FontSizes.name)
        c.drawString((self.page_width - name_w) / 2, y, name)
        y -= FontSizes.name + 5

        # Contact line (centred, slightly grey)
        contact_parts = []
        for field in ("email", "phone", "location", "linkedin"):
            val = info.get(field, "").strip()
            if val:
                contact_parts.append(val)
        contact_line = "   |   ".join(contact_parts)

        c.setFont("Helvetica", FontSizes.contact)
        c.setFillColor(pal.contact_text)
        cw = stringWidth(contact_line, "Helvetica", FontSizes.contact)
        c.drawString((self.page_width - cw) / 2, y, contact_line)
        y -= FontSizes.contact + 8

        # Thick accent rule
        c.setStrokeColor(pal.accent)
        c.setLineWidth(1.5)
        c.line(self.margin, y, self.page_width - self.margin, y)
        y -= 10

        return y

    def _draw_summary(self, c: rl_canvas.Canvas, summary: str, y: float) -> float:
        pal = ClassicPalette
        y = self.draw_section_rule(
            c, y, SECTION_TITLES["professional_summary"],
            text_color=pal.header_text, rule_color=pal.rule_line,
        )
        lines = self.wrap_text(summary, self.content_width, "Helvetica", FontSizes.body)
        c.setFont("Helvetica", FontSizes.body)
        c.setFillColor(pal.body_text)
        for line in lines:
            y = self.check_page_break(c, y)
            c.drawString(self.margin, y, line)
            y -= LINE_SPACING
        return y

    def _draw_experience(self, c: rl_canvas.Canvas, experience: List[Dict], y: float) -> float:
        pal = ClassicPalette
        y = self.draw_section_rule(
            c, y, SECTION_TITLES["work_experience"],
            text_color=pal.header_text, rule_color=pal.rule_line,
        )

        for job in experience:
            title   = job.get("title", "").strip()
            company = job.get("company", "").strip()
            loc     = job.get("location", "").strip()
            dates   = job.get("dates", "").strip()

            y = self.check_page_break(c, y, needed=50)

            # Title bold, accent colour
            c.setFont("Helvetica-Bold", FontSizes.job_title)
            c.setFillColor(pal.accent)
            c.drawString(self.margin, y, title)

            # Dates right-aligned, body colour
            if dates:
                c.setFillColor(pal.body_text)
                self.draw_right_aligned(c, y, dates, "Helvetica-Oblique", FontSizes.date_right)

            y -= LINE_SPACING

            # Company | location
            company_parts = [p for p in (company, loc) if p]
            c.setFont("Helvetica-Oblique", FontSizes.company)
            c.setFillColor(pal.body_text)
            c.drawString(self.margin, y, "  |  ".join(company_parts))
            y -= LINE_SPACING + 2

            # Bullets
            c.setFillColor(pal.body_text)
            for bullet in job.get("bullets", []):
                if not bullet.strip():
                    continue
                y = self.draw_bullet_line(
                    c, y, bullet.strip(),
                    font_name="Helvetica", font_size=FontSizes.bullet,
                    bullet_char=_BULLET,
                )

            y -= 8

        return y

    def _draw_education(self, c: rl_canvas.Canvas, education: List, y: float) -> float:
        pal = ClassicPalette
        y = self.draw_section_rule(
            c, y, SECTION_TITLES["education"],
            text_color=pal.header_text, rule_color=pal.rule_line,
        )

        for edu in education:
            if isinstance(edu, str):
                y = self.check_page_break(c, y)
                c.setFont("Helvetica", FontSizes.body)
                c.setFillColor(pal.body_text)
                c.drawString(self.margin, y, edu)
                y -= LINE_SPACING
                continue

            degree  = edu.get("degree", "").strip()
            school  = edu.get("school", edu.get("institution", "")).strip()
            year    = str(edu.get("year", edu.get("graduation_year", ""))).strip()
            gpa     = edu.get("gpa", "")

            y = self.check_page_break(c, y, needed=35)

            c.setFont("Helvetica-Bold", FontSizes.job_title)
            c.setFillColor(pal.accent)
            c.drawString(self.margin, y, degree)
            if year:
                c.setFillColor(pal.body_text)
                self.draw_right_aligned(c, y, year, "Helvetica", FontSizes.date_right)
            y -= LINE_SPACING

            school_line = school
            if gpa:
                school_line += f"  |  GPA: {gpa}"
            c.setFont("Helvetica-Oblique", FontSizes.company)
            c.setFillColor(pal.body_text)
            c.drawString(self.margin, y, school_line)
            y -= LINE_SPACING + 4

        return y

    def _draw_skills(self, c: rl_canvas.Canvas, skills, y: float) -> float:
        pal = ClassicPalette
        y = self.draw_section_rule(
            c, y, SECTION_TITLES["skills"],
            text_color=pal.header_text, rule_color=pal.rule_line,
        )

        skill_lines = self._format_skills(skills)
        c.setFont("Helvetica", FontSizes.skills)
        c.setFillColor(pal.body_text)
        for line in skill_lines:
            wrapped = self.wrap_text(line, self.content_width, "Helvetica", FontSizes.skills)
            for wl in wrapped:
                y = self.check_page_break(c, y)
                c.drawString(self.margin, y, wl)
                y -= LINE_SPACING

        return y

    def _draw_certifications(self, c: rl_canvas.Canvas, certs: List, y: float) -> float:
        pal = ClassicPalette
        y = self.draw_section_rule(
            c, y, SECTION_TITLES["certifications"],
            text_color=pal.header_text, rule_color=pal.rule_line,
        )
        for cert in certs:
            if isinstance(cert, str):
                text = cert
            else:
                name   = cert.get("name", cert.get("title", "")).strip()
                issuer = cert.get("issuer", "").strip()
                year   = str(cert.get("year", "")).strip()
                parts  = [name]
                if issuer:
                    parts.append(issuer)
                if year:
                    parts.append(year)
                text = "  |  ".join(parts)

            y = self.check_page_break(c, y)
            c.setFont("Helvetica", FontSizes.body)
            c.setFillColor(pal.body_text)
            c.drawString(self.margin, y, text)
            y -= LINE_SPACING

        return y

    def _draw_projects(self, c: rl_canvas.Canvas, projects: List[Dict], y: float) -> float:
        pal = ClassicPalette
        y = self.draw_section_rule(
            c, y, SECTION_TITLES["projects"],
            text_color=pal.header_text, rule_color=pal.rule_line,
        )
        for proj in projects:
            name  = proj.get("name", "").strip()
            desc  = proj.get("description", "").strip()
            tech  = proj.get("tech", proj.get("technologies", []))

            y = self.check_page_break(c, y, needed=30)

            c.setFont("Helvetica-Bold", FontSizes.job_title)
            c.setFillColor(pal.accent)
            c.drawString(self.margin, y, name)
            y -= LINE_SPACING

            if desc:
                lines = self.wrap_text(desc, self.content_width, "Helvetica", FontSizes.body)
                c.setFont("Helvetica", FontSizes.body)
                c.setFillColor(pal.body_text)
                for line in lines:
                    y = self.check_page_break(c, y)
                    c.drawString(self.margin, y, line)
                    y -= LINE_SPACING

            if tech:
                tech_list = tech if isinstance(tech, list) else [str(tech)]
                tech_str = "Technologies: " + ", ".join(tech_list)
                lines = self.wrap_text(tech_str, self.content_width, "Helvetica", FontSizes.body)
                c.setFont("Helvetica", FontSizes.body)
                c.setFillColor(pal.body_text)
                for line in lines:
                    y = self.check_page_break(c, y)
                    c.drawString(self.margin, y, line)
                    y -= LINE_SPACING

            y -= 4

        return y

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_skills(skills) -> List[str]:
        if isinstance(skills, list):
            return [", ".join(str(s) for s in skills if s)]
        if isinstance(skills, dict):
            lines = []
            for category, items in skills.items():
                if isinstance(items, list):
                    label = category.replace("_", " ").title()
                    lines.append(f"{label}: {', '.join(str(i) for i in items if i)}")
            return lines
        return [str(skills)]
