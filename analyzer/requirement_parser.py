"""
Structured requirement extraction from job descriptions.

Parses three types of structured requirements that appear in almost every
job posting but are buried in unstructured prose:

    ExperienceRequirement  – "5+ years of Python experience"
    EducationRequirement   – "Bachelor's degree in Computer Science required"
    CertificationRequirement – "AWS Certified Solutions Architect preferred"

All parsing is regex-based so there are no additional NLP dependencies.
Patterns are applied in priority order; the first match wins to avoid
double-counting the same requirement phrase.

Typical usage::

    parser = RequirementParser()
    result = parser.parse(job_description)
    print(result.min_years_experience)   # 3
    print(result.education_level)        # "bachelor"
    print(result.certifications)         # ["aws solutions architect"]
"""

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ExperienceRequirement:
    """A single years-of-experience requirement extracted from job text.

    Attributes:
        skill: The skill or domain the experience refers to.  May be ``""``
            when the pattern expresses general experience without a named skill.
        min_years: Minimum number of years required.
        max_years: Upper bound of a range (``None`` if no upper bound stated).
        is_minimum: ``True`` when the posting uses "X+" notation.
        raw_text: The original matched substring (useful for debugging).
    """

    skill: str
    min_years: int
    max_years: Optional[int] = None
    is_minimum: bool = False
    raw_text: str = ""


@dataclass
class EducationRequirement:
    """Parsed education level requirement.

    Attributes:
        level: One of ``"high_school"``, ``"associate"``, ``"bachelor"``,
            ``"master"``, ``"mba"``, ``"phd"``.
        field_of_study: Subject area if mentioned (e.g. ``"computer science"``).
        is_required: ``True`` if the posting says "required"; ``False`` if
            "preferred", "desired", or similar.
        raw_text: Original matched substring.
    """

    level: str
    field_of_study: str = ""
    is_required: bool = True
    raw_text: str = ""


@dataclass
class CertificationRequirement:
    """A certification mentioned in the job posting.

    Attributes:
        name: Normalised certification name (lowercased).
        is_required: ``False`` if the posting marks it as preferred/nice-to-have.
        raw_text: Original matched substring.
    """

    name: str
    is_required: bool = False
    raw_text: str = ""


@dataclass
class ParsedRequirements:
    """Aggregated output of :class:`RequirementParser`.

    Attributes:
        experience: All experience requirements found.
        education: All education requirements found (usually 0–1).
        certifications: All certifications mentioned.
        required_text: Concatenated text of sections classified as required/
            must-have. Falls back to the full job description when no explicit
            required section is detected.
        preferred_text: Concatenated text of sections classified as preferred/
            nice-to-have. Empty string when no preferred section found.
        min_years_experience: The smallest ``min_years`` value across all
            experience requirements, or ``0`` if none were found.
        education_level: The highest education level mentioned (useful for
            quick comparisons).
    """

    experience: List[ExperienceRequirement] = field(default_factory=list)
    education: List[EducationRequirement] = field(default_factory=list)
    certifications: List[CertificationRequirement] = field(default_factory=list)
    required_text: str = ""
    preferred_text: str = ""

    @property
    def min_years_experience(self) -> int:
        """Return the minimum years of experience found, or 0."""
        if not self.experience:
            return 0
        return min(e.min_years for e in self.experience)

    @property
    def max_years_experience(self) -> int:
        """Return the maximum years of experience found, or 0."""
        if not self.experience:
            return 0
        return max(e.min_years for e in self.experience)

    @property
    def education_level(self) -> Optional[str]:
        """Return the highest education level required."""
        _RANK = {
            "high_school": 0,
            "associate": 1,
            "bachelor": 2,
            "master": 3,
            "mba": 3,
            "phd": 4,
        }
        if not self.education:
            return None
        return max(self.education, key=lambda e: _RANK.get(e.level, 0)).level

    def to_dict(self) -> dict:
        """Serialise to a JSON-compatible dictionary."""
        return {
            "experience": [
                {
                    "skill": e.skill,
                    "min_years": e.min_years,
                    "max_years": e.max_years,
                    "is_minimum": e.is_minimum,
                }
                for e in self.experience
            ],
            "education": [
                {
                    "level": e.level,
                    "field_of_study": e.field_of_study,
                    "is_required": e.is_required,
                }
                for e in self.education
            ],
            "certifications": [
                {"name": c.name, "is_required": c.is_required}
                for c in self.certifications
            ],
            "min_years_experience": self.min_years_experience,
            "max_years_experience": self.max_years_experience,
            "education_level": self.education_level,
            "required_text": self.required_text,
            "preferred_text": self.preferred_text,
        }


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class RequirementParser:
    """Extract structured requirements from raw job description text.

    All methods accept a plain-text string and return typed dataclasses.
    Regex patterns are compiled once on construction for efficiency.
    """

    # ------------------------------------------------------------------
    # Section-header patterns for required vs preferred classification
    # ------------------------------------------------------------------

    # Headers that introduce a "required / must-have" block.
    _REQUIRED_HEADER_RE = re.compile(
        r"^\s*(?:"
        r"requirements?|must[\s\-]?have|what you(?:'ll|'re going to)? need|"
        r"basic qualifications?|minimum qualifications?|"
        r"qualifications?(?:\s+required)?|"
        r"you(?:\s+(?:have|bring|must|will))|"
        r"required\s+(?:skills?|qualifications?|experience)|"
        r"(?:skills?|experience)\s+required|"
        r"what we(?:'re)?\s+looking\s+for"
        r")\s*:?\s*$",
        re.IGNORECASE,
    )

    # Headers that introduce a "preferred / nice-to-have" block.
    _PREFERRED_HEADER_RE = re.compile(
        r"^\s*(?:"
        r"preferred(?:\s+qualifications?)?|"
        r"nice[\s\-]?to[\s\-]?have|"
        r"bonus(?:\s+points?)?|"
        r"ideally|desired(?:\s+qualifications?)?|"
        r"additional\s+qualifications?|"
        r"advantageous|"
        r"what would be (?:great|nice|awesome)|"
        r"preferred\s+(?:skills?|experience)|"
        r"plus(?:\s+points?)?|"
        r"would be (?:great|nice|a plus)\s+if"
        r")\s*:?\s*$",
        re.IGNORECASE,
    )

    # ------------------------------------------------------------------
    # Experience patterns
    # Capture groups: (years_number, skill_phrase) OR (skill_phrase, years_number)
    # ------------------------------------------------------------------
    _EXPERIENCE_PATTERNS: List[tuple[str, str]] = [
        # "5+ years of Python experience" / "5+ years Python"
        # Skill capture stops at conjunction words (and/or/with) and sentence ends
        (
            r"(\d+)\s*\+?\s*(?:to\s*\d+\s*)?years?\s+(?:of\s+)?(?:professional\s+)?"
            r"(?:experience\s+(?:in|with)\s+)?([a-zA-Z][a-zA-Z0-9./+#\-]{0,25}(?:\s+[a-zA-Z0-9./+#\-]+){0,3}?)"
            r"(?=\s+(?:and|or|with|experience|,|$|\.)|\s*$)",
            "years_first",
        ),
        # "experience: 3+ years" (no skill)
        (
            r"(\d+)\s*\+?\s*years?\s+(?:of\s+)?(?:relevant\s+)?(?:professional\s+)?experience",
            "years_only",
        ),
        # "Python: 3+ years"
        (
            r"([a-zA-Z][a-zA-Z0-9 ./+#\-]{1,30})\s*:\s*(\d+)\s*\+?\s*years?",
            "skill_first",
        ),
    ]

    # ------------------------------------------------------------------
    # Education patterns
    # ------------------------------------------------------------------
    _EDU_LEVEL_MAP: dict = {
        "phd": "phd",
        "ph.d": "phd",
        "doctorate": "phd",
        "doctoral": "phd",
        "master": "master",
        "master's": "master",
        "masters": "master",
        "m.s.": "master",
        "m.s": "master",
        "ms ": "master",
        "mba": "mba",
        "m.b.a": "mba",
        "bachelor": "bachelor",
        "bachelor's": "bachelor",
        "bachelors": "bachelor",
        "b.s.": "bachelor",
        "b.s": "bachelor",
        "bs ": "bachelor",
        "b.a.": "bachelor",
        "undergraduate": "bachelor",
        "associate": "associate",
        "associate's": "associate",
        "high school": "high_school",
        "ged": "high_school",
    }

    _EDU_PATTERN = re.compile(
        r"(phd|ph\.d\.?|doctorate|doctoral|master'?s?|m\.s\.?|mba|m\.b\.a\.?|"
        r"bachelor'?s?|b\.s\.?|b\.a\.?|undergraduate|associate'?s?|"
        r"high\s+school|ged)"
        r"(?:'?s)?"
        r"(?:\s+(?:degree|of\s+science|of\s+arts))?"
        r"(?:\s+(?:in|of)\s+([a-zA-Z][a-zA-Z0-9 ,&/]{2,50}))?",
        re.IGNORECASE,
    )

    _PREFERRED_RE = re.compile(
        r"\b(preferred|desired|nice\s+to\s+have|plus|advantage)\b",
        re.IGNORECASE,
    )

    # ------------------------------------------------------------------
    # Certification patterns
    # ------------------------------------------------------------------
    _CERT_PATTERNS: List[str] = [
        r"aws\s+certified\s+[a-z\s\-]+(?:associate|professional|specialty|practitioner)?",
        r"aws\s+cloud\s+practitioner",
        r"aws\s+solutions\s+architect",
        r"google\s+cloud\s+(?:professional\s+)?[a-z\s]+(?:engineer|architect|developer)?",
        r"microsoft\s+certified\s*:?\s*[a-z\s]+(?:associate|expert|fundamentals)?",
        r"azure\s+(?:developer|administrator|architect|fundamentals)",
        r"certified\s+kubernetes\s+(?:administrator|application\s+developer)",
        r"\b(?:cka|ckad)\b",
        r"\bpmp\b",
        r"project\s+management\s+professional",
        r"certified\s+scrum\s+master",
        r"\bcsm\b",
        r"\bcissp\b",
        r"\bceh\b",
        r"comptia\s+(?:security\+|a\+|network\+|cloud\+|cysa\+)?",
        r"six\s+sigma\s+(?:green|black|yellow)\s+belt",
        r"\bitil\s+(?:v3|v4|4)?",
        r"salesforce\s+(?:certified\s+)?[a-z\s]+(?:developer|administrator|consultant)?",
    ]

    def __init__(self) -> None:
        self._experience_compiled = [
            (re.compile(p, re.IGNORECASE), variant)
            for p, variant in self._EXPERIENCE_PATTERNS
        ]
        self._cert_compiled = [
            re.compile(p, re.IGNORECASE) for p in self._CERT_PATTERNS
        ]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse(self, text: str) -> ParsedRequirements:
        """Run all parsers on *text* and return aggregated results.

        Also splits the text into ``required_text`` and ``preferred_text``
        blocks by detecting section headers.

        Args:
            text: Raw job description text.

        Returns:
            :class:`ParsedRequirements` instance with all parsed data.
        """
        if not text or not text.strip():
            return ParsedRequirements()

        required_text, preferred_text = self._split_sections(text)

        return ParsedRequirements(
            experience=self.parse_experience(text),
            education=self.parse_education(text),
            certifications=self.parse_certifications(text),
            required_text=required_text,
            preferred_text=preferred_text,
        )

    def _split_sections(self, text: str) -> tuple[str, str]:
        """Split *text* into required and preferred section text.

        Algorithm:
        1. Walk lines; detect section headers using the class regexes.
        2. Assign each line to a "required" bucket, "preferred" bucket,
           or "neutral" (neither).
        3. If NO required header was found, the entire text is required
           (safe fallback — existing behaviour).
        4. If NO preferred header was found, preferred_text is "".

        Returns:
            ``(required_text, preferred_text)`` as plain strings.
        """
        lines = text.splitlines()
        # State: "neutral", "required", or "preferred"
        state = "neutral"
        required_chunks: list[str] = []
        preferred_chunks: list[str] = []
        found_required_header = False
        found_preferred_header = False

        for line in lines:
            # Detect header transitions (only match header-looking lines,
            # i.e. short lines or lines ending with colon)
            stripped = line.strip()
            if self._PREFERRED_HEADER_RE.match(stripped):
                state = "preferred"
                found_preferred_header = True
                continue  # skip the header line itself
            if self._REQUIRED_HEADER_RE.match(stripped):
                state = "required"
                found_required_header = True
                continue  # skip the header line itself

            # Accumulate
            if state == "required":
                required_chunks.append(line)
            elif state == "preferred":
                preferred_chunks.append(line)
            else:
                # Neutral (before any header) — treat as required
                required_chunks.append(line)

        required_text = "\n".join(required_chunks).strip()
        preferred_text = "\n".join(preferred_chunks).strip()

        # Fallback: if no explicit required header found, use full text
        if not found_required_header:
            required_text = text.strip()

        logger.debug(
            "_split_sections: found_required=%s, found_preferred=%s, "
            "req_len=%d, pref_len=%d",
            found_required_header, found_preferred_header,
            len(required_text), len(preferred_text),
        )
        return required_text, preferred_text

    # Negative-context prefixes: phrases that indicate the skill is NOT required.
    # Only fires when there is NO sentence boundary (.!?\n) between the negative
    # word and the current match position (i.e. same sentence).
    _NEGATIVE_PREFIXES = re.compile(
        r"\b(no|not|without|don'?t need|doesn'?t require|waive|waiving)\b"
        r"[^.!?\n]{0,50}$",  # same sentence, up to 50 chars before the match
        re.IGNORECASE,
    )

    def parse_experience(self, text: str) -> List[ExperienceRequirement]:
        """Extract years-of-experience requirements.

        Skips matches preceded by negative context (e.g. "No PHP experience
        required") within a 60-character look-behind window.

        Args:
            text: Raw job description.

        Returns:
            List of :class:`ExperienceRequirement` objects.
        """
        results: List[ExperienceRequirement] = []
        used_spans: List[tuple[int, int]] = []

        for compiled, variant in self._experience_compiled:
            for match in compiled.finditer(text):
                start, end = match.start(), match.end()
                if any(s <= start and end <= e for s, e in used_spans):
                    continue

                # Skip negative-context mentions
                prefix = text[max(0, start - 60): start]
                if self._NEGATIVE_PREFIXES.search(prefix):
                    logger.debug("Skipping negative-context experience: %r", match.group(0))
                    continue

                used_spans.append((start, end))

                raw = match.group(0).strip()
                is_min = "+" in raw

                try:
                    if variant == "years_first":
                        min_years = int(match.group(1))
                        skill = self._clean_skill(match.group(2)) if len(match.groups()) >= 2 else ""
                    elif variant == "years_only":
                        min_years = int(match.group(1))
                        skill = ""
                    else:  # skill_first
                        skill = self._clean_skill(match.group(1))
                        min_years = int(match.group(2))

                    # Reject implausibly large values or empty skills after cleaning
                    if min_years > 25:
                        continue
                    if variant == "years_first" and skill == "":
                        continue

                    results.append(
                        ExperienceRequirement(
                            skill=skill,
                            min_years=min_years,
                            is_minimum=is_min,
                            raw_text=raw,
                        )
                    )
                except (IndexError, ValueError):
                    logger.debug("Could not parse experience from: %r", raw)

        logger.debug("Found %d experience requirements.", len(results))
        return results

    def parse_experience_requirements(self, text: str) -> List[dict]:
        """Public alias matching the spec API – returns plain dicts.

        Converts :class:`ExperienceRequirement` objects into the simple
        dictionary format used by the test suite and CLI callers::

            [{"skill": "Python", "years": 5, "is_minimum": True}, ...]

        Skill names are title-cased to match natural English capitalisation.

        Args:
            text: Raw job description.

        Returns:
            List of dictionaries with keys ``skill``, ``years``,
            ``is_minimum``, and ``raw_text``.
        """
        return [
            {
                "skill": exp.skill.title() if exp.skill else "",
                "years": exp.min_years,
                "is_minimum": exp.is_minimum,
                "raw_text": exp.raw_text,
            }
            for exp in self.parse_experience(text)
        ]

    def parse_education(self, text: str) -> List[EducationRequirement]:
        """Extract education level requirements.

        Args:
            text: Raw job description.

        Returns:
            List of :class:`EducationRequirement` objects.
        """
        results: List[EducationRequirement] = []
        seen_levels: set = set()

        for match in self._EDU_PATTERN.finditer(text):
            level_raw = match.group(1).lower().rstrip("'s").strip()

            # Map variant → canonical level
            canonical = None
            for key, level in self._EDU_LEVEL_MAP.items():
                if level_raw.startswith(key.lower()):
                    canonical = level
                    break

            if not canonical:
                continue
            if canonical in seen_levels:
                continue
            seen_levels.add(canonical)

            field_group = match.group(2)
            field_of_study = self._clean_skill(field_group) if field_group else ""

            # Check surrounding 80 chars for "preferred" language
            context_start = max(0, match.end())
            context = text[context_start: context_start + 80]
            is_required = not bool(self._PREFERRED_RE.search(context))

            results.append(
                EducationRequirement(
                    level=canonical,
                    field_of_study=field_of_study,
                    is_required=is_required,
                    raw_text=match.group(0).strip(),
                )
            )

        logger.debug("Found %d education requirements.", len(results))
        return results

    def parse_certifications(self, text: str) -> List[CertificationRequirement]:
        """Extract certification requirements.

        Args:
            text: Raw job description.

        Returns:
            List of :class:`CertificationRequirement` objects.
        """
        results: List[CertificationRequirement] = []
        seen: set = set()

        for pattern in self._cert_compiled:
            for match in pattern.finditer(text):
                name = match.group(0).strip().lower()
                # Normalise whitespace
                name = re.sub(r"\s+", " ", name)
                if name in seen:
                    continue
                seen.add(name)

                # Check 80 chars either side of the match for a "preferred" qualifier
                ctx_start = max(0, match.start() - 40)
                ctx_end = min(len(text), match.end() + 80)
                context = text[ctx_start:ctx_end]
                is_preferred = bool(self._PREFERRED_RE.search(context))

                results.append(
                    CertificationRequirement(
                        name=name,
                        is_required=not is_preferred,
                        raw_text=match.group(0).strip(),
                    )
                )

        logger.debug("Found %d certifications.", len(results))
        return results

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_skill(text: str) -> str:
        """Normalise a skill phrase extracted from a regex capture group.

        Args:
            text: Raw captured text.

        Returns:
            Lowercased, stripped, punctuation-trimmed skill string.
        """
        cleaned = text.strip().lower()
        # Strip trailing punctuation characters
        cleaned = cleaned.rstrip(".,;:!?\"'")
        # Strip trailing noise words that bleed in from the surrounding sentence
        _noise_suffixes = (
            " experience", " skills", " knowledge", " expertise",
            " required", " preferred", " or", " and", " requirements",
        )
        for suffix in _noise_suffixes:
            if cleaned.endswith(suffix):
                cleaned = cleaned[: -len(suffix)].strip()
        cleaned = cleaned.rstrip(".,;:!?\"'")
        # If what remains is a structural label ("requirements", "qualifications"),
        # discard it entirely.
        _discard = {"requirements", "qualifications", "responsibilities", "overview"}
        if cleaned in _discard:
            return ""
        return cleaned
