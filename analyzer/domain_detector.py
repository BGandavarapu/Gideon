"""DomainDetector — classify a job posting or resume into one of the supported
job domains.

Two-stage detection:

1. **Heuristic** — keyword scoring with word-boundary matching.  Title keywords
   are weighted 3× over body-text keywords.  Fast, free, no API calls.
2. **NVIDIA NIM** — when the heuristic has low confidence (< 0.5) or returns
   ``"other"``, the text is sent to NVIDIA NIM for semantic classification.
   Returns up to 3 domains (multi-domain support).

Supported domains are exported as :data:`DOMAINS` so other modules can import
the canonical list from one place::

    from analyzer.domain_detector import DOMAINS, DomainDetector

    result = DomainDetector().detect_from_text(description, job_title=title)
    print(result['domain'])          # e.g. 'software_engineering'
    print(result['domains'])         # e.g. ['software_engineering', 'ai_ml']
    print(result['confidence'])      # e.g. 0.82
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_NVIDIA_MODEL_ID = "nvidia/llama-3.3-nemotron-super-49b-v1"
_NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
_NIM_CONFIDENCE_THRESHOLD = 0.5

# ---------------------------------------------------------------------------
# Domain registry
# ---------------------------------------------------------------------------

DOMAINS: Dict[str, str] = {
    "software_engineering": "Software Engineering",
    "ai_ml":                "AI / Machine Learning",
    "product_management":   "Product Management",
    "marketing":            "Marketing",
    "data_analytics":       "Data & Analytics",
    "design":               "Design (UX/UI)",
    "finance":              "Finance & Accounting",
    "sales":                "Sales",
    "operations":           "Operations",
    "other":                "Other",
}

# ---------------------------------------------------------------------------
# Keyword signals per domain
# ---------------------------------------------------------------------------

DOMAIN_KEYWORDS: Dict[str, List[str]] = {
    "software_engineering": [
        # Job title variants
        "software engineer", "backend", "frontend", "front-end", "back-end",
        "full stack", "fullstack", "full-stack", "web developer",
        "mobile developer", "ios developer", "android developer",
        "ios", "android", "devops", "platform engineer", "site reliability",
        "sre", "infrastructure engineer", "cloud engineer",
        "software development", "software developer",
        "python developer", "react developer", "java developer",
        "node developer", "golang", "go developer", "c++ developer",
        "solutions architect", "cloud architect", "api engineer",
        "flutter developer", "ruby developer", "scala developer",
        "typescript developer", "javascript developer",
        # Distinctive technologies
        "kubernetes", "microservices", "ci/cd", "docker",
        "terraform", "rest api", "graphql", "github actions", "jenkins",
    ],
    "ai_ml": [
        # Job title variants
        "machine learning", "deep learning", "artificial intelligence",
        "data scientist", "nlp", "natural language processing",
        "computer vision", "large language model", "llm",
        "neural network", "research scientist", "ml engineer",
        "ai engineer", "reinforcement learning", "generative ai",
        "foundation model", "transformer",
        "ml researcher", "ai researcher", "prompt engineer",
        "nlp engineer", "computer vision engineer", "ai scientist",
        # Distinctive technologies
        "pytorch", "tensorflow", "hugging face", "langchain",
        "scikit-learn", "mlflow", "model training", "fine-tuning", "embeddings",
    ],
    "product_management": [
        # Job title variants
        "product manager", "product owner", "program manager",
        "product lead", "head of product", "vp product",
        "director of product", "associate product manager", "apm",
        "technical product manager", "tpm", "product management",
        "product roadmap", "product strategy",
        "group product manager", "senior product manager",
        "product strategist", "digital product manager", "product analyst",
        # Distinctive terms
        "user stories", "okrs", "a/b testing",
        "go-to-market", "customer discovery", "agile", "sprint", "confluence",
    ],
    "marketing": [
        # Job title variants
        "marketing manager", "growth manager", "content marketer",
        "seo specialist", "digital marketing", "brand manager",
        "marketing director", "demand generation", "performance marketing",
        "social media manager", "email marketing", "marketing analyst",
        "growth hacker", "marketing coordinator", "content marketing",
        "seo", "sem", "ppc",
        "digital strategist", "content writer", "copywriter",
        "marketing specialist", "growth lead", "brand strategist",
        "media buyer", "affiliate marketing", "influencer marketing",
        # Distinctive technologies
        "hubspot", "google analytics", "google ads",
        "facebook ads", "mailchimp", "ahrefs",
        "semrush", "wordpress", "klaviyo",
    ],
    "data_analytics": [
        # Job title variants
        "data analyst", "business analyst", "analytics engineer",
        "bi developer", "business intelligence", "data engineer",
        "reporting analyst", "insights analyst", "revenue operations",
        "analytics", "data analysis", "data visualization",
        "business intelligence analyst",
        "sql analyst", "dashboard developer",
        "data warehouse", "reporting engineer", "analytics manager",
        # Distinctive technologies
        "tableau", "power bi", "looker", "dbt", "snowflake",
        "redshift", "databricks", "apache spark", "airflow", "etl pipeline",
    ],
    "design": [
        # Job title variants
        "ux designer", "ui designer", "product designer",
        "graphic designer", "visual designer", "interaction designer",
        "design lead", "head of design", "ux researcher",
        "motion designer", "brand designer", "user experience",
        "user interface", "figma", "sketch",
        "ux/ui designer", "product design", "design systems",
        "creative director", "design technologist", "accessibility designer",
        # Distinctive technologies / methods
        "adobe xd", "invision", "prototyping", "wireframing",
        "usability testing", "design thinking",
    ],
    "finance": [
        # Job title variants
        "financial analyst", "investment analyst", "fp&a",
        "accountant", "controller", "cfo", "finance manager",
        "investment banking", "private equity", "venture capital",
        "portfolio manager", "risk analyst", "treasury",
        "financial modeling", "financial planning",
        "finance analyst", "accounting manager",
        "tax analyst", "audit manager", "equity research", "credit analyst",
        # Distinctive technologies / terms
        "dcf", "bloomberg", "quickbooks",
        "gaap", "ifrs", "valuation", "financial statements",
    ],
    "sales": [
        # Job title variants
        "sales representative", "account executive", "ae",
        "account manager", "business development", "bdr", "sdr",
        "sales manager", "vp sales", "customer success", "csm",
        "solution engineer", "sales engineer", "sales development",
        "enterprise sales", "saas sales",
        "sales specialist", "inside sales", "outside sales",
        "relationship manager", "territory manager", "sales consultant",
        "client executive", "revenue manager",
        # Distinctive technologies / terms
        "salesforce", "hubspot crm", "outreach", "salesloft",
        "zoominfo", "gong", "quota", "pipeline management",
    ],
    "operations": [
        # Job title variants
        "operations manager", "operations analyst", "chief of staff",
        "project manager", "supply chain",
        "logistics manager", "process improvement", "strategy ops",
        "revenue operations", "revops", "operations coordinator",
        "office manager", "business operations",
        "operations lead", "ops manager", "process manager",
        "strategy consultant", "implementation manager",
        "solutions manager", "head of operations",
        # Distinctive technologies / terms
        "six sigma", "lean", "asana", "jira",
        "okr", "kpi", "vendor management", "cross-functional",
    ],
}

# Weight multiplier applied to job-title keyword matches vs body text
_TITLE_WEIGHT = 3


# ---------------------------------------------------------------------------
# DomainDetector
# ---------------------------------------------------------------------------


class DomainDetector:
    """Classify a job posting or resume into one of :data:`DOMAINS`.

    All methods never raise — on any error they return the ``"other"`` domain
    with zero confidence.
    """

    def detect_from_text(
        self,
        text: str,
        job_title: str = "",
    ) -> Dict[str, Any]:
        """Detect domain from raw text (body) and an optional job title.

        The job title is matched with :data:`_TITLE_WEIGHT` × weight to reflect
        its higher signal value.

        Args:
            text: Full job description or any free-form text.
            job_title: Optional job title string (used with 3× weight).

        Returns:
            Dict with keys:
            - ``domain``: canonical domain key
            - ``display_name``: human-readable label
            - ``confidence``: float 0.0–1.0
            - ``scores``: raw scores per domain
        """
        try:
            result = self._score(text, job_title)

            # Two-stage: if heuristic is low confidence or "other", try NIM
            if result["confidence"] < _NIM_CONFIDENCE_THRESHOLD or result["domain"] == "other":
                nim_result = self.detect_with_nvidia(text, job_title)
                if nim_result:
                    return nim_result

            return result
        except Exception as exc:
            logger.warning("DomainDetector.detect_from_text() failed: %s", exc)
            return self._other_result()

    def detect_from_resume(self, resume_content: Dict[str, Any]) -> Dict[str, Any]:
        """Detect domain from a MasterResume content dict.

        Concatenates summary, all bullet points, skills list, and job titles
        from work experience for detection.

        Args:
            resume_content: JSON dict stored in ``MasterResume.content``.

        Returns:
            Same shape as :meth:`detect_from_text`.
        """
        try:
            parts: List[str] = []

            summary = resume_content.get("professional_summary", "") or ""
            if summary:
                parts.append(summary)

            # Work experience — bullets + job titles
            job_titles: List[str] = []
            for exp in resume_content.get("work_experience", []) or []:
                if not isinstance(exp, dict):
                    continue
                title = exp.get("title", "") or ""
                if title:
                    job_titles.append(title)
                for bullet in exp.get("bullets", []) or []:
                    if bullet:
                        parts.append(str(bullet))

            # Skills
            skills = resume_content.get("skills", []) or []
            if isinstance(skills, list):
                parts.extend(str(s) for s in skills if s)

            # Use the most frequent job title as the title signal
            title_signal = " ".join(job_titles)
            body = " ".join(parts)

            result = self._score(body, title_signal)

            # Two-stage: if heuristic is low confidence or "other", try NIM
            if result["confidence"] < _NIM_CONFIDENCE_THRESHOLD or result["domain"] == "other":
                nim_result = self.detect_with_nvidia(body, title_signal)
                if nim_result:
                    logger.info(
                        "NIM upgraded domain: %s → %s (conf %.2f → %.2f)",
                        result["domain"], nim_result["domain"],
                        result["confidence"], nim_result["confidence"],
                    )
                    return nim_result

            return result

        except Exception as exc:
            logger.warning("DomainDetector.detect_from_resume() failed: %s", exc)
            return self._other_result()

    def detect_from_job(self, job: Any) -> Dict[str, Any]:
        """Detect domain from a Job ORM object.

        Args:
            job: A ``Job`` ORM instance (needs ``job_title`` and
                ``job_description`` attributes).

        Returns:
            Same shape as :meth:`detect_from_text`.
        """
        try:
            title = getattr(job, "job_title", "") or ""
            body = getattr(job, "job_description", "") or ""
            return self._score(body, title)
        except Exception as exc:
            logger.warning("DomainDetector.detect_from_job() failed: %s", exc)
            return self._other_result()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _score(self, text: str, title: str) -> Dict[str, Any]:
        """Core scoring algorithm using word-boundary matching.

        Args:
            text: Body text (job description or resume content).
            title: Title string matched with :data:`_TITLE_WEIGHT` weight.

        Returns:
            Domain detection result dict.
        """
        text_lower = (text or "").lower()
        title_lower = (title or "").lower()

        scores: Dict[str, int] = {domain: 0 for domain in DOMAIN_KEYWORDS}

        for domain, keywords in DOMAIN_KEYWORDS.items():
            for kw in keywords:
                # Word-boundary matching prevents "java" matching "javascript"
                pattern = r"\b" + re.escape(kw.lower()) + r"\b"
                title_hits = len(re.findall(pattern, title_lower))
                scores[domain] += title_hits * _TITLE_WEIGHT
                body_hits = len(re.findall(pattern, text_lower))
                scores[domain] += body_hits

        best_domain = max(scores, key=lambda d: scores[d])
        best_score = scores[best_domain]

        # Check for ties — if tied, return "other"
        top_domains = [d for d, s in scores.items() if s == best_score]
        if best_score == 0 or len(top_domains) > 1:
            return self._other_result(scores)

        # Confidence: ratio of winning score to total score across all domains
        total_score = sum(scores.values())
        confidence = best_score / total_score if total_score > 0 else 0.0

        return {
            "domain": best_domain,
            "display_name": DOMAINS.get(best_domain, best_domain),
            "confidence": round(confidence, 4),
            "scores": scores,
        }

    # ------------------------------------------------------------------
    # NVIDIA NIM classification
    # ------------------------------------------------------------------

    @staticmethod
    def detect_with_nvidia(text: str, job_title: str = "") -> Optional[Dict[str, Any]]:
        """Use NVIDIA NIM to classify domain when heuristic confidence is low.

        Args:
            text: Body text (job description or resume content).
            job_title: Optional job title for context.

        Returns:
            Detection result dict with ``domain``, ``domains`` (list),
            ``display_name``, ``confidence``, or ``None`` on failure.
        """
        api_key = os.getenv("NVIDIA_API_KEY")
        if not api_key:
            logger.debug("NVIDIA_API_KEY not set — skipping NIM domain detection.")
            return None

        try:
            from openai import OpenAI

            client = OpenAI(base_url=_NVIDIA_BASE_URL, api_key=api_key)

            domain_list = "\n".join(
                f"- {key}: {label}" for key, label in DOMAINS.items() if key != "other"
            )

            input_text = ""
            if job_title:
                input_text += f"Title: {job_title}\n\n"
            input_text += text[:3000]

            response = client.chat.completions.create(
                model=_NVIDIA_MODEL_ID,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a resume and job posting classifier. "
                            "Given the text, classify it into one or more domains "
                            "from this list:\n"
                            f"{domain_list}\n\n"
                            "Return ONLY a JSON object with:\n"
                            '- "domains": list of domain keys (most relevant first, max 3)\n'
                            '- "confidence": float 0.0-1.0\n'
                            "Do NOT include any text outside the JSON."
                        ),
                    },
                    {"role": "user", "content": input_text},
                ],
                temperature=0.1,
                max_tokens=256,
            )

            raw = response.choices[0].message.content.strip()
            # Strip markdown fences if present
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*", "", raw)
                raw = re.sub(r"\s*```$", "", raw)

            result = json.loads(raw)
            domains = result.get("domains", [])
            confidence = float(result.get("confidence", 0.0))

            # Validate returned domains are in our registry
            valid_domains = [d for d in domains if d in DOMAINS and d != "other"]
            if not valid_domains:
                return None

            primary = valid_domains[0]
            return {
                "domain": primary,
                "domains": valid_domains,
                "display_name": DOMAINS.get(primary, primary),
                "confidence": round(confidence, 4),
                "scores": {},
                "method": "nvidia",
            }

        except Exception as exc:
            logger.warning("NIM domain detection failed: %s", exc)
            return None

    @staticmethod
    def _other_result(scores: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
        """Return a safe fallback result for the ``'other'`` domain."""
        return {
            "domain": "other",
            "display_name": DOMAINS["other"],
            "confidence": 0.0,
            "scores": scores or {d: 0 for d in DOMAIN_KEYWORDS},
        }
