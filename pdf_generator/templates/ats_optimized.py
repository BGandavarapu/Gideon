"""
ATS-Optimised resume template.

Produces a plain, single-column PDF that is maximally compatible with
Applicant Tracking Systems:

- Standard built-in fonts only (Helvetica / Times).
- No images, no colour fills, no table structures.
- Linear top-to-bottom reading order.
- Unicode bullet replaced with a plain hyphen for widest parser support.
- Thin horizontal rules between sections (safe for most parsers).
- Contact details on a single line separated by ``|``.

Layout order (sections missing from the data are silently skipped):

    Name + contact
    ──────────────────────────────────────────
    PROFESSIONAL SUMMARY
    ──────────────────────────────────────────
    WORK EXPERIENCE
    ──────────────────────────────────────────
    EDUCATION
    ──────────────────────────────────────────
    SKILLS
    ──────────────────────────────────────────
    CERTIFICATIONS
    ──────────────────────────────────────────
    PROJECTS
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
    SECTION_TITLES,
)

logger = logging.getLogger(__name__)

# ATS parsers often choke on the Unicode bullet; use a plain hyphen.
_BULLET = "-"


class ATSOptimizedTemplate(BasePDFTemplate):
    """Single-column, monochrome, ATS-friendly resume template."""

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def generate_pdf(self, resume_data: Dict, output_path: str) -> None:
        """Render *resume_data* to a PDF at *output_path*.

        Args:
            resume_data: Structured resume dictionary.
            output_path: Filesystem path for the output ``.pdf`` file.
        """
        c = rl_canvas.Canvas(output_path, pagesize=(self.page_width, self.page_height))
        y = self.page_height - self.margin

        y = self._draw_header(c, resume_data.get("personal_info", {}), y)

        for section_key in SECTION_ORDER:
            if section_key not in resume_data or not resume_data[section_key]:
                continue

            y = self.check_page_break(c, y, needed=40, bottom=PAGE_BOTTOM_MARGIN)
            y -= SECTION_GAP  # breathing room before each section

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
    # Section renderers
    # ------------------------------------------------------------------

    def _draw_header(self, c: rl_canvas.Canvas, info: Dict, y: float) -> float:
        """Render name + contact line."""
        pal = ATSPalette

        # Name
        name = info.get("name", "").strip()
        c.setFont("Helvetica-Bold", FontSizes.name)
        c.setFillColor(pal.name_text)
        c.drawString(self.margin, y, name)
        y -= FontSizes.name + 4

        # Contact line: email | phone | location | linkedin
        contact_parts = []
        for field in ("email", "phone", "location", "linkedin"):
            val = info.get(field, "").strip()
            if val:
                contact_parts.append(val)
        contact_line = "  |  ".join(contact_parts)

        c.setFont("Helvetica", FontSizes.contact)
        c.setFillColor(pal.contact_text)
        c.drawString(self.margin, y, contact_line)
        y -= FontSizes.contact + 10

        # Thin rule under header
        c.setStrokeColor(pal.rule_line)
        c.setLineWidth(0.5)
        c.line(self.margin, y, self.page_width - self.margin, y)
        y -= 8

        return y

    def _draw_summary(self, c: rl_canvas.Canvas, summary: str, y: float) -> float:
        y = self.draw_section_rule(
            c, y, SECTION_TITLES["professional_summary"],
            text_color=ATSPalette.header_text, rule_color=ATSPalette.rule_line,
        )
        lines = self.wrap_text(summary, self.content_width, "Helvetica", FontSizes.body)
        c.setFont("Helvetica", FontSizes.body)
        c.setFillColor(ATSPalette.body_text)
        for line in lines:
            y = self.check_page_break(c, y)
            c.drawString(self.margin, y, line)
            y -= LINE_SPACING
        return y

    def _draw_experience(self, c: rl_canvas.Canvas, experience: List[Dict], y: float) -> float:
        y = self.draw_section_rule(
            c, y, SECTION_TITLES["work_experience"],
            text_color=ATSPalette.header_text, rule_color=ATSPalette.rule_line,
        )

        for job in experience:
            title   = job.get("title", "").strip()
            company = job.get("company", "").strip()
            loc     = job.get("location", "").strip()
            dates   = job.get("dates", "").strip()

            # Ensure room for at least title + company + 1 bullet
            y = self.check_page_break(c, y, needed=50)

            # Job title (bold)
            c.setFont("Helvetica-Bold", FontSizes.job_title)
            c.setFillColor(ATSPalette.body_text)
            c.drawString(self.margin, y, title)

            # Dates right-aligned on the same line
            if dates:
                self.draw_right_aligned(c, y, dates, "Helvetica", FontSizes.date_right)

            y -= LINE_SPACING

            # Company | location
            company_line_parts = [p for p in (company, loc) if p]
            company_line = "  |  ".join(company_line_parts)
            c.setFont("Helvetica", FontSizes.company)
            c.setFillColor(ATSPalette.body_text)
            c.drawString(self.margin, y, company_line)
            y -= LINE_SPACING + 2

            # Bullets
            for bullet in job.get("bullets", []):
                if not bullet.strip():
                    continue
                y = self.draw_bullet_line(
                    c, y, bullet.strip(),
                    font_name="Helvetica", font_size=FontSizes.bullet,
                    bullet_char=_BULLET,
                )

            y -= 6   # gap between positions

        return y

    def _draw_education(self, c: rl_canvas.Canvas, education: List, y: float) -> float:
        y = self.draw_section_rule(
            c, y, SECTION_TITLES["education"],
            text_color=ATSPalette.header_text, rule_color=ATSPalette.rule_line,
        )

        for edu in education:
            if isinstance(edu, str):
                # Plain string entry
                y = self.check_page_break(c, y)
                c.setFont("Helvetica", FontSizes.body)
                c.setFillColor(ATSPalette.body_text)
                c.drawString(self.margin, y, edu)
                y -= LINE_SPACING
                continue

            degree  = edu.get("degree", "").strip()
            school  = edu.get("school", edu.get("institution", "")).strip()
            year    = str(edu.get("year", edu.get("graduation_year", ""))).strip()
            gpa     = edu.get("gpa", "")

            y = self.check_page_break(c, y, needed=35)

            c.setFont("Helvetica-Bold", FontSizes.job_title)
            c.setFillColor(ATSPalette.body_text)
            c.drawString(self.margin, y, degree)
            if year:
                self.draw_right_aligned(c, y, year, "Helvetica", FontSizes.date_right)
            y -= LINE_SPACING

            school_line = school
            if gpa:
                school_line += f"  |  GPA: {gpa}"
            c.setFont("Helvetica", FontSizes.company)
            c.drawString(self.margin, y, school_line)
            y -= LINE_SPACING + 4

        return y

    def _draw_skills(self, c: rl_canvas.Canvas, skills, y: float) -> float:
        y = self.draw_section_rule(
            c, y, SECTION_TITLES["skills"],
            text_color=ATSPalette.header_text, rule_color=ATSPalette.rule_line,
        )

        # Normalise: skills can be a list of strings or a dict of categories
        skill_lines = self._format_skills(skills)

        c.setFont("Helvetica", FontSizes.skills)
        c.setFillColor(ATSPalette.body_text)
        for line in skill_lines:
            y = self.check_page_break(c, y)
            wrapped = self.wrap_text(line, self.content_width, "Helvetica", FontSizes.skills)
            for wl in wrapped:
                c.drawString(self.margin, y, wl)
                y -= LINE_SPACING

        return y

    def _draw_certifications(self, c: rl_canvas.Canvas, certs: List, y: float) -> float:
        y = self.draw_section_rule(
            c, y, SECTION_TITLES["certifications"],
            text_color=ATSPalette.header_text, rule_color=ATSPalette.rule_line,
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
            c.setFillColor(ATSPalette.body_text)
            c.drawString(self.margin, y, text)
            y -= LINE_SPACING

        return y

    def _draw_projects(self, c: rl_canvas.Canvas, projects: List[Dict], y: float) -> float:
        y = self.draw_section_rule(
            c, y, SECTION_TITLES["projects"],
            text_color=ATSPalette.header_text, rule_color=ATSPalette.rule_line,
        )
        for proj in projects:
            name  = proj.get("name", "").strip()
            desc  = proj.get("description", "").strip()
            tech  = proj.get("tech", proj.get("technologies", []))

            y = self.check_page_break(c, y, needed=30)

            c.setFont("Helvetica-Bold", FontSizes.job_title)
            c.setFillColor(ATSPalette.body_text)
            c.drawString(self.margin, y, name)
            y -= LINE_SPACING

            if desc:
                lines = self.wrap_text(desc, self.content_width, "Helvetica", FontSizes.body)
                c.setFont("Helvetica", FontSizes.body)
                for line in lines:
                    y = self.check_page_break(c, y)
                    c.drawString(self.margin, y, line)
                    y -= LINE_SPACING

            if tech:
                tech_str = "Technologies: " + ", ".join(tech if isinstance(tech, list) else [str(tech)])
                lines = self.wrap_text(tech_str, self.content_width, "Helvetica", FontSizes.body)
                c.setFont("Helvetica", FontSizes.body)
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
        """Normalise skills (list or dict) into display lines."""
        if isinstance(skills, list):
            # Flat list: join onto wrapped lines (comma-separated)
            return [", ".join(str(s) for s in skills if s)]

        if isinstance(skills, dict):
            # Categorised: one line per category
            lines = []
            for category, items in skills.items():
                if isinstance(items, list):
                    label = category.replace("_", " ").title()
                    lines.append(f"{label}: {', '.join(str(i) for i in items if i)}")
            return lines

        return [str(skills)]
