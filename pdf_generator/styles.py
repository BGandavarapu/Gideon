"""
Centralised style definitions for PDF resume templates.

All colour values, font sizes, spacing constants and layout measurements
live here so that templates never hard-code presentation details.
"""

from reportlab.lib import colors
from reportlab.lib.units import inch

# ---------------------------------------------------------------------------
# Page layout
# ---------------------------------------------------------------------------

MARGIN = 0.70 * inch          # all four page margins
LINE_SPACING = 14              # points between body text lines
SECTION_GAP = 14               # extra vertical space before each section header
BULLET_INDENT = 0.18 * inch   # left indent for bullet points
BULLET_CHAR = "\u2022"         # Unicode bullet (U+2022)

# Minimum y-position before a new page is forced (avoid orphaned headers)
PAGE_BOTTOM_MARGIN = 1.1 * inch

# ---------------------------------------------------------------------------
# Colour palettes
# ---------------------------------------------------------------------------

class ClassicPalette:
    """Subtle blue-accent palette for the Classic template."""
    name_text    = colors.HexColor("#1a1a2e")
    header_text  = colors.HexColor("#16213e")
    accent       = colors.HexColor("#0f3460")
    body_text    = colors.HexColor("#2c2c2c")
    rule_line    = colors.HexColor("#0f3460")
    contact_text = colors.HexColor("#444444")


class ATSPalette:
    """Monochrome palette for the ATS-optimised template."""
    name_text    = colors.black
    header_text  = colors.black
    accent       = colors.black
    body_text    = colors.black
    rule_line    = colors.black
    contact_text = colors.black


# ---------------------------------------------------------------------------
# Font sizes
# ---------------------------------------------------------------------------

class FontSizes:
    name        = 17
    section     = 11
    job_title   = 11
    company     = 10
    body        = 10
    contact     = 10
    bullet      = 10
    skills      = 10
    date_right  = 9


# ---------------------------------------------------------------------------
# Section ordering
# (templates iterate this list; missing sections are silently skipped)
# ---------------------------------------------------------------------------

SECTION_ORDER = [
    "professional_summary",
    "work_experience",
    "education",
    "skills",
    "certifications",
    "projects",
]

SECTION_TITLES = {
    "professional_summary": "PROFESSIONAL SUMMARY",
    "work_experience":      "WORK EXPERIENCE",
    "education":            "EDUCATION",
    "skills":               "SKILLS",
    "certifications":       "CERTIFICATIONS",
    "projects":             "PROJECTS",
}
