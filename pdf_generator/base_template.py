"""
Abstract base class for all PDF resume templates.

Provides shared utilities (text wrapping, section headers, page-overflow
detection, bullet drawing) so that concrete templates only need to
implement their specific layout logic.
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.pdfbase.pdfmetrics import stringWidth

from pdf_generator.styles import MARGIN, LINE_SPACING, PAGE_BOTTOM_MARGIN

import logging

logger = logging.getLogger(__name__)


class BasePDFTemplate(ABC):
    """Abstract base for all resume PDF templates.

    Subclasses must implement :meth:`generate_pdf`.  All geometry is
    expressed in ReportLab *points* (1 pt = 1/72 inch).

    Attributes:
        page_width:  Page width in points (letter = 612).
        page_height: Page height in points (letter = 792).
        margin:      Left/right/top/bottom margin in points.
        content_width: Usable width between left and right margins.
    """

    def __init__(self) -> None:
        self.page_width, self.page_height = letter   # 612 x 792 pts
        self.margin: float = MARGIN
        self.content_width: float = self.page_width - 2 * self.margin

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    def generate_pdf(self, resume_data: Dict, output_path: str) -> None:
        """Generate a complete PDF from *resume_data* and save to *output_path*."""

    # ------------------------------------------------------------------
    # Text utilities
    # ------------------------------------------------------------------

    def wrap_text(
        self,
        text: str,
        max_width: float,
        font_name: str,
        font_size: int,
    ) -> List[str]:
        """Word-wrap *text* so each line fits within *max_width* points.

        Args:
            text:       The text to wrap.
            max_width:  Maximum line width in points.
            font_name:  ReportLab font name (e.g. ``"Helvetica"``).
            font_size:  Font size in points.

        Returns:
            List of strings, each fitting within *max_width*.
        """
        words = text.split()
        lines: List[str] = []
        current_line: List[str] = []

        for word in words:
            candidate = " ".join(current_line + [word])
            if stringWidth(candidate, font_name, font_size) <= max_width:
                current_line.append(word)
            else:
                if current_line:
                    lines.append(" ".join(current_line))
                current_line = [word]

        if current_line:
            lines.append(" ".join(current_line))

        return lines or [""]

    def text_height(self, n_lines: int, line_spacing: float = LINE_SPACING) -> float:
        """Return the total vertical space consumed by *n_lines* of text."""
        return n_lines * line_spacing

    # ------------------------------------------------------------------
    # Canvas helpers
    # ------------------------------------------------------------------

    def check_page_break(
        self,
        c: rl_canvas.Canvas,
        y: float,
        needed: float = 0.0,
        bottom: float = PAGE_BOTTOM_MARGIN,
    ) -> float:
        """Start a new page if *y* is too close to the bottom.

        Args:
            c:       Active canvas.
            y:       Current y position in points.
            needed:  Minimum vertical space required (points).
            bottom:  Absolute lower-bound y before forcing a new page.

        Returns:
            Updated y position (reset to top margin on new page, else
            unchanged).
        """
        if y - needed < bottom:
            c.showPage()
            y = self.page_height - self.margin
        return y

    def draw_section_rule(
        self,
        c: rl_canvas.Canvas,
        y: float,
        title: str,
        font_name: str = "Helvetica-Bold",
        font_size: int = 11,
        text_color=None,
        rule_color=None,
    ) -> float:
        """Draw a section header with a full-width rule underneath.

        Args:
            c:          Active canvas.
            y:          Y position for the header baseline.
            title:      Section title string (already uppercased by caller).
            font_name:  Font for the header text.
            font_size:  Font size in points.
            text_color: ReportLab color for the text (default black).
            rule_color: ReportLab color for the rule (default black).

        Returns:
            Updated y position after the rule.
        """
        from reportlab.lib import colors as _colors

        tc = text_color or _colors.black
        rc = rule_color or _colors.black

        c.setFont(font_name, font_size)
        c.setFillColor(tc)
        c.drawString(self.margin, y, title)

        rule_y = y - 3
        c.setStrokeColor(rc)
        c.setLineWidth(0.75)
        c.line(self.margin, rule_y, self.page_width - self.margin, rule_y)

        return y - font_size - 8   # space below rule

    def draw_bullet_line(
        self,
        c: rl_canvas.Canvas,
        y: float,
        text: str,
        font_name: str = "Helvetica",
        font_size: int = 10,
        indent: Optional[float] = None,
        bullet_char: str = "\u2022",
    ) -> float:
        """Draw a single wrapped bullet line, auto-paginating as needed.

        Args:
            c:           Active canvas.
            y:           Starting y position.
            text:        Bullet text (without the bullet character).
            font_name:   Font for the text.
            font_size:   Font size in points.
            indent:      Left indent for bullet text (defaults to
                         ``self.margin + BULLET_INDENT``).
            bullet_char: Character to use as the bullet marker.

        Returns:
            Updated y position after the last line.
        """
        from pdf_generator.styles import BULLET_INDENT

        x_bullet = self.margin + 0.04 * inch
        x_text = self.margin + (indent or BULLET_INDENT)
        line_w = self.page_width - self.margin - x_text

        lines = self.wrap_text(text, line_w, font_name, font_size)

        c.setFont(font_name, font_size)
        for i, line in enumerate(lines):
            y = self.check_page_break(c, y, needed=font_size + 2)
            if i == 0:
                c.drawString(x_bullet, y, bullet_char)
            c.drawString(x_text, y, line)
            y -= LINE_SPACING

        return y

    def draw_right_aligned(
        self,
        c: rl_canvas.Canvas,
        y: float,
        text: str,
        font_name: str = "Helvetica",
        font_size: int = 9,
    ) -> None:
        """Draw *text* right-aligned to the right margin at *y*."""
        w = stringWidth(text, font_name, font_size)
        x = self.page_width - self.margin - w
        c.setFont(font_name, font_size)
        c.drawString(x, y, text)
