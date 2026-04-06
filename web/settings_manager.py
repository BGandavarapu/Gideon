"""SettingsManager — reads and writes data/settings.json.

This is the single source of truth for user-controlled automation settings.
It is intentionally separate from config.yaml so user preferences survive
config changes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_VALID_TASKS = ("scrape", "generate")
_VALID_MODES = ("manual", "automatic")
_VALID_RESUME_MODES = ("sample", "own")
_SCHEDULE_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# All supported job domains.
DOMAINS = {
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


class SettingsManager:
    """Read/write user automation preferences stored in *data/settings.json*.

    All public methods are safe to call from Flask request handlers —
    they never propagate ``IOError`` or ``json.JSONDecodeError``;
    errors are logged as warnings and defaults are returned instead.

    Example::

        sm = SettingsManager()
        sm.set_mode("scrape", "automatic")
        sm.set_schedule("scrape", "08:30")
        print(sm.get_mode("scrape"))   # → "automatic"
    """

    SETTINGS_PATH: str = "data/settings.json"

    DEFAULTS: Dict[str, Any] = {
        "automation": {
            "scrape":   {"mode": "manual",   "schedule": "09:00"},
            "generate": {"mode": "manual",   "schedule": "10:00"},
        },
        "resume_mode": "sample",
        "preferred_location": "",
        # Kept as factory defaults for brand-new installs that haven't yet
        # selected a domain via a resume.  Once a resume/domain is active,
        # industry configs (driven by the active domain) take over and these
        # are removed by clear_legacy_search_configs() on startup.
        "search_configs": [
            {
                "id": "sc_1",
                "keywords": "python developer",
                "location": "San Francisco",
                "source": "linkedin",
                "max_results": 20,
                "domain": "software_engineering",
                "enabled": True,
            },
            {
                "id": "sc_2",
                "keywords": "machine learning engineer",
                "location": "Remote",
                "source": "linkedin",
                "max_results": 15,
                "domain": "ai_ml",
                "enabled": True,
            },
            {
                "id": "sc_3",
                "keywords": "software engineer",
                "location": "San Francisco",
                "source": "linkedin",
                "max_results": 15,
                "domain": "software_engineering",
                "enabled": True,
            },
        ],
        "domain_resumes": {domain: None for domain in DOMAINS},
        "last_updated": None,
    }

    # ------------------------------------------------------------------
    # Core load / save
    # ------------------------------------------------------------------

    def load(self) -> Dict[str, Any]:
        """Load settings from disk.

        If the file is missing or corrupt, writes and returns
        :attr:`DEFAULTS`. Never raises.
        """
        path = Path(self.SETTINGS_PATH)
        if path.exists():
            try:
                raw = path.read_text(encoding="utf-8")
                data = json.loads(raw)
                # Merge defaults for any missing keys so old files stay valid
                return self._merge_defaults(data)
            except Exception as exc:
                logger.warning(
                    "settings.json unreadable (%s) — resetting to defaults.", exc
                )
        # File missing or corrupt: write defaults and return them
        defaults = self._deep_copy(self.DEFAULTS)
        self.save(defaults)
        return defaults

    def save(self, settings: Dict[str, Any]) -> bool:
        """Persist *settings* to disk with an updated ``last_updated`` timestamp.

        Returns ``True`` on success, ``False`` on ``IOError``.
        """
        settings["last_updated"] = datetime.now(timezone.utc).isoformat()
        try:
            path = Path(self.SETTINGS_PATH)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(settings, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            return True
        except OSError as exc:
            logger.error("Failed to save settings.json: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    def get_mode(self, task: str) -> str:
        """Return ``'manual'`` or ``'automatic'`` for *task*.

        Falls back to ``'manual'`` if the setting is missing.
        """
        settings = self.load()
        return settings.get("automation", {}).get(task, {}).get("mode", "manual")

    def set_mode(self, task: str, mode: str) -> bool:
        """Set *mode* for *task* and save.

        Raises:
            ValueError: if *task* is not in ``('scrape', 'generate')``
                        or *mode* is not in ``('manual', 'automatic')``.

        Returns:
            ``True`` on success.
        """
        if task not in _VALID_TASKS:
            raise ValueError(
                f"Invalid task {task!r}. Must be one of: {_VALID_TASKS}"
            )
        if mode not in _VALID_MODES:
            raise ValueError(
                f"Invalid mode {mode!r}. Must be one of: {_VALID_MODES}"
            )
        settings = self.load()
        settings.setdefault("automation", {}).setdefault(task, {})
        settings["automation"][task]["mode"] = mode
        return self.save(settings)

    def get_schedule(self, task: str) -> str:
        """Return the schedule time string (e.g. ``'09:00'``) for *task*.

        Falls back to ``'09:00'`` for scrape and ``'10:00'`` for generate.
        """
        defaults = {"scrape": "09:00", "generate": "10:00"}
        settings = self.load()
        return (
            settings.get("automation", {})
            .get(task, {})
            .get("schedule", defaults.get(task, "09:00"))
        )

    def set_schedule(self, task: str, schedule: str) -> bool:
        """Set the schedule time string for *task* and save.

        Raises:
            ValueError: if *schedule* is not a valid ``HH:MM`` string
                        (00:00–23:59) or *task* is invalid.

        Returns:
            ``True`` on success.
        """
        if task not in _VALID_TASKS:
            raise ValueError(
                f"Invalid task {task!r}. Must be one of: {_VALID_TASKS}"
            )
        if not _SCHEDULE_RE.match(schedule or ""):
            raise ValueError(
                f"Invalid schedule {schedule!r}. Must be HH:MM (e.g. '09:00')."
            )
        settings = self.load()
        settings.setdefault("automation", {}).setdefault(task, {})
        settings["automation"][task]["schedule"] = schedule
        return self.save(settings)

    # ------------------------------------------------------------------
    # Resume mode accessors
    # ------------------------------------------------------------------

    def get_resume_mode(self) -> str:
        """Return ``'sample'`` or ``'own'`` (current active resume mode).

        Falls back to ``'sample'`` if the key is absent.
        """
        return self.load().get("resume_mode", "sample")

    def set_resume_mode(self, mode: str) -> bool:
        """Set the active resume mode and save.

        Raises:
            ValueError: if *mode* is not ``'sample'`` or ``'own'``.

        Returns:
            ``True`` on success.
        """
        if mode not in _VALID_RESUME_MODES:
            raise ValueError(
                f"Invalid resume_mode {mode!r}. Must be one of: {_VALID_RESUME_MODES}"
            )
        settings = self.load()
        settings["resume_mode"] = mode
        return self.save(settings)

    def get_preferred_location(self) -> str:
        """Return the user's preferred scraping location (empty string = no preference)."""
        return str(self.load().get("preferred_location", "")).strip()

    def set_preferred_location(self, location: str) -> None:
        """Save the user's preferred scraping location."""
        settings = self.load()
        settings["preferred_location"] = location.strip()
        self.save(settings)

    # ------------------------------------------------------------------
    # Search config accessors
    # ------------------------------------------------------------------

    def get_search_configs(self, enabled_only: bool = True) -> List[Dict[str, Any]]:
        """Return search configuration list.

        Args:
            enabled_only: When ``True`` (default), only configs with
                ``enabled=True`` are returned.

        Returns:
            List of search config dicts.
        """
        configs = self.load().get("search_configs", [])
        if enabled_only:
            return [c for c in configs if c.get("enabled", True)]
        return list(configs)

    def add_search_config(self, config: Dict[str, Any]) -> str:
        """Add a new search configuration.

        Validates required fields (``keywords``, ``location``, ``source``,
        ``domain``).  ``source`` must be ``'linkedin'``.  ``domain`` must be
        a key in :data:`DOMAINS`.  Generates a unique ``id`` automatically.

        Args:
            config: Dict with at minimum ``keywords``, ``location``,
                ``source``, ``domain``.

        Returns:
            The generated config id string.

        Raises:
            ValueError: On missing required fields or invalid values.
        """
        for field in ("keywords", "location", "source", "domain"):
            if not config.get(field):
                raise ValueError(f"search config missing required field: {field!r}")
        if config["source"] != "linkedin":
            raise ValueError(
                f"source must be 'linkedin' (received {config['source']!r})"
            )
        if config["domain"] not in DOMAINS:
            raise ValueError(
                f"Invalid domain {config['domain']!r}. "
                f"Must be one of: {list(DOMAINS.keys())}"
            )

        settings = self.load()
        configs: List[Dict] = settings.setdefault("search_configs", [])

        # Generate unique id
        existing_ids = {c.get("id", "") for c in configs}
        n = len(configs) + 1
        new_id = f"sc_{n}"
        while new_id in existing_ids:
            n += 1
            new_id = f"sc_{n}"

        new_config: Dict[str, Any] = {
            "id": new_id,
            "keywords":    config["keywords"],
            "location":    config["location"],
            "source":      config["source"],
            "max_results": int(config.get("max_results", 20)),
            "domain":      config["domain"],
            "enabled":     bool(config.get("enabled", True)),
        }
        configs.append(new_config)
        self.save(settings)
        logger.info("Added search config %r: %r", new_id, new_config["keywords"])
        return new_id

    def update_search_config(self, config_id: str, updates: Dict[str, Any]) -> bool:
        """Update fields on an existing search config by id.

        Args:
            config_id: The ``id`` of the config to update.
            updates: Dict of fields to update (partial).

        Returns:
            ``True`` if found and updated, ``False`` if not found.
        """
        settings = self.load()
        configs: List[Dict] = settings.get("search_configs", [])
        for cfg in configs:
            if cfg.get("id") == config_id:
                # Validate domain if being updated
                if "domain" in updates and updates["domain"] not in DOMAINS:
                    raise ValueError(
                        f"Invalid domain {updates['domain']!r}."
                    )
                if "source" in updates and updates["source"] != "linkedin":
                    raise ValueError("source must be 'linkedin'")
                cfg.update(updates)
                self.save(settings)
                return True
        return False

    def delete_search_config(self, config_id: str) -> bool:
        """Remove a search config by id.

        Args:
            config_id: The ``id`` of the config to remove.

        Returns:
            ``True`` if found and removed, ``False`` if not found.
        """
        settings = self.load()
        configs: List[Dict] = settings.get("search_configs", [])
        new_configs = [c for c in configs if c.get("id") != config_id]
        if len(new_configs) == len(configs):
            return False
        settings["search_configs"] = new_configs
        self.save(settings)
        return True

    # ------------------------------------------------------------------
    # Industry search configs (read-only built-in defaults per domain)
    # ------------------------------------------------------------------

    #: Built-in search query presets for each domain.  These are READ-ONLY
    #: and cannot be modified by users through the UI — they serve as the
    #: resume-driven search baseline.
    INDUSTRY_SEARCH_CONFIGS: Dict[str, list] = {
        "software_engineering": [
            {"keywords": "software engineer",      "location": "San Francisco", "source": "linkedin", "max_results": 20},
            {"keywords": "backend engineer",        "location": "Remote",        "source": "linkedin", "max_results": 15},
            {"keywords": "full stack engineer",     "location": "New York",      "source": "linkedin", "max_results": 15},
        ],
        "ai_ml": [
            {"keywords": "machine learning engineer", "location": "Remote",        "source": "linkedin", "max_results": 20},
            {"keywords": "AI engineer",               "location": "San Francisco", "source": "linkedin", "max_results": 15},
            {"keywords": "data scientist",            "location": "Remote",        "source": "linkedin", "max_results": 15},
        ],
        "product_management": [
            {"keywords": "product manager",           "location": "Remote",        "source": "linkedin", "max_results": 20},
            {"keywords": "senior product manager",    "location": "San Francisco", "source": "linkedin", "max_results": 15},
            {"keywords": "technical product manager", "location": "Remote",        "source": "linkedin", "max_results": 15},
        ],
        "marketing": [
            {"keywords": "marketing manager",         "location": "Remote",   "source": "linkedin", "max_results": 20},
            {"keywords": "growth manager",            "location": "New York", "source": "linkedin", "max_results": 15},
            {"keywords": "digital marketing manager", "location": "Remote",   "source": "linkedin", "max_results": 15},
        ],
        "data_analytics": [
            {"keywords": "data analyst",         "location": "Remote",   "source": "linkedin", "max_results": 20},
            {"keywords": "analytics engineer",   "location": "Remote",   "source": "linkedin", "max_results": 15},
            {"keywords": "business analyst",     "location": "New York", "source": "linkedin", "max_results": 15},
        ],
        "design": [
            {"keywords": "product designer", "location": "Remote",        "source": "linkedin", "max_results": 20},
            {"keywords": "UX designer",      "location": "San Francisco", "source": "linkedin", "max_results": 15},
            {"keywords": "UI designer",      "location": "Remote",        "source": "linkedin", "max_results": 15},
        ],
        "finance": [
            {"keywords": "financial analyst",  "location": "New York", "source": "linkedin", "max_results": 20},
            {"keywords": "FP&A analyst",       "location": "Remote",   "source": "linkedin", "max_results": 15},
            {"keywords": "investment analyst", "location": "New York", "source": "linkedin", "max_results": 15},
        ],
        "sales": [
            {"keywords": "account executive",                  "location": "Remote",   "source": "linkedin", "max_results": 20},
            {"keywords": "enterprise sales manager",           "location": "New York", "source": "linkedin", "max_results": 15},
            {"keywords": "business development representative","location": "Remote",   "source": "linkedin", "max_results": 15},
        ],
        "operations": [
            {"keywords": "operations manager",      "location": "Remote",        "source": "linkedin", "max_results": 20},
            {"keywords": "program manager",         "location": "San Francisco", "source": "linkedin", "max_results": 15},
            {"keywords": "strategy and operations", "location": "Remote",        "source": "linkedin", "max_results": 15},
        ],
        "other": [],
    }

    def get_industry_search_configs(self, domain: str) -> List[Dict[str, Any]]:
        """Return the built-in (read-only) search configs for *domain*.

        Args:
            domain: A key from :data:`DOMAINS`.

        Returns:
            List of search config dicts (each with keywords, location,
            source, max_results).  Returns ``[]`` if domain not found.
        """
        return list(self.INDUSTRY_SEARCH_CONFIGS.get(domain, []))

    def get_active_domains(self) -> List[str]:
        """Return all domains of the active resume (multi-domain aware).

        Reads ``MasterResume.domains`` if set; otherwise falls back to
        the single ``domain`` field.  The ``other`` pseudo-domain and any
        invalid entries are filtered out before returning.

        Returns:
            List of valid domain strings (e.g. ``['software_engineering', 'ai_ml']``).
            Returns ``[]`` if no active resume or no domain configured.
        """
        try:
            from database.database import get_db as _get_db
            from database.models import MasterResume as _MR

            with _get_db() as db:
                db.expire_all()
                resume = (
                    db.query(_MR)
                    .filter(_MR.is_active.is_(True))
                    .first()
                )
                if resume:
                    if resume.domains and isinstance(resume.domains, list):
                        valid = [d for d in resume.domains if d in DOMAINS and d != "other"]
                        if valid:
                            logger.info(
                                "[settings] get_active_domains() -> %r (id=%s)",
                                valid, resume.id,
                            )
                            return valid
                    if resume.domain and resume.domain not in (None, "other"):
                        logger.info(
                            "[settings] get_active_domains() -> [%r] (id=%s, fallback)",
                            resume.domain, resume.id,
                        )
                        return [resume.domain]
        except Exception as exc:
            logger.error("[settings] get_active_domains() failed: %s", exc)
        return []

    def get_industry_search_configs_for_domains(self, domains: List[str]) -> List[Dict[str, Any]]:
        """Return deduplicated built-in search configs for multiple domains.

        Iterates over each domain in order, appending configs that haven't
        been seen yet (deduplication key: ``keywords + location``).

        Args:
            domains: List of domain keys (e.g. ``['software_engineering', 'ai_ml']``).

        Returns:
            Merged, deduplicated list of search config dicts.
        """
        preferred_location = self.get_preferred_location()
        seen: set = set()
        merged: List[Dict[str, Any]] = []
        for domain in domains:
            for cfg in self.INDUSTRY_SEARCH_CONFIGS.get(domain, []):
                effective_location = preferred_location if preferred_location else cfg.get("location", "")
                key = (cfg["keywords"].lower(), effective_location.lower())
                if key not in seen:
                    seen.add(key)
                    merged.append({**cfg, "location": effective_location})
        return merged

    def get_active_domain(self) -> Optional[str]:
        """Return the domain of the currently active :class:`MasterResume`.

        Always opens a fresh DB session and calls ``expire_all()`` to avoid
        stale reads when called from background threads.  Returns ``None`` if
        no active resume exists or if the active resume has no domain set.

        Returns:
            Domain string (e.g. ``'software_engineering'``) or ``None``.
        """
        try:
            from database.database import get_db as _get_db
            from database.models import MasterResume as _MR

            with _get_db() as db:
                db.expire_all()
                resume = (
                    db.query(_MR)
                    .filter(_MR.is_active.is_(True))
                    .first()
                )
                if resume and resume.domain:
                    logger.info(
                        "[settings] get_active_domain() -> %r (id=%s)",
                        resume.domain, resume.id,
                    )
                    return resume.domain
                logger.warning(
                    "[settings] get_active_domain() -> None "
                    "(no active resume or no domain set)"
                )
        except Exception as exc:
            logger.error("[settings] get_active_domain() failed: %s", exc)
        return None

    # ------------------------------------------------------------------
    # Domain resume accessors
    # ------------------------------------------------------------------

    def get_domain_resume(self, domain: str) -> Optional[int]:
        """Return the resume_id assigned to *domain*, or ``None``.

        Args:
            domain: A key from :data:`DOMAINS`.

        Returns:
            Integer resume id or ``None`` if not assigned.
        """
        mapping = self.load().get("domain_resumes", {})
        val = mapping.get(domain)
        return int(val) if val is not None else None

    def set_domain_resume(self, domain: str, resume_id: Optional[int]) -> bool:
        """Assign (or clear) a resume for *domain*.

        Args:
            domain: A key from :data:`DOMAINS`.
            resume_id: Integer resume id, or ``None`` to clear.

        Returns:
            ``True`` on success.

        Raises:
            ValueError: If *domain* is not a valid domain key.
        """
        if domain not in DOMAINS:
            raise ValueError(
                f"Invalid domain {domain!r}. Must be one of: {list(DOMAINS.keys())}"
            )
        settings = self.load()
        settings.setdefault("domain_resumes", {})[domain] = resume_id
        return self.save(settings)

    # ------------------------------------------------------------------
    # Migration helpers
    # ------------------------------------------------------------------

    def clear_legacy_search_configs(self) -> None:
        """Remove the 3 original hardcoded SE search configs seeded at project creation.

        Only removes configs whose ``keywords`` exactly match:
        ``'python developer'``, ``'machine learning engineer'``, ``'software engineer'``.
        Any configs added by the user are preserved.

        Safe to call multiple times — a no-op when the legacy configs are absent.
        """
        _LEGACY = {
            "python developer",
            "machine learning engineer",
            "software engineer",
        }
        settings = self.load()
        original: List[Dict] = settings.get("search_configs", [])
        cleaned = [
            c for c in original
            if c.get("keywords", "").lower() not in _LEGACY
        ]
        removed = len(original) - len(cleaned)
        if removed:
            settings["search_configs"] = cleaned
            self.save(settings)
            logger.info(
                "[settings] Removed %d legacy SE search config(s) from settings.json.",
                removed,
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _deep_copy(obj: Any) -> Any:
        """Return a deep copy via JSON round-trip."""
        return json.loads(json.dumps(obj))

    def _merge_defaults(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Fill in any keys missing from *data* using :attr:`DEFAULTS`."""
        merged = self._deep_copy(self.DEFAULTS)
        for key, val in data.items():
            if key == "automation" and isinstance(val, dict):
                for task, task_val in val.items():
                    if task in merged["automation"] and isinstance(task_val, dict):
                        merged["automation"][task].update(task_val)
                    else:
                        merged["automation"][task] = task_val
            elif key == "search_configs" and isinstance(val, list):
                # User's list replaces defaults entirely if non-empty
                merged["search_configs"] = val
            elif key == "domain_resumes" and isinstance(val, dict):
                # Merge: fill in missing domains from defaults, preserve user values
                for domain in merged["domain_resumes"]:
                    if domain in val:
                        merged["domain_resumes"][domain] = val[domain]
                # Keep any extra domains user may have added
                for domain, rid in val.items():
                    if domain not in merged["domain_resumes"]:
                        merged["domain_resumes"][domain] = rid
            else:
                merged[key] = val
        return merged
