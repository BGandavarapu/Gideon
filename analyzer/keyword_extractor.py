"""
Keyword extraction module for job descriptions.

Combines two complementary strategies:

1. **Taxonomy matching** – case-insensitive phrase lookup against
   ``skills_taxonomy.yaml``.  Longer phrases are matched first (greedy)
   so "machine learning" is preferred over a bare "learning" hit.

2. **spaCy NER** – entity labels ``ORG``, ``PRODUCT``, and ``GPE`` are
   captured as potential technical terms when they are not already
   covered by the taxonomy.  This picks up newly coined tools and
   frameworks that have not yet been added to the taxonomy.

The two result sets are merged and deduplicated before being returned as
a list of :class:`ExtractedKeyword` objects.

Performance note
----------------
The spaCy model (``en_core_web_sm``) is loaded once per
:class:`KeywordExtractor` instance.  Callers should create a single
instance and reuse it across many descriptions rather than constructing
it per-call.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import spacy
import yaml
from spacy.language import Language

logger = logging.getLogger(__name__)

_TAXONOMY_PATH = Path(__file__).resolve().parent / "skills_taxonomy.yaml"
_SPACY_MODEL = "en_core_web_sm"

# Negative-context pattern: "No PHP", "not required", "without Java", etc.
# Matches when one of these words appears within 40 chars BEFORE the skill,
# with no sentence boundary in between.
_NEGATION_RE = re.compile(
    r"\b(no|not|without|don'?t need|doesn'?t require)\b[^.!?\n]{0,40}$",
    re.IGNORECASE,
)

# spaCy entity labels that can represent genuine technical skills/tools.
# GPE (geo-political entity) and DATE/MONEY are deliberately excluded.
_TECH_NER_LABELS = {"ORG", "PRODUCT", "GPE"}

# Allowlist of entity labels treated as definite skill signals
_SKILL_ENTITY_LABELS = {"PRODUCT", "WORK_OF_ART", "LAW", "LANGUAGE"}

# Minimum character length for an NER-derived keyword (filters out noise)
_MIN_NER_LENGTH = 3

# Known tech tool names that may be classified as ORG but are genuine skills
_TECH_ORG_RE = re.compile(
    r"^(AWS|GCP|Azure|Docker|Kubernetes|Kafka|Redis|Linux|"
    r"GitHub|GitLab|Jenkins|Terraform|Ansible|Spark|Hadoop|"
    r"Airflow|Databricks|Snowflake|MongoDB|PostgreSQL|MySQL|"
    r"ElasticSearch|RabbitMQ|Celery|Nginx|Apache|Heroku|Vercel|"
    r"CircleCI|TravisCI|Datadog|Splunk|Grafana|Prometheus|"
    r"Kubernetes|K8s|Helm|Istio|Vault|Consul)$",
    re.IGNORECASE,
)
# Short ALL-CAPS acronyms that are likely tech tools when labelled ORG
_TECH_ACRONYM_RE = re.compile(r"^[A-Z]{2,6}$")

# Location / noise terms that should never appear as skills
_NOISE_LOCATIONS: frozenset = frozenset({
    "san francisco", "new york", "seattle", "austin", "boston",
    "chicago", "los angeles", "denver", "atlanta", "remote",
    "united states", "usa", "california", "texas", "new york city",
    "nyc", "sf", "bay area", "silicon valley", "new jersey", "virginia",
    "washington", "oregon", "florida", "georgia", "colorado", "ohio",
    "united kingdom", "uk", "canada", "india", "australia", "europe",
    "worldwide", "global", "anywhere",
})

# Regex patterns for year/salary/percentage noise terms
_NOISE_PATTERNS: tuple = (
    re.compile(r"^\d+\+?\s*(?:years?|yrs?)\b", re.IGNORECASE),  # "5+ years"
    re.compile(r"^\d{4}$"),                                       # "2019", "2024"
    re.compile(r"^\$[\d,]+"),                                     # "$120,000"
    re.compile(r"^(?:inc|llc|ltd|corp|co\.)$", re.IGNORECASE),  # company suffixes
    re.compile(r"^\d+%$"),                                        # "25%"
    re.compile(r"^(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b", re.IGNORECASE),
    re.compile(r"^https?://", re.IGNORECASE),                     # URLs
    re.compile(r"^www\.", re.IGNORECASE),                         # www. links
)


@dataclass
class ExtractedKeyword:
    """A single keyword extracted from a job description.

    Attributes:
        text: Normalised (lowercased) keyword text.
        category: Taxonomy category, or ``"ner_entity"`` for NER-derived terms.
        confidence: Float 0.0–1.0 indicating extraction confidence.
            Taxonomy hits always receive ``1.0``; NER hits receive ``0.7``.
        context: The sentence (or sentence fragment) in which the keyword
            appeared, useful for downstream prompts to Gemini.
        original_text: The raw un-normalised text as it appeared in the source.
    """

    text: str
    category: str
    confidence: float
    context: str = ""
    original_text: str = ""

    def __post_init__(self) -> None:
        if not self.original_text:
            self.original_text = self.text

    def to_dict(self) -> dict:
        """Return a plain dictionary suitable for JSON serialisation."""
        return {
            "text": self.text,
            "category": self.category,
            "confidence": self.confidence,
            "context": self.context,
            "original_text": self.original_text,
        }


class KeywordExtractor:
    """Extract and categorise keywords from job descriptions.

    Args:
        taxonomy_path: Path to the YAML taxonomy file.  Defaults to the
            bundled ``skills_taxonomy.yaml``.
        spacy_model: Name of the spaCy model to load.

    Attributes:
        taxonomy: Mapping of category → list of skill phrases.
        _phrase_index: Pre-built mapping of lowercased phrase → category,
            sorted longest-first for greedy matching.
    """

    def __init__(
        self,
        taxonomy_path: Path = _TAXONOMY_PATH,
        spacy_model: str = _SPACY_MODEL,
    ) -> None:
        self.taxonomy: Dict[str, List[str]] = self._load_taxonomy(taxonomy_path)
        self._phrase_index: List[tuple[str, str]] = self._build_phrase_index()
        try:
            self._nlp: Language = spacy.load(spacy_model)
            logger.debug("Loaded spaCy model %r.", spacy_model)
        except OSError:
            logger.warning(
                "spaCy model %r not found – NER will be skipped. "
                "Run: python -m spacy download %s",
                spacy_model,
                spacy_model,
            )
            self._nlp = None  # type: ignore[assignment]

    # ------------------------------------------------------------------
    # Public API — split-bucket extraction (required vs preferred)
    # ------------------------------------------------------------------

    def extract(self, job_description: str) -> dict:
        """Extract keywords split into required and preferred buckets.

        Uses :class:`~analyzer.requirement_parser.RequirementParser` to
        detect which parts of the description are "required" vs "preferred",
        then runs taxonomy matching and (filtered) NER on each section
        independently.

        Args:
            job_description: Raw job posting text.

        Returns:
            Dictionary with keys:
            - ``required_skills``  – list of str
            - ``preferred_skills`` – list of str (non-overlapping with required)
            - ``experience_required`` – min years int or None
            - ``education_required``  – education level str or None
            - ``certifications``      – list of str
        """
        from analyzer.requirement_parser import RequirementParser  # avoid circular at module level

        if not job_description or not job_description.strip():
            return {
                "required_skills": [],
                "preferred_skills": [],
                "experience_required": None,
                "education_required": None,
                "certifications": [],
            }

        parser = RequirementParser()
        parsed = parser.parse(job_description)

        req_text  = parsed.required_text  or job_description
        pref_text = parsed.preferred_text or ""

        # Taxonomy matching per section
        required_skills  = self._extract_taxonomy_skills(req_text)
        preferred_skills = self._extract_taxonomy_skills(pref_text) if pref_text else []

        # Filtered NER on required section only
        ner_skills = self._extract_ner_entities_filtered(req_text)
        req_set = set(required_skills)
        pref_set = set(preferred_skills)
        for skill in ner_skills:
            if skill not in req_set and skill not in pref_set:
                required_skills.append(skill)
                req_set.add(skill)

        # Preferred must not duplicate required
        preferred_skills = [s for s in preferred_skills if s not in req_set]

        # Remove noise from both buckets
        required_skills  = [s for s in required_skills  if not self._is_noise(s)]
        preferred_skills = [s for s in preferred_skills if not self._is_noise(s)]

        cert_names = [c.name for c in parsed.certifications]

        logger.debug(
            "extract(): %d required, %d preferred, %d certs",
            len(required_skills), len(preferred_skills), len(cert_names),
        )
        return {
            "required_skills":    required_skills,
            "preferred_skills":   preferred_skills,
            "experience_required": parsed.min_years_experience or None,
            "education_required":  parsed.education_level,
            "certifications":      cert_names,
        }

    # ------------------------------------------------------------------
    # Public API — original methods (unchanged)
    # ------------------------------------------------------------------

    def extract_keywords(self, job_description: str) -> List[ExtractedKeyword]:
        """Extract all relevant keywords from a job description.

        Args:
            job_description: Raw job posting text (plain-text, not HTML).

        Returns:
            Deduplicated list of :class:`ExtractedKeyword` objects, sorted by
            confidence descending then alphabetically by text.
        """
        if not job_description or not job_description.strip():
            return []

        text = job_description.strip()
        sentences = self._split_sentences(text)

        taxonomy_hits = self._match_taxonomy(text, sentences)
        ner_hits = self._extract_ner_entities(text, taxonomy_hits)

        merged = self._deduplicate(taxonomy_hits + ner_hits)
        merged.sort(key=lambda kw: (-kw.confidence, kw.text))
        logger.debug("Extracted %d keywords from description.", len(merged))
        return merged

    def extract_by_category(
        self, job_description: str
    ) -> Dict[str, List[str]]:
        """Return keywords grouped by their taxonomy category.

        Args:
            job_description: Raw job posting text.

        Returns:
            Dictionary mapping category names to lists of keyword strings.
            NER-only terms appear under ``"ner_entity"``.
        """
        keywords = self.extract_keywords(job_description)
        result: Dict[str, List[str]] = {}
        for kw in keywords:
            result.setdefault(kw.category, []).append(kw.text)
        return result

    def get_technical_skills(self, job_description: str) -> List[str]:
        """Return a flat list of all technical skill names found.

        Excludes ``soft_skills`` and ``education_keywords`` categories.

        Args:
            job_description: Raw job posting text.

        Returns:
            Deduplicated list of lowercased technical skill strings.
        """
        non_technical = {"soft_skills", "education_keywords"}
        keywords = self.extract_keywords(job_description)
        seen: set = set()
        result: List[str] = []
        for kw in keywords:
            if kw.category not in non_technical and kw.text not in seen:
                result.append(kw.text)
                seen.add(kw.text)
        return result

    # ------------------------------------------------------------------
    # Taxonomy loading + phrase index
    # ------------------------------------------------------------------

    @staticmethod
    def _load_taxonomy(path: Path) -> Dict[str, List[str]]:
        """Load the YAML taxonomy file.

        Args:
            path: Filesystem path to the YAML file.

        Returns:
            Dictionary mapping category names to skill phrase lists.
        """
        if not path.exists():
            logger.warning("Taxonomy file not found at %s – using empty taxonomy.", path)
            return {}
        try:
            with path.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
            logger.debug("Loaded %d taxonomy categories from %s.", len(raw), path)
            result: Dict[str, List[str]] = {}
            for cat, skills in raw.items():
                if isinstance(skills, list):
                    result[cat] = [str(s).lower() for s in skills]
                elif isinstance(skills, dict):
                    # Nested structure (e.g. finance: {skills: [...], tools: [...]})
                    # Flatten all sub-lists into one list for the category
                    flat: List[str] = []
                    for sub_list in skills.values():
                        if isinstance(sub_list, list):
                            flat.extend(str(s).lower() for s in sub_list)
                    result[cat] = flat
            return result
        except yaml.YAMLError as exc:
            logger.error("Failed to parse taxonomy YAML: %s – using empty taxonomy.", exc)
            return {}

    def _build_phrase_index(self) -> List[tuple[str, str]]:
        """Build a flat (phrase, category) list sorted longest phrase first.

        Sorting longest-first ensures greedy matching prefers
        "machine learning" over "machine" or "learning".

        Returns:
            Sorted list of ``(phrase, category)`` tuples.
        """
        pairs: List[tuple[str, str]] = []
        for category, phrases in self.taxonomy.items():
            for phrase in phrases:
                pairs.append((phrase.lower(), category))
        pairs.sort(key=lambda x: len(x[0]), reverse=True)
        return pairs

    # ------------------------------------------------------------------
    # Taxonomy matching
    # ------------------------------------------------------------------

    def _match_taxonomy(
        self, text: str, sentences: List[str]
    ) -> List[ExtractedKeyword]:
        """Find all taxonomy phrases in the normalised text.

        Uses whole-word boundary matching (``\\b``) so "c" does not match
        "catch" and "go" does not match "going".

        Args:
            text: Full normalised description text.
            sentences: Pre-split sentences for context extraction.

        Returns:
            List of taxonomy-matched :class:`ExtractedKeyword` objects.
        """
        text_lower = text.lower()
        found: List[ExtractedKeyword] = []
        matched_spans: List[tuple[int, int]] = []

        for phrase, category in self._phrase_index:
            # Build a word-boundary pattern; escape special regex chars
            escaped = re.escape(phrase)
            pattern = rf"\b{escaped}\b"
            for match in re.finditer(pattern, text_lower):
                start, end = match.start(), match.end()
                # Skip if this span is already covered by a longer match
                if any(s <= start and end <= e for s, e in matched_spans):
                    continue
                # Skip negated mentions: "No PHP", "without Java", etc.
                prefix = text_lower[max(0, start - 50): start]
                if _NEGATION_RE.search(prefix):
                    logger.debug("Skipping negated keyword %r at position %d", phrase, start)
                    continue
                matched_spans.append((start, end))
                context = self._find_sentence(phrase, sentences)
                found.append(
                    ExtractedKeyword(
                        text=phrase,
                        category=category,
                        confidence=1.0,
                        context=context,
                        original_text=text[start:end],
                    )
                )

        return found

    # ------------------------------------------------------------------
    # spaCy NER
    # ------------------------------------------------------------------

    def _extract_ner_entities(
        self,
        text: str,
        existing: List[ExtractedKeyword],
    ) -> List[ExtractedKeyword]:
        """Run spaCy NER and return entities not already in *existing*.

        Only entities with labels in :data:`_TECH_NER_LABELS` and length
        >= :data:`_MIN_NER_LENGTH` are considered.

        Args:
            text: Raw job description text.
            existing: Already-found taxonomy keywords (used to filter dupes).

        Returns:
            List of NER-derived :class:`ExtractedKeyword` objects.
        """
        if self._nlp is None:
            return []

        existing_texts = {kw.text for kw in existing}
        ner_hits: List[ExtractedKeyword] = []
        seen: set = set()

        try:
            doc = self._nlp(text[:100_000])  # Truncate for model limits
        except Exception as exc:
            logger.warning("spaCy NER failed: %s", exc)
            return []

        for ent in doc.ents:
            if ent.label_ not in _TECH_NER_LABELS:
                continue
            # Strip zero-width and other invisible Unicode characters that can
            # cause cp1252 encoding errors on Windows consoles/databases.
            raw_text = "".join(c for c in ent.text if c.isprintable() and ord(c) < 0x2000)
            normalised = raw_text.strip().lower()
            if len(normalised) < _MIN_NER_LENGTH:
                continue
            if normalised in existing_texts or normalised in seen:
                continue
            # Skip plain English words that are not likely tech terms
            if not self._looks_like_tech_term(normalised):
                continue
            seen.add(normalised)
            sentence = ent.sent.text.strip() if ent.sent else ""
            ner_hits.append(
                ExtractedKeyword(
                    text=normalised,
                    category="ner_entity",
                    confidence=0.7,
                    context=sentence,
                    original_text=ent.text,
                )
            )

        return ner_hits

    # ------------------------------------------------------------------
    # Helper: taxonomy-only extraction (returns plain strings)
    # ------------------------------------------------------------------

    def _extract_taxonomy_skills(self, text: str) -> List[str]:
        """Return a deduplicated list of taxonomy skill strings found in *text*.

        Runs the same greedy phrase-index matching as :meth:`_match_taxonomy`
        but returns plain lowercased strings rather than :class:`ExtractedKeyword`
        objects, which is more convenient for the split-bucket ``extract()`` API.

        Args:
            text: Plain-text section to scan.

        Returns:
            Deduplicated list of matched skill strings.
        """
        if not text or not text.strip():
            return []
        sentences = self._split_sentences(text)
        hits = self._match_taxonomy(text, sentences)
        # Exclude soft skills and education keywords from skill buckets
        _non_skill = {"soft_skills", "education_keywords"}
        seen: set = set()
        result: List[str] = []
        for kw in hits:
            if kw.category in _non_skill:
                continue
            if kw.text not in seen:
                result.append(kw.text)
                seen.add(kw.text)
        return result

    # ------------------------------------------------------------------
    # Helper: filtered NER extraction (returns plain strings)
    # ------------------------------------------------------------------

    def _extract_ner_entities_filtered(self, text: str) -> List[str]:
        """Run spaCy NER with strict noise filtering.

        Only keeps entities whose label is in the skill allowlist, or ORG
        entities that match known tech tool patterns.  Locations, dates,
        money, and people are always excluded.

        Args:
            text: Text to analyse (usually the required section only).

        Returns:
            Deduplicated list of cleaned skill strings.
        """
        if self._nlp is None:
            return []

        try:
            doc = self._nlp(text[:100_000])
        except Exception as exc:
            logger.warning("spaCy NER failed: %s", exc)
            return []

        seen: set = set()
        result: List[str] = []

        for ent in doc.ents:
            label = ent.label_

            # Only accept allowlisted labels
            if label not in _SKILL_ENTITY_LABELS and label != "ORG":
                continue

            # Strip invisible/weird Unicode
            raw = "".join(
                c for c in ent.text if c.isprintable() and ord(c) < 0x2000
            )
            cleaned = raw.strip()
            if len(cleaned) < _MIN_NER_LENGTH or len(cleaned) > 50:
                continue

            # ORG secondary filter: must look like a tech tool, not a company
            if label == "ORG":
                if not (
                    _TECH_ORG_RE.match(cleaned)
                    or _TECH_ACRONYM_RE.match(cleaned)
                ):
                    continue

            normalised = cleaned.lower()

            if self._is_noise(normalised):
                continue
            if not self._looks_like_tech_term(normalised):
                continue
            if normalised in seen:
                continue

            seen.add(normalised)
            result.append(normalised)

        return result

    # ------------------------------------------------------------------
    # Helper: noise filter
    # ------------------------------------------------------------------

    @staticmethod
    def _is_noise(term: str) -> bool:
        """Return ``True`` if *term* is a noise term rather than a real skill.

        Filters out locations, year patterns, salary figures, and other
        non-skill strings that NER or taxonomy matching may surface.

        Args:
            term: Lowercased, stripped candidate skill string.

        Returns:
            ``True`` when the term should be discarded.
        """
        t = term.lower().strip()
        if len(t) <= 1:
            return True
        if t in _NOISE_LOCATIONS:
            return True
        for pattern in _NOISE_PATTERNS:
            if pattern.match(t):
                return True
        return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _looks_like_tech_term(text: str) -> bool:
        """Heuristically decide whether *text* resembles a technology name.

        Rejects all-lowercase single common-word strings while allowing
        acronyms, product names with digits, and compound terms.

        Args:
            text: Normalised (lowercased) candidate term.

        Returns:
            ``True`` if the term looks technology-related.
        """
        # Reject trivially short tokens
        if len(text) < 2:
            return False
        # Keep if it contains a digit (e.g. "python3", "h2o")
        if any(c.isdigit() for c in text):
            return True
        # Keep if it is an acronym (2-6 uppercase letters in original = all same case)
        if text.isupper() and 2 <= len(text) <= 6:
            return True
        # Keep if it contains a dot or hyphen (e.g. "node.js", "c++")
        if "." in text or "+" in text or "-" in text:
            return True
        # Reject very common English words that NER mis-tags
        _stopwords = {
            "the", "and", "for", "with", "your", "our", "their", "have",
            "has", "been", "will", "from", "this", "that", "these",
            "those", "team", "role", "work", "join", "help", "build",
            "new", "use", "using", "based", "strong", "good",
        }
        if text in _stopwords:
            return False
        return True

    @staticmethod
    def _split_sentences(text: str) -> List[str]:
        """Split text into sentences using simple punctuation rules.

        Args:
            text: Plain text string.

        Returns:
            List of non-empty sentence strings.
        """
        sentences = re.split(r"(?<=[.!?])\s+", text)
        return [s.strip() for s in sentences if s.strip()]

    @staticmethod
    def _find_sentence(keyword: str, sentences: List[str]) -> str:
        """Return the first sentence containing *keyword*.

        Args:
            keyword: Lowercased keyword to search for.
            sentences: List of candidate sentences.

        Returns:
            Matching sentence, or empty string if none found.
        """
        keyword_lower = keyword.lower()
        for sentence in sentences:
            if keyword_lower in sentence.lower():
                return sentence[:200]  # Cap context length
        return ""

    @staticmethod
    def _deduplicate(keywords: List[ExtractedKeyword]) -> List[ExtractedKeyword]:
        """Remove duplicate keywords keeping the highest-confidence copy.

        Args:
            keywords: Possibly-duplicated keyword list.

        Returns:
            Deduplicated list.
        """
        seen: Dict[str, ExtractedKeyword] = {}
        for kw in keywords:
            if kw.text not in seen or kw.confidence > seen[kw.text].confidence:
                seen[kw.text] = kw
        return list(seen.values())
