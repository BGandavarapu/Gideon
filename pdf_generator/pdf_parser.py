"""ResumeParser — extract structured resume data from uploaded PDF, DOCX, or TXT files.

Uses ``pdfminer.six`` for PDF text extraction, ``python-docx`` for DOCX, and
plain UTF-8 decoding for TXT. Structured content extraction is performed first
via NVIDIA NIM (``nvidia/llama-3.3-nemotron-super-49b-v1.5``), falling back to
heuristic section parsers when the API is unavailable.

The result is a dict compatible with the MasterResume.content JSON schema::

    {
        "personal_info": {"name": ..., "email": ..., "phone": ...,
                          "location": ..., "linkedin": ..., "github": ...},
        "professional_summary": "...",
        "skills": [...],
        "work_experience": [{"title": ..., "company": ..., "location": ...,
                             "dates": ..., "bullets": [...]}],
        "education": [{"degree": ..., "institution": ..., "year": ..., "gpa": ...}],
        "certifications": [...],
        "projects": [{"name": ..., "description": ..., "technologies": [...]}],
    }

Before parsing, :class:`ResumeClassifier` runs a two-stage check:

1. **Heuristic** – fast pattern matching, no API call.
2. **NVIDIA NIM** – ``nvidia/llama-3.3-nemotron-super-49b-v1.5`` for inconclusive docs.

Non-resume documents (invoices, research papers, contracts, …) raise
:class:`NotAResumeError` before any expensive parse call is made.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_EMAIL_RE    = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
_PHONE_RE    = re.compile(
    r"(\+?1?\s*[-.]?\s*\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4})"
)
_LINKEDIN_RE = re.compile(r"linkedin\.com/in/[\w-]+", re.IGNORECASE)
_GITHUB_RE   = re.compile(r"github\.com/[\w-]+", re.IGNORECASE)
_SECTION_RE  = re.compile(
    r"^(SUMMARY|PROFESSIONAL SUMMARY|EXECUTIVE SUMMARY|CAREER SUMMARY|"
    r"EXPERIENCE|WORK EXPERIENCE|PROFESSIONAL EXPERIENCE|EMPLOYMENT|EMPLOYMENT HISTORY|WORK HISTORY|"
    r"EDUCATION|ACADEMIC BACKGROUND|"
    r"SKILLS|TECHNICAL SKILLS|CORE COMPETENCIES|TECHNOLOGIES|KEY SKILLS|AREAS OF EXPERTISE|"
    r"PROJECTS|KEY PROJECTS|PERSONAL PROJECTS|"
    r"CERTIFICATIONS|CERTIFICATES|LICENSES|"
    r"OBJECTIVE|PROFILE|CAREER OBJECTIVE)\b",
    re.IGNORECASE,
)

_NVIDIA_MODEL_ID = "nvidia/llama-3.3-nemotron-super-49b-v1.5"
_NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------


class NotAResumeError(Exception):
    """Raised when an uploaded file is not classified as a resume/CV."""

    def __init__(self, document_type: str, confidence: float, reason: str) -> None:
        self.document_type = document_type
        self.confidence = confidence
        self.reason = reason
        super().__init__(reason)


# ---------------------------------------------------------------------------
# Resume Classifier
# ---------------------------------------------------------------------------


class ResumeClassifier:
    """Two-stage classifier: heuristics first, NVIDIA NIM only for ambiguous docs.

    Stage 1 (heuristic) handles obvious cases — real resumes and clear
    non-resumes (invoices, research papers) — without an API call.

    Stage 2 (NVIDIA NIM) is invoked only when the heuristic returns
    ``"inconclusive"``, saving API quota.
    """

    CONFIDENCE_THRESHOLD = 0.70

    RESUME_HEADERS: frozenset = frozenset({
        "experience", "work experience", "employment history",
        "work history", "professional experience", "career history",
        "relevant experience", "employment", "work background",
        "education", "academic background", "qualifications",
        "skills", "technical skills", "core competencies",
        "technical expertise", "programming languages", "tools",
        "summary", "professional summary", "career summary",
        "objective", "career objective", "profile", "about me",
        "professional profile", "background",
        "certifications", "certificates", "achievements",
        "accomplishments", "projects", "publications", "references",
        "languages", "volunteer", "awards", "honors",
        "leadership", "professional development",
        "coursework", "activities", "interests", "portfolio",
    })

    NON_RESUME_SIGNALS: frozenset = frozenset({
        "invoice", "bill to", "purchase order", "payment due",
        "total amount", "tax invoice", "receipt",
        "quantity", "unit price", "subtotal",
        "abstract", "introduction", "methodology", "conclusion",
        "bibliography", "doi:", "arxiv",
        "findings", "results", "discussion",
        "whereas", "hereby", "hereinafter", "agreement",
        "terms and conditions", "privacy policy",
        "plaintiff", "defendant", "court",
        "chapter ", "table of contents",
        "exhibit", "section", "article",
        "profit", "loss", "balance sheet",
        "dear ", "sincerely,", "regards,", "to whom it may concern",
    })

    # ------------------------------------------------------------------
    # Stage 1: heuristic
    # ------------------------------------------------------------------

    def classify_heuristic(self, text: str) -> dict:
        """Fast, API-free classification based on keyword patterns.

        Returns a dict with keys:
            verdict  – ``"resume"`` | ``"not_resume"`` | ``"inconclusive"``
            confidence  – float 0.0–1.0
            signals_found – list of matched signal strings
            method   – ``"heuristic"``
        """
        lower = text.lower()
        signals_found: list[str] = []

        # Count resume headers
        resume_header_count = 0
        for header in self.RESUME_HEADERS:
            if header in lower:
                resume_header_count += 1
                signals_found.append(f"resume_header:{header}")

        # Count non-resume signals
        non_resume_count = 0
        for signal in self.NON_RESUME_SIGNALS:
            if signal in lower:
                non_resume_count += 1
                signals_found.append(f"non_resume:{signal}")

        # Personal contact info
        email_found = bool(_EMAIL_RE.search(text))
        phone_found = bool(_PHONE_RE.search(text))
        if email_found:
            signals_found.append("email")
        if phone_found:
            signals_found.append("phone")

        # Confidence calculation
        base = 0.0
        base += min(resume_header_count * 0.15, 0.60)
        base += 0.15 if email_found else 0.0
        base += 0.10 if phone_found else 0.0
        base -= min(non_resume_count * 0.25, 0.75)
        confidence = max(0.0, min(1.0, base))

        # Verdict
        if non_resume_count >= 2:
            verdict = "not_resume"
        elif confidence >= self.CONFIDENCE_THRESHOLD:
            verdict = "resume"
        elif confidence <= 0.30:
            verdict = "not_resume"
        else:
            verdict = "inconclusive"

        return {
            "verdict":       verdict,
            "confidence":    round(confidence, 4),
            "signals_found": signals_found,
            "document_type": "resume" if verdict == "resume" else "unknown",
            "reason":        "",
            "method":        "heuristic",
        }

    # ------------------------------------------------------------------
    # Stage 2: NVIDIA NIM
    # ------------------------------------------------------------------

    def classify_with_nvidia(self, text: str) -> dict:
        """Classify an ambiguous document using NVIDIA NIM (Nemotron).

        Returns the same shape as :meth:`classify_heuristic` with
        ``method="nvidia"`` or ``method="nvidia_failed"`` on parse error.

        Accepts is_resume=true at confidence >= 0.50 (the model has read
        the content; we trust it at a lower bar than the keyword heuristic).
        Requires confidence >= 0.60 for not_resume to avoid false rejections.
        """
        api_key = os.environ.get("NVIDIA_API_KEY", "")
        if not api_key:
            logger.warning("NVIDIA_API_KEY not set — NVIDIA classification skipped")
            return {
                "verdict":       "inconclusive",
                "confidence":    0.5,
                "document_type": "unknown",
                "signals_found": [],
                "reason":        "API key not configured",
                "method":        "nvidia_failed",
            }

        snippet = text[:2000]
        prompt = (
            "You are a document classifier. Analyze the following document "
            "text and determine if it is a resume or CV.\n\n"
            "A resume/CV typically contains:\n"
            "- Personal contact information (name, email, phone)\n"
            "- Work experience or employment history\n"
            "- Education background\n"
            "- Skills section\n"
            "- Written in first or third person about ONE individual\n\n"
            "A non-resume document includes: invoices, research papers, "
            "contracts, letters, articles, manuals, reports, presentations.\n\n"
            "Respond with ONLY a JSON object in this exact format:\n"
            '{"is_resume": true or false, "confidence": 0.0 to 1.0, '
            '"document_type": "resume" or "cv" or "invoice" or '
            '"research_paper" or "contract" or "letter" or "article" or "other", '
            '"reason": "one sentence explanation"}\n\n'
            f"Document text (first 2000 characters):\n{snippet}"
        )

        try:
            from openai import OpenAI as _OpenAI
            client = _OpenAI(
                base_url=_NVIDIA_BASE_URL,
                api_key=api_key,
            )
            response = client.chat.completions.create(
                model=_NVIDIA_MODEL_ID,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=256,
            )
            raw = (response.choices[0].message.content or "").strip()
            # Strip markdown code fences if present
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            parsed = json.loads(raw)
        except (json.JSONDecodeError, AttributeError, Exception) as exc:
            logger.warning("NVIDIA NIM classification parse failed: %s", exc)
            return {
                "verdict":       "inconclusive",
                "confidence":    0.5,
                "document_type": "unknown",
                "signals_found": [],
                "reason":        "Could not parse NVIDIA NIM response",
                "method":        "nvidia_failed",
            }

        is_resume  = bool(parsed.get("is_resume", False))
        confidence = float(parsed.get("confidence", 0.5))
        doc_type   = str(parsed.get("document_type", "unknown"))
        reason     = str(parsed.get("reason", ""))

        # Trust the model's is_resume=true at a lower confidence bar (0.50)
        # since it has read the content. Be more conservative rejecting (0.60).
        if is_resume and confidence >= 0.50:
            verdict = "resume"
        elif not is_resume and confidence >= 0.60:
            verdict = "not_resume"
        else:
            verdict = "inconclusive"

        return {
            "verdict":       verdict,
            "confidence":    round(confidence, 4),
            "document_type": doc_type,
            "signals_found": [],
            "reason":        reason,
            "method":        "nvidia",
        }

    # ------------------------------------------------------------------
    # Full pipeline
    # ------------------------------------------------------------------

    def classify(self, text: str) -> dict:
        """Run the two-stage classification pipeline.

        Stage 1 (heuristic) runs first. NVIDIA NIM is only called when the
        heuristic returns ``"inconclusive"``. If both stages are inconclusive
        the verdict defaults to ``"not_resume"``.

        Returns a dict with at minimum:
            verdict, confidence, document_type, method, signals_found, reason
        """
        h_result = self.classify_heuristic(text)

        if h_result["verdict"] != "inconclusive":
            logger.info(
                "Document classified as %s (confidence=%.2f, method=%s)",
                h_result["verdict"], h_result["confidence"], h_result["method"],
            )
            return h_result

        # Heuristic inconclusive → call NVIDIA NIM
        g_result = self.classify_with_nvidia(text)

        if g_result["verdict"] != "inconclusive":
            logger.info(
                "Document classified as %s (confidence=%.2f, method=%s)",
                g_result["verdict"], g_result["confidence"], g_result["method"],
            )
            return g_result

        # Both inconclusive → safe default
        fallback = {
            "verdict":       "not_resume",
            "confidence":    0.0,
            "document_type": "unknown",
            "signals_found": h_result.get("signals_found", []),
            "reason":        "Could not confidently classify document",
            "method":        g_result["method"],
        }
        logger.info(
            "Document classified as not_resume (confidence=0.00, method=both_inconclusive)"
        )
        return fallback


# ---------------------------------------------------------------------------
# Resume Parser (PDF, DOCX, TXT)
# ---------------------------------------------------------------------------


class ResumePDFParser:
    """Parse a resume file (PDF, DOCX, or TXT) into a structured dictionary.

    Usage::

        parser = ResumePDFParser()
        data   = parser.parse(file_bytes, file_extension="pdf")
    """

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def parse(self, file_bytes: bytes, file_extension: str = "pdf") -> Dict[str, Any]:
        """Extract resume data from *file_bytes*.

        Runs :class:`ResumeClassifier` BEFORE structured parsing. If the
        document is not a resume, :class:`NotAResumeError` is raised.

        Structured extraction is attempted via NVIDIA NIM first; heuristic
        section parsers are used as a fallback when the API is unavailable.

        Args:
            file_bytes: Raw file contents.
            file_extension: One of ``"pdf"``, ``"docx"``, ``"txt"`` (without dot).

        Returns:
            Dict with keys: ``personal_info``, ``professional_summary``,
            ``skills``, ``work_experience``, ``education``, ``projects``.

        Raises:
            NotAResumeError: If the document is classified as a non-resume.
            ValueError: If the file cannot be parsed or is an unsupported type.
        """
        ext = file_extension.lower().lstrip(".")

        font_family = "Helvetica"
        detected_bullet = "none"
        name_alignment = "left"
        if ext == "pdf":
            text = self._extract_text(file_bytes)
            font_family = self._detect_font_family(file_bytes)
            detected_bullet = self._detect_bullet_char(file_bytes)
            name_alignment = self._detect_name_alignment(file_bytes)
        elif ext == "docx":
            text = self._extract_text_from_docx(file_bytes)
        elif ext == "txt":
            text = self._extract_text_from_txt(file_bytes)
        else:
            raise ValueError(
                f"Unsupported file type: .{ext}. Upload a PDF, Word (.docx), or plain text (.txt) file."
            )

        if not text.strip():
            raise ValueError("Could not extract text from file. Is it a scanned image or empty?")

        logger.info(
            "Extracted %d chars from .%s file. Preview: %.200r",
            len(text), ext, text[:200],
        )

        # -- Classification gate --
        classifier = ResumeClassifier()
        classification = classifier.classify(text)

        if classification["verdict"] == "not_resume":
            raise NotAResumeError(
                document_type=classification.get("document_type", "unknown"),
                confidence=classification["confidence"],
                reason=(
                    classification.get("reason")
                    or "Document does not appear to be a resume"
                ),
            )

        if classification["verdict"] == "inconclusive":
            raise NotAResumeError(
                document_type="unknown",
                confidence=classification["confidence"],
                reason=(
                    "Could not confidently determine if this is a resume. "
                    "Please upload a standard resume or CV."
                ),
            )

        # -- Try NVIDIA NIM structured parsing first --
        result = self._parse_with_nvidia(text)

        # If NIM returned but with empty skills AND experience, discard it.
        # `skills` may be a dict (categorised) or a list — both are truthy when populated.
        if result and not result.get("skills") and not result.get("work_experience"):
            logger.warning("NIM parse returned empty skills and experience — falling back to heuristic.")
            result = None

        if not result:
            # Fall back to heuristic section parsers
            logger.info("Using heuristic section parsers for resume extraction.")
            lines = [l.rstrip() for l in text.splitlines()]
            result = {
                "personal_info":        self._parse_personal_info(lines),
                "professional_summary": self._parse_summary(lines),
                "skills":               self._parse_skills(lines),
                "work_experience":      self._parse_experience(lines),
                "education":            self._parse_education(lines),
                "certifications":       self._parse_certifications(lines),
                "projects":             self._parse_projects(lines),
            }

        # Ensure all expected keys are present with safe defaults
        result.setdefault("personal_info", {})
        result.setdefault("professional_summary", "")
        result.setdefault("skills", [])
        result.setdefault("work_experience", [])
        result.setdefault("education", [])
        result.setdefault("certifications", [])
        result.setdefault("projects", [])
        result.setdefault("section_header_style", {"case": "title_colon", "rule": True})

        # Normalize field names to canonical schema
        result = self._normalize_parsed_result(result)

        # Attach detected font family so the style fingerprint can carry it
        # downstream to the PDF generator.
        result["font_family"] = font_family
        result["detected_bullet_char"] = detected_bullet
        result["name_alignment"] = name_alignment

        logger.info(
            "Resume parsed: name=%r, skills=%d, experience=%d, certs=%d, projects=%d",
            result["personal_info"].get("name", "?"),
            len(result["skills"]),
            len(result["work_experience"]),
            len(result["certifications"]),
            len(result["projects"]),
        )
        return result

    # ------------------------------------------------------------------
    # Text extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_text(pdf_bytes: bytes) -> str:
        """Return plain text from a PDF using pdfminer.six."""
        try:
            from pdfminer.high_level import extract_text as _extract  # type: ignore
            return _extract(io.BytesIO(pdf_bytes))
        except ImportError:
            raise ValueError(
                "pdfminer.six is required for PDF parsing. "
                "Install it with: pip install pdfminer.six"
            )
        except Exception as exc:
            raise ValueError(f"PDF extraction failed: {exc}") from exc

    @staticmethod
    def _detect_font_family(pdf_bytes: bytes) -> str:
        """Detect the dominant font family from a PDF's body text.

        Walks pdfminer's layout tree, weights each LTChar by count, and
        returns one of ReportLab's built-in families: ``"Times"``,
        ``"Helvetica"``, or ``"Courier"``. Returns ``"Helvetica"`` on any
        error (safe default matching the existing pipeline).
        """
        try:
            from pdfminer.high_level import extract_pages  # type: ignore
            from pdfminer.layout import LTChar, LTTextContainer  # type: ignore
        except ImportError:
            return "Helvetica"

        times_markers = ("times", "serif", "roman", "garamond", "georgia",
                         "cambria", "bookman", "palatino")
        courier_markers = ("mono", "courier", "consolas", "inconsolata", "menlo")

        times_chars = 0
        courier_chars = 0
        helvetica_chars = 0

        def _count_char(obj):
            nonlocal times_chars, courier_chars, helvetica_chars
            if isinstance(obj, LTChar):
                name = (obj.fontname or "").lower()
                if any(m in name for m in times_markers):
                    times_chars += 1
                elif any(m in name for m in courier_markers):
                    courier_chars += 1
                else:
                    helvetica_chars += 1
            elif hasattr(obj, "__iter__"):
                for child in obj:
                    _count_char(child)

        try:
            for page in extract_pages(io.BytesIO(pdf_bytes)):
                for element in page:
                    if not isinstance(element, LTTextContainer):
                        continue
                    _count_char(element)
                total = times_chars + courier_chars + helvetica_chars
                if total > 500:
                    break
        except Exception as exc:
            logger.debug("Font detection failed: %s", exc)
            return "Helvetica"

        total = times_chars + courier_chars + helvetica_chars
        if total == 0:
            return "Helvetica"

        # Winner-takes-all
        if times_chars >= helvetica_chars and times_chars >= courier_chars:
            return "Times"
        if courier_chars >= helvetica_chars:
            return "Courier"
        return "Helvetica"

    @staticmethod
    def _detect_bullet_char(pdf_bytes: bytes) -> str:
        """Detect the dominant bullet character from the raw PDF text.

        Scans each line for a leading special character from a known set of
        bullet markers. Returns the most common one, or ``"none"`` if no
        bullet characters are found.
        """
        _KNOWN_BULLETS = frozenset("●•○◦■□▪▸►➤-*—–")

        try:
            from pdfminer.high_level import extract_text  # type: ignore

            text = extract_text(io.BytesIO(pdf_bytes), maxpages=3)
        except Exception:
            return "none"

        counts: dict[str, int] = {}
        for line in text.split("\n"):
            stripped = line.strip()
            if len(stripped) < 3:
                continue
            first = stripped[0]
            if first in _KNOWN_BULLETS and stripped[1] in (" ", "\t"):
                counts[first] = counts.get(first, 0) + 1

        if not counts:
            return "none"
        return max(counts, key=counts.get)

    @staticmethod
    def _detect_name_alignment(pdf_bytes: bytes) -> str:
        """Detect whether the candidate's name is centered or left-aligned.

        Uses pdfminer to find the first text element on the first page and
        compares its horizontal midpoint to the page center.  Returns
        ``"center"`` or ``"left"``.
        """
        try:
            from pdfminer.high_level import extract_pages  # type: ignore
            from pdfminer.layout import LTTextBoxHorizontal  # type: ignore

            for page in extract_pages(io.BytesIO(pdf_bytes), maxpages=1):
                page_width = page.width
                page_center = page_width / 2.0
                tolerance = page_width * 0.15

                for element in page:
                    if not isinstance(element, LTTextBoxHorizontal):
                        continue
                    text = element.get_text().strip()
                    if not text or len(text) < 2:
                        continue
                    elem_mid = (element.x0 + element.x1) / 2.0
                    if abs(elem_mid - page_center) < tolerance:
                        return "center"
                    return "left"
        except Exception:
            pass
        return "left"

    @staticmethod
    def _extract_text_from_docx(file_bytes: bytes) -> str:
        """Return plain text from a DOCX file using python-docx."""
        try:
            import docx  # type: ignore  # python-docx
            doc = docx.Document(io.BytesIO(file_bytes))
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            return "\n".join(paragraphs)
        except Exception as exc:
            raise ValueError(f"DOCX extraction failed: {exc}") from exc

    @staticmethod
    def _extract_text_from_txt(file_bytes: bytes) -> str:
        """Decode a plain-text file, trying UTF-8 then latin-1."""
        for enc in ("utf-8", "latin-1"):
            try:
                return file_bytes.decode(enc)
            except UnicodeDecodeError:
                continue
        return file_bytes.decode("ascii", errors="replace")

    # ------------------------------------------------------------------
    # NVIDIA NIM structured parsing
    # ------------------------------------------------------------------

    def _parse_with_nvidia(self, text: str) -> Optional[Dict[str, Any]]:
        """Use NVIDIA NIM to extract structured resume sections from plain text.

        Returns a fully populated content dict on success, or ``None`` when
        the API key is missing or the call fails (so callers fall back to
        heuristic parsers).
        """
        api_key = os.environ.get("NVIDIA_API_KEY", "")
        if not api_key:
            return None

        # Cap input to stay within token limits (15k chars ≈ 3.75k tokens)
        snippet = text[:15000]

        system_msg = (
            "You are a precise resume parser. You extract structured data from resume text "
            "and return ONLY valid JSON — no markdown fences, no explanation, no preamble.\n\n"
            "Return a JSON object with this exact structure:\n"
            "{\n"
            '  "personal_info": {"name": "", "email": "", "phone": "", "location": "", "linkedin": "", "github": ""},\n'
            '  "professional_summary": "",\n'
            '  "skills": {"Category Name": ["skill1", "skill2"]},\n'
            '  "work_experience": [\n'
            '    {"title": "", "company": "", "location": "", '
            '"dates": "", "bullets": ["..."]}\n'
            '  ],\n'
            '  "education": [\n'
            '    {"degree": "", "institution": "", "location": "", '
            '"year": "", "gpa": "", "coursework": ""}\n'
            '  ],\n'
            '  "certifications": ["cert1"],\n'
            '  "projects": [\n'
            '    {"name": "", "date": "", "bullets": ["..."], "technologies": ["..."]}\n'
            '  ],\n'
            '  "section_order": ["education", "work_experience", "projects", "skills"],\n'
            '  "section_header_style": {"case": "title_colon", "rule": true}\n'
            "}\n\n"
            "CRITICAL RULES — violating any of these is wrong:\n"
            "\n"
            "SKILLS:\n"
            "- Extract ALL skills mentioned anywhere in the resume.\n"
            "- If the resume organizes skills into labeled categories like 'Programming Languages:', "
            "'Frameworks and Libraries:', 'Tools:', 'Databases:' — you MUST preserve them as a JSON "
            "object where each key is the EXACT category label (without the trailing colon) and each "
            "value is an array of the skills listed under it. Example: "
            '{"Programming Languages": ["Python", "Java"], "Frameworks and Libraries": ["React", "Flask"]}.\n'
            "- Only use a flat array when the resume has a single uncategorized skills list.\n"
            "\n"
            "WORK EXPERIENCE:\n"
            "- Each entry must include EVERY bullet point under it verbatim in the bullets array — "
            "never collapse bullets into a single paragraph.\n"
            '- Combine start and end dates into one string (e.g. "May 2023 - August 2023" or "2022 - Present").\n'
            "\n"
            "PROJECTS:\n"
            "- EVERY line that starts with a bullet character (•, -, *, ●) under a project name MUST "
            "go into that project's bullets array. NEVER collapse them into a description paragraph.\n"
            "- If a project has no bulleted lines but has descriptive sentences, put each sentence as "
            "its own entry in the bullets array.\n"
            '- For project date, extract the date or timeframe if present (e.g. "March 2023").\n'
            "\n"
            "EDUCATION:\n"
            '- The `year` field MUST be the FULL date range as it appears in the PDF '
            '(e.g. "August 2024 - May 2026"), NOT just the end year.\n'
            "- Extract the city/state into `location` if present (e.g. \"Tempe, AZ\").\n"
            "- `coursework` MUST be populated if the resume has a 'Relevant Coursework' or similar "
            "label — extract every comma-separated item verbatim as one string.\n"
            "\n"
            "PERSONAL INFO:\n"
            "- Scan the first 15 lines for any URL matching `linkedin.com/in/<handle>` or "
            "`github.com/<handle>` — these MUST be captured into the linkedin / github fields "
            "even if they appear as hyperlinks or without an https:// prefix.\n"
            "\n"
            "SUMMARY / CERTIFICATIONS:\n"
            "- Extract the summary/objective paragraph into professional_summary if present.\n"
            "- Extract all certifications, licenses, and certificates into the certifications array.\n"
            "\n"
            "SECTION ORDER & HEADER STYLE:\n"
            "- section_order: list the sections IN THE EXACT ORDER THEY APPEAR in the resume, using "
            "these keys: professional_summary, education, work_experience, skills, projects, certifications. "
            "Omit sections that don't exist.\n"
            "- section_header_style.case: use \"title_colon\" if headers look like "
            "'Education:' or 'Work Experience:' (title case with trailing colon); use \"upper\" if "
            "headers are ALL CAPS like 'EDUCATION' / 'WORK EXPERIENCE'; use \"title\" if title case with "
            "no colon. Default to \"title_colon\" if unclear.\n"
            "- section_header_style.rule: true if the original has a thin horizontal line under each "
            "section header, false otherwise. Default to true.\n"
            "\n"
            "GENERAL:\n"
            "- If a field is not found, use an empty string or empty list/object.\n"
            "- Do NOT hallucinate data not present in the text."
        )

        try:
            from openai import OpenAI as _OpenAI
            client = _OpenAI(base_url=_NVIDIA_BASE_URL, api_key=api_key)
            response = client.chat.completions.create(
                model=_NVIDIA_MODEL_ID,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": f"Parse this resume:\n\n{snippet}"},
                ],
                temperature=0.1,
                max_tokens=8192,
            )
            raw = (response.choices[0].message.content or "").strip()
            # Strip markdown code fences if present
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            parsed = json.loads(raw)
            # Only accept if it has at least one meaningful section
            if parsed.get("skills") or parsed.get("work_experience"):
                logger.info(
                    "NIM resume parse: skills=%d, experience=%d",
                    len(parsed.get("skills", [])),
                    len(parsed.get("work_experience", [])),
                )
                return parsed
            logger.warning("NIM returned empty skills and work_experience — falling back to heuristic")
        except Exception as exc:
            logger.warning("NIM resume parsing failed: %s", exc)
        return None

    # ------------------------------------------------------------------
    # Post-processing normalization
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_parsed_result(result: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize field names so output always matches the canonical schema.

        Handles mismatches between NIM output, heuristic output, and the
        canonical sample resume format used by templates and the modifier.
        """
        # --- personal_info: ensure linkedin/github keys ---
        pi = result.get("personal_info", {})
        pi.setdefault("name", "")
        pi.setdefault("email", "")
        pi.setdefault("phone", "")
        pi.setdefault("location", "")
        pi.setdefault("linkedin", "")
        pi.setdefault("github", "")
        result["personal_info"] = pi

        # --- work_experience: start_date/end_date → dates ---
        for job in result.get("work_experience", []):
            if "dates" not in job or not job["dates"]:
                sd = job.pop("start_date", "")
                ed = job.pop("end_date", "")
                if sd or ed:
                    job["dates"] = f"{sd} - {ed}".strip(" -")
                else:
                    job["dates"] = ""
            else:
                job.pop("start_date", None)
                job.pop("end_date", None)

        # --- education: school → institution, graduation_year → year ---
        for edu in result.get("education", []):
            if "institution" not in edu and "school" in edu:
                edu["institution"] = edu.pop("school")
            if "year" not in edu and "graduation_year" in edu:
                edu["year"] = edu.pop("graduation_year")
            edu.setdefault("institution", "")
            edu.setdefault("year", "")
            edu.setdefault("location", "")
            edu.setdefault("coursework", "")

        # --- projects: ensure bullets/date keys ---
        for proj in result.get("projects", []):
            proj.setdefault("bullets", [])
            proj.setdefault("date", "")
            proj.setdefault("technologies", [])
            # If description exists but bullets is empty, keep description as-is
            # Templates will fall back to description when bullets is empty

        # --- certifications: ensure key exists ---
        result.setdefault("certifications", [])

        return result

    # ------------------------------------------------------------------
    # Heuristic section parsers (fallback)
    # ------------------------------------------------------------------

    def _parse_personal_info(self, lines: List[str]) -> Dict[str, str]:
        info: Dict[str, str] = {
            "name": "", "email": "", "phone": "", "location": "",
            "linkedin": "", "github": "",
        }
        # Name: first non-empty line is usually the name
        for line in lines[:10]:
            stripped = line.strip()
            if stripped and not _EMAIL_RE.search(stripped) and not _PHONE_RE.search(stripped):
                info["name"] = stripped
                break

        full_text = "\n".join(lines[:30])
        m = _EMAIL_RE.search(full_text)
        if m:
            info["email"] = m.group(0)
        m = _PHONE_RE.search(full_text)
        if m:
            info["phone"] = m.group(1).strip()

        # LinkedIn / GitHub
        m = _LINKEDIN_RE.search(full_text)
        if m:
            info["linkedin"] = m.group(0)
        m = _GITHUB_RE.search(full_text)
        if m:
            info["github"] = m.group(0)

        # Location: line with "City, ST" or "City, Country" pattern
        loc_re = re.compile(r"([A-Z][a-zA-Z ]+,\s*[A-Z]{2,})")
        for line in lines[:20]:
            m = loc_re.search(line)
            if m:
                info["location"] = m.group(1)
                break

        return info

    def _parse_summary(self, lines: List[str]) -> str:
        in_section = False
        buf: List[str] = []
        for line in lines:
            upper = line.strip().upper()
            if re.match(r"^(SUMMARY|OBJECTIVE|PROFILE|PROFESSIONAL SUMMARY|EXECUTIVE SUMMARY|CAREER SUMMARY|CAREER OBJECTIVE|ABOUT ME)\b", upper):
                in_section = True
                continue
            if in_section:
                if _SECTION_RE.match(line.strip()) and buf:
                    break
                if line.strip():
                    buf.append(line.strip())
                    if len(buf) >= 5:
                        break
        return " ".join(buf)

    def _parse_skills(self, lines: List[str]) -> List[str]:
        in_section = False
        raw_skills: List[str] = []

        for line in lines:
            upper = line.strip().upper()
            if re.match(r"^(SKILLS?|TECHNICAL SKILLS|CORE COMPETENCIES|TECHNOLOGIES|KEY SKILLS|AREAS OF EXPERTISE|TOOLS & TECHNOLOGIES)\b", upper):
                in_section = True
                continue
            if in_section:
                if _SECTION_RE.match(line.strip()) and raw_skills:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                # Comma/pipe/bullet separated on a single line
                for sep in ("|", "•", "·", ","):
                    if sep in stripped:
                        raw_skills.extend(s.strip() for s in stripped.split(sep))
                        break
                else:
                    raw_skills.append(stripped)

        # Deduplicate and remove noise
        seen: set = set()
        skills: List[str] = []
        for s in raw_skills:
            clean = s.strip(" •·-")
            if clean and len(clean) > 1 and clean not in seen:
                seen.add(clean)
                skills.append(clean)
        return skills

    def _parse_experience(self, lines: List[str]) -> List[Dict[str, Any]]:
        in_section = False
        jobs: List[Dict[str, Any]] = []
        current: Dict[str, Any] | None = None

        date_re = re.compile(
            r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|\d{4})"
            r".*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|\d{4}|Present|Current)\b",
            re.IGNORECASE,
        )

        for line in lines:
            stripped = line.strip()
            upper = stripped.upper()

            if re.match(r"^(EXPERIENCE|WORK HISTORY|WORK EXPERIENCE|PROFESSIONAL EXPERIENCE|EMPLOYMENT|EMPLOYMENT HISTORY)\b", upper):
                in_section = True
                continue

            if in_section:
                if _SECTION_RE.match(stripped) and stripped.upper() not in (
                    "EXPERIENCE", "WORK HISTORY", "WORK EXPERIENCE"
                ):
                    if current:
                        jobs.append(current)
                    break

                if not stripped:
                    continue

                m = date_re.search(stripped)
                if m and current is None:
                    current = {"title": "", "company": stripped, "dates": stripped, "bullets": []}
                elif m and current:
                    jobs.append(current)
                    current = {"title": "", "company": stripped, "dates": stripped, "bullets": []}
                elif current:
                    if not current["title"]:
                        current["title"] = stripped
                    elif stripped.startswith(("•", "-", "*", "·")):
                        current["bullets"].append(stripped.lstrip("•-*· "))
                    elif len(stripped) > 20:
                        current["bullets"].append(stripped)

        if current:
            jobs.append(current)
        return jobs

    def _parse_education(self, lines: List[str]) -> List[Dict[str, str]]:
        in_section = False
        schools: List[Dict[str, str]] = []
        current: Dict[str, str] | None = None

        degree_re = re.compile(
            r"\b(bachelor|master|phd|doctorate|associate|b\.s|m\.s|b\.a|m\.a|mba)\b",
            re.IGNORECASE,
        )

        for line in lines:
            stripped = line.strip()
            upper = stripped.upper()

            if re.match(r"^EDUCATION\b", upper):
                in_section = True
                continue

            if in_section:
                if _SECTION_RE.match(stripped) and stripped.upper() != "EDUCATION":
                    if current:
                        schools.append(current)
                    break

                if not stripped:
                    continue

                m = degree_re.search(stripped)
                if m:
                    if current:
                        schools.append(current)
                    current = {"degree": stripped, "institution": "", "year": ""}
                elif current and not current["institution"]:
                    current["institution"] = stripped
                elif current and not current["year"]:
                    year_m = re.search(r"\b(19|20)\d{2}\b", stripped)
                    if year_m:
                        current["year"] = year_m.group(0)

        if current:
            schools.append(current)
        return schools

    def _parse_certifications(self, lines: List[str]) -> List[str]:
        in_section = False
        certs: List[str] = []

        for line in lines:
            upper = line.strip().upper()
            if re.match(r"^(CERTIFICATIONS?|CERTIFICATES?|LICENSES?)\b", upper):
                in_section = True
                continue
            if in_section:
                stripped = line.strip()
                if _SECTION_RE.match(stripped) and certs:
                    break
                if stripped:
                    # Strip bullet characters
                    clean = stripped.lstrip("•-*· ")
                    if clean:
                        certs.append(clean)
        return certs

    def _parse_projects(self, lines: List[str]) -> List[Dict[str, Any]]:
        in_section = False
        projects: List[Dict[str, Any]] = []
        current: Dict[str, Any] | None = None

        for line in lines:
            stripped = line.strip()
            upper = stripped.upper()

            if re.match(r"^(PROJECTS|KEY PROJECTS|PERSONAL PROJECTS)\b", upper):
                in_section = True
                continue

            if in_section:
                if _SECTION_RE.match(stripped) and stripped.upper() not in (
                    "PROJECTS", "KEY PROJECTS", "PERSONAL PROJECTS"
                ):
                    if current:
                        projects.append(current)
                    break

                if not stripped:
                    continue

                # A short line (< 60 chars) without bullet chars is likely a project name
                is_bullet = stripped[0] in "•-*·"
                if not is_bullet and len(stripped) < 60 and current is not None:
                    projects.append(current)
                    current = {"name": stripped, "description": "", "technologies": []}
                elif current is None:
                    current = {"name": stripped, "description": "", "technologies": []}
                elif current:
                    desc_line = stripped.lstrip("•-*· ")
                    if current["description"]:
                        current["description"] += " " + desc_line
                    else:
                        current["description"] = desc_line

        if current:
            projects.append(current)
        return projects
