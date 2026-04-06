"""
PDF generator package - ReportLab-based resume PDF creation (Phase 5).

Public exports::

    from pdf_generator import PDFGenerator, ATSOptimizedTemplate, ClassicTemplate, AVAILABLE_TEMPLATES
"""

from pdf_generator.generator import PDFGenerator, AVAILABLE_TEMPLATES
from pdf_generator.templates.ats_optimized import ATSOptimizedTemplate
from pdf_generator.templates.classic import ClassicTemplate

__all__ = [
    "PDFGenerator",
    "AVAILABLE_TEMPLATES",
    "ATSOptimizedTemplate",
    "ClassicTemplate",
]
