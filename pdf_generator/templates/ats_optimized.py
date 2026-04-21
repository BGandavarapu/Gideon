"""
ATS-Optimised resume template.

Produces a plain, single-column PDF that is maximally compatible with
Applicant Tracking Systems. Layout is linear, monochrome, and driven by
the style fingerprint so that the tailored PDF matches the original
uploaded resume's font, bullet character, and section-header style.
"""

import logging
from typing import Dict, List

from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas as rl_canvas

from pdf_generator.base_template import BasePDFTemplate
from pdf_generator.styles import (
    ATSPalette,
    FontSizes,
    LINE_SPACING,
    MARGIN,
    PAGE_BOTTOM_MARGIN,
    SECTION_GAP,
    SECTION_ORDER,
    SECTION_TITLES_TITLE,
    SECTION_TITLES_TITLE_COLON,
    SECTION_TITLES_UPPER,
)

logger = logging.getLogger(__name__)


class ATSOptimizedTemplate(BasePDFTemplate):
    """Single-column, monochrome, ATS-friendly resume template."""

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def generate_pdf(self, resume_data: Dict, output_path: str) -> None:
        """Render *resume_data* to a PDF at *output_path*."""
        c = rl_canvas.Canvas(output_path, pagesize=(self.page_width, self.page_height))
        y = self.page_height - self.margin

        self._title_map = self._select_title_map()

        y = self._draw_header(c, resume_data.get("personal_info", {}), y)

        seen = set()
        effective_order = []
        for key in resume_data:
            if key in SECTION_ORDER and key not in seen:
                effective_order.append(key)
                seen.add(key)
        for key in SECTION_ORDER:
            if key not in seen:
                effective_order.append(key)
                seen.add(key)

        for section_key in effective_order:
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
        logger.info("ATS-optimised PDF saved: %s", output_path)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _select_title_map(self) -> Dict[str, str]:
        case = self._style.get("section_header_case", "title_colon")
        if case == "upper":
            return SECTION_TITLES_UPPER
        if case == "title":
            return SECTION_TITLES_TITLE
        return SECTION_TITLES_TITLE_COLON

    def _section_header(self, c: rl_canvas.Canvas, y: float, key: str) -> float:
        return self.draw_section_rule(
            c, y, self._title_map[key],
            font_name=self._style["bold_font"],
            text_color=ATSPalette.header_text, rule_color=ATSPalette.rule_line,
        )

    # ------------------------------------------------------------------
    # Section renderers
    # ------------------------------------------------------------------

    def _draw_header(self, c: rl_canvas.Canvas, info: Dict, y: float) -> float:
        pal = ATSPalette
        bold = self._style["bold_font"]
        body = self._style["body_font"]
        centered = self._style.get("name_alignment") == "center"

        name = info.get("name", "").strip()
        c.setFont(bold, FontSizes.name)
        c.setFillColor(pal.name_text)
        if centered:
            from reportlab.pdfbase.pdfmetrics import stringWidth
            name_w = stringWidth(name, bold, FontSizes.name)
            c.drawString((self.page_width - name_w) / 2, y, name)
        else:
            c.drawString(self.margin, y, name)
        y -= FontSizes.name + 4

        contact_parts = []
        for field in ("email", "phone", "location", "linkedin", "github"):
            val = info.get(field, "").strip()
            if val:
                contact_parts.append(val)
        contact_line = "  |  ".join(contact_parts)

        c.setFont(body, FontSizes.contact)
        c.setFillColor(pal.contact_text)
        if centered:
            from reportlab.pdfbase.pdfmetrics import stringWidth
            cw = stringWidth(contact_line, body, FontSizes.contact)
            c.drawString((self.page_width - cw) / 2, y, contact_line)
        else:
            c.drawString(self.margin, y, contact_line)
        y -= FontSizes.contact + 10

        c.setStrokeColor(pal.rule_line)
        c.setLineWidth(0.5)
        c.line(self.margin, y, self.page_width - self.margin, y)
        y -= 8

        return y

    def _draw_summary(self, c: rl_canvas.Canvas, summary: str, y: float) -> float:
        body = self._style["body_font"]
        y = self._section_header(c, y, "professional_summary")
        lines = self.wrap_text(summary, self.content_width, body, FontSizes.body)
        c.setFont(body, FontSizes.body)
        c.setFillColor(ATSPalette.body_text)
        for line in lines:
            y = self.check_page_break(c, y)
            c.drawString(self.margin, y, line)
            y -= LINE_SPACING
        return y

    def _draw_experience(self, c: rl_canvas.Canvas, experience: List[Dict], y: float) -> float:
        bold = self._style["bold_font"]
        body = self._style["body_font"]
        bullet_char = self._style["bullet_char"]

        y = self._section_header(c, y, "work_experience")

        for job in experience:
            title   = job.get("title", "").strip()
            company = job.get("company", "").strip()
            loc     = job.get("location", "").strip()
            dates   = job.get("dates", "").strip()

            y = self.check_page_break(c, y, needed=50)

            c.setFont(bold, FontSizes.job_title)
            c.setFillColor(ATSPalette.body_text)
            c.drawString(self.margin, y, title)

            if dates:
                self.draw_right_aligned(c, y, dates, body, FontSizes.date_right)

            y -= LINE_SPACING

            company_line_parts = [p for p in (company, loc) if p]
            company_line = "  |  ".join(company_line_parts)
            c.setFont(body, FontSizes.company)
            c.setFillColor(ATSPalette.body_text)
            c.drawString(self.margin, y, company_line)
            y -= LINE_SPACING + 2

            for bullet in job.get("bullets", []):
                if not bullet.strip():
                    continue
                y = self.draw_bullet_line(
                    c, y, bullet.strip(),
                    font_name=body, font_size=FontSizes.bullet,
                    bullet_char=bullet_char,
                )

            y -= 6

        return y

    def _draw_education(self, c: rl_canvas.Canvas, education: List, y: float) -> float:
        bold = self._style["bold_font"]
        body = self._style["body_font"]

        y = self._section_header(c, y, "education")

        for edu in education:
            if isinstance(edu, str):
                y = self.check_page_break(c, y)
                c.setFont(body, FontSizes.body)
                c.setFillColor(ATSPalette.body_text)
                c.drawString(self.margin, y, edu)
                y -= LINE_SPACING
                continue

            degree  = edu.get("degree", "").strip()
            school  = edu.get("school", edu.get("institution", "")).strip()
            year    = str(edu.get("year", edu.get("graduation_year", ""))).strip()
            gpa     = edu.get("gpa", "")

            y = self.check_page_break(c, y, needed=35)

            c.setFont(bold, FontSizes.job_title)
            c.setFillColor(ATSPalette.body_text)
            c.drawString(self.margin, y, degree)
            if year:
                self.draw_right_aligned(c, y, year, body, FontSizes.date_right)
            y -= LINE_SPACING

            school_line = school
            loc = edu.get("location", "").strip()
            if loc:
                school_line += f", {loc}"
            if gpa:
                school_line += f"  |  GPA: {gpa}"
            c.setFont(body, FontSizes.company)
            c.drawString(self.margin, y, school_line)
            y -= LINE_SPACING + 2

            coursework = edu.get("coursework", "").strip()
            if coursework:
                y = self.check_page_break(c, y, needed=20)
                cw_label = "Relevant Coursework: "
                c.setFont(bold, FontSizes.body)
                label_w = c.stringWidth(cw_label, bold, FontSizes.body)
                c.drawString(self.margin, y, cw_label)
                c.setFont(body, FontSizes.body)
                c.drawString(self.margin + label_w, y, coursework)
                y -= LINE_SPACING + 2

        return y

    def _draw_skills(self, c: rl_canvas.Canvas, skills, y: float) -> float:
        bold = self._style["bold_font"]
        body = self._style["body_font"]

        y = self._section_header(c, y, "skills")

        if isinstance(skills, dict):
            for category, items in skills.items():
                if not isinstance(items, list) or not items:
                    continue
                label = f"{category}: "
                value = ", ".join(str(i) for i in items if i)

                label_w = c.stringWidth(label, bold, FontSizes.skills)
                remaining_w = self.content_width - label_w
                wrapped = self.wrap_text(value, remaining_w, body, FontSizes.skills)

                y = self.check_page_break(c, y)
                c.setFont(bold, FontSizes.skills)
                c.setFillColor(ATSPalette.body_text)
                c.drawString(self.margin, y, label)
                c.setFont(body, FontSizes.skills)
                if wrapped:
                    c.drawString(self.margin + label_w, y, wrapped[0])
                y -= LINE_SPACING
                for extra in wrapped[1:]:
                    y = self.check_page_break(c, y)
                    c.drawString(self.margin + label_w, y, extra)
                    y -= LINE_SPACING
            return y

        skill_lines = self._format_skills(skills)
        c.setFont(body, FontSizes.skills)
        c.setFillColor(ATSPalette.body_text)
        for line in skill_lines:
            y = self.check_page_break(c, y)
            wrapped = self.wrap_text(line, self.content_width, body, FontSizes.skills)
            for wl in wrapped:
                c.drawString(self.margin, y, wl)
                y -= LINE_SPACING

        return y

    def _draw_certifications(self, c: rl_canvas.Canvas, certs: List, y: float) -> float:
        body = self._style["body_font"]
        y = self._section_header(c, y, "certifications")
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
            c.setFont(body, FontSizes.body)
            c.setFillColor(ATSPalette.body_text)
            c.drawString(self.margin, y, text)
            y -= LINE_SPACING

        return y

    def _draw_projects(self, c: rl_canvas.Canvas, projects: List[Dict], y: float) -> float:
        bold = self._style["bold_font"]
        body = self._style["body_font"]
        bullet_char = self._style["bullet_char"]

        y = self._section_header(c, y, "projects")
        for proj in projects:
            name    = proj.get("name", "").strip()
            date    = proj.get("date", "").strip()
            desc    = proj.get("description", "").strip()
            bullets = proj.get("bullets", [])
            tech    = proj.get("tech", proj.get("technologies", []))

            y = self.check_page_break(c, y, needed=30)

            c.setFont(bold, FontSizes.job_title)
            c.setFillColor(ATSPalette.body_text)
            c.drawString(self.margin, y, name)
            if date:
                self.draw_right_aligned(c, y, date, body, FontSizes.date_right)
            y -= LINE_SPACING

            if bullets:
                bullet_width = self.content_width - 15
                for bullet in bullets:
                    lines = self.wrap_text(bullet, bullet_width, body, FontSizes.body)
                    for i, line in enumerate(lines):
                        y = self.check_page_break(c, y)
                        c.setFont(body, FontSizes.body)
                        if i == 0:
                            c.drawString(self.margin + 5, y, f"{bullet_char}  {line}")
                        else:
                            c.drawString(self.margin + 15, y, line)
                        y -= LINE_SPACING
            elif desc:
                lines = self.wrap_text(desc, self.content_width, body, FontSizes.body)
                c.setFont(body, FontSizes.body)
                for line in lines:
                    y = self.check_page_break(c, y)
                    c.drawString(self.margin, y, line)
                    y -= LINE_SPACING

            if tech:
                tech_str = "Technologies: " + ", ".join(tech if isinstance(tech, list) else [str(tech)])
                lines = self.wrap_text(tech_str, self.content_width, body, FontSizes.body)
                c.setFont(body, FontSizes.body)
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
                    lines.append(f"{category}: {', '.join(str(i) for i in items if i)}")
            return lines

        return [str(skills)]
