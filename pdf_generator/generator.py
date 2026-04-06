"""
PDF generation orchestrator.

:class:`PDFGenerator` is the single entry-point for creating PDF resumes.
It handles template selection, data validation, output path management, and
error reporting so that callers (CLI, tests) have a clean interface.

Supported templates
-------------------
+----------+------------------------------------------+
| Name     | Class                                    |
+==========+==========================================+
| ``ats``  | :class:`~.templates.ATSOptimizedTemplate` |
+----------+------------------------------------------+
| ``classic`` | :class:`~.templates.ClassicTemplate`  |
+----------+------------------------------------------+

Usage::

    from pdf_generator.generator import PDFGenerator

    gen = PDFGenerator(output_dir="data/output")
    path = gen.generate(resume_data, template_name="ats")
    # path -> "data/output/alex_smith_ats_20260319_143000.pdf"
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from pdf_generator.templates.ats_optimized import ATSOptimizedTemplate
from pdf_generator.templates.classic import ClassicTemplate

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Template registry
# ---------------------------------------------------------------------------

_TEMPLATES = {
    "ats":     ATSOptimizedTemplate,
    "classic": ClassicTemplate,
}

AVAILABLE_TEMPLATES = list(_TEMPLATES.keys())


class PDFGenerator:
    """Orchestrate PDF resume generation.

    Args:
        output_dir: Directory where generated PDFs are saved.
                    Created automatically if it does not exist.
    """

    def __init__(self, output_dir: str = "data/output") -> None:
        self.output_dir = output_dir
        Path(output_dir).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(
        self,
        resume_data: Dict,
        template_name: str = "ats",
        filename: Optional[str] = None,
    ) -> str:
        """Generate a PDF resume and return the filesystem path.

        Args:
            resume_data:   Structured resume dict (see validation rules below).
            template_name: One of ``"ats"`` or ``"classic"``.
            filename:      Optional custom filename.  A timestamp-based name is
                           generated when omitted.  The ``.pdf`` extension is
                           appended automatically if missing.

        Returns:
            Absolute path to the generated PDF file.

        Raises:
            ValueError: If *template_name* is not recognised or required
                        resume sections / fields are missing.
            RuntimeError: If the underlying template raises an error during
                          PDF creation.
        """
        # -- template validation --
        template_name = template_name.lower().strip()
        if template_name not in _TEMPLATES:
            raise ValueError(
                f"Unknown template '{template_name}'. "
                f"Available: {', '.join(AVAILABLE_TEMPLATES)}"
            )

        # -- data validation --
        self._validate(resume_data)

        # -- output path --
        output_path = self._build_output_path(resume_data, template_name, filename)

        # -- generate --
        template = _TEMPLATES[template_name]()
        try:
            template.generate_pdf(resume_data, output_path)
        except Exception as exc:
            logger.exception("PDF generation failed for template='%s'", template_name)
            raise RuntimeError(f"PDF generation failed: {exc}") from exc

        file_size = Path(output_path).stat().st_size
        logger.info(
            "PDF generated: %s  template=%s  size=%d bytes",
            output_path, template_name, file_size,
        )
        return output_path

    def generate_from_db(
        self,
        tailored_resume_id: int,
        template_name: str = "ats",
        filename: Optional[str] = None,
    ) -> str:
        """Convenience wrapper: load a :class:`~database.models.TailoredResume`
        by ID and generate a PDF, then persist the path back to the DB row.

        Args:
            tailored_resume_id: Database ID of the :class:`TailoredResume`.
            template_name:      Template to use.
            filename:           Optional custom filename.

        Returns:
            Path to the generated PDF.

        Raises:
            LookupError: If no tailored resume exists with that ID.
            ValueError:  If data validation fails.
            RuntimeError: If PDF creation fails.
        """
        from database.database import get_db
        from database.models import TailoredResume

        with get_db() as db:
            row = db.query(TailoredResume).filter(TailoredResume.id == tailored_resume_id).first()
            if row is None:
                raise LookupError(f"No TailoredResume found with id={tailored_resume_id}")

            resume_data: dict = row.tailored_content or {}
            output_path = self.generate(resume_data, template_name, filename)

            row.pdf_path = output_path
            db.commit()

        return output_path

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate(data: Dict) -> None:
        """Raise :class:`ValueError` for structurally invalid resume data.

        Rules
        -----
        * ``personal_info`` section must be present.
        * ``personal_info`` must include ``name`` and ``email``.
        * ``name`` must be a non-empty string.
        * ``email`` must be a non-empty string.
        """
        if not isinstance(data, dict):
            raise ValueError("resume_data must be a dictionary.")

        if "personal_info" not in data:
            raise ValueError("Missing required section: personal_info")

        pi = data["personal_info"]
        if not isinstance(pi, dict):
            raise ValueError("personal_info must be a dictionary.")

        for field in ("name", "email"):
            if field not in pi:
                raise ValueError(f"Missing required field in personal_info: {field}")
            if not str(pi[field]).strip():
                raise ValueError(f"personal_info.{field} must not be blank.")

    # ------------------------------------------------------------------
    # Path building
    # ------------------------------------------------------------------

    def _build_output_path(
        self,
        resume_data: Dict,
        template_name: str,
        filename: Optional[str],
    ) -> str:
        if filename:
            if not filename.endswith(".pdf"):
                filename += ".pdf"
            return str(Path(self.output_dir) / filename)

        raw_name = resume_data.get("personal_info", {}).get("name", "resume")
        safe_name = (
            raw_name.strip()
            .lower()
            .replace(" ", "_")
            .replace("/", "_")
            .replace("\\", "_")
        )
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        auto_name = f"{safe_name}_{template_name}_{timestamp}.pdf"
        return str(Path(self.output_dir) / auto_name)
