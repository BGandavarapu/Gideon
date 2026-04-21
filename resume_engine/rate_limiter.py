"""
Token-bucket rate limiter for the NVIDIA NIM API.

NVIDIA NIM limits (2026):
    nvidia/llama-3.3-nemotron-super-49b-v1.5: 60 RPM / 5000 RPD

Each model has its own :class:`RateLimiter` instance, identified by a
``model_key`` (default ``"nvidia"``).  Instances share the same JSON file
(``data/nvidia_usage.json``) but each writes to its own top-level key,
so quota is preserved independently across process restarts.

Usage::

    limiter = RateLimiter(rpm=60, rpd=5000, model_key="nvidia")

    with limiter:           # blocks until a slot is available
        response = call_nvidia_api(...)

    # or as a decorator:
    @limiter.guard
    def call_api(...):
        ...
"""

import json
import logging
import threading
import time
from collections import deque
from datetime import date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_USAGE_FILE = Path("data") / "nvidia_usage.json"
# Support legacy filename during transition
_LEGACY_USAGE_FILE = Path("data") / "gemini_usage.json"

# Default model key used when none is provided.
_DEFAULT_KEY = "nvidia"


class QuotaExceededError(Exception):
    """Raised when the daily API quota would be exceeded."""


class RateLimiter:
    """Thread-safe token-bucket rate limiter with per-model daily quota tracking.

    Args:
        rpm: Maximum requests per minute.
        rpd: Maximum requests per day.
        usage_file: Path to the shared JSON file used to persist daily totals.
        model_key: Which top-level key to read/write in the JSON file.
            Use ``"nvidia"`` for the Nemotron model.  Defaults to ``"nvidia"``.

    Attributes:
        rpm: Configured requests-per-minute ceiling.
        rpd: Configured requests-per-day ceiling.
        model_key: The JSON key used to namespace this limiter's data.
        total_calls: In-process call counter (resets on restart).
        total_tokens_estimated: Rough token estimate (prompt+response chars/4).
    """

    def __init__(
        self,
        rpm: int = 10,
        rpd: int = 250,
        usage_file: Path = _USAGE_FILE,
        model_key: str = _DEFAULT_KEY,
    ) -> None:
        self.rpm = rpm
        self.rpd = rpd
        self.model_key = model_key
        self._usage_file = usage_file
        self._lock = threading.Lock()

        # Sliding window: deque of timestamps for the current 60-second window
        self._window: deque = deque()

        # In-process counters (reset on restart)
        self.total_calls: int = 0
        self.total_tokens_estimated: int = 0

        # Load (or initialise) the persistent daily counter for this model
        self._daily: dict = self._load_daily()

    # ------------------------------------------------------------------
    # Context-manager interface
    # ------------------------------------------------------------------

    def __enter__(self) -> "RateLimiter":
        self.acquire()
        return self

    def __exit__(self, *_) -> None:
        pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(self) -> None:
        """Block until a request slot is available, then claim it.

        Raises:
            QuotaExceededError: If the daily limit has already been hit.
        """
        with self._lock:
            self._check_daily_quota()
            self._enforce_rpm()
            self._record_call()

    def record_tokens(self, prompt_chars: int, response_chars: int) -> None:
        """Update the estimated token counter after a successful call.

        Args:
            prompt_chars: Character length of the prompt sent.
            response_chars: Character length of the response received.
        """
        with self._lock:
            estimated = (prompt_chars + response_chars) // 4
            self.total_tokens_estimated += estimated
            self._daily["tokens_estimated"] = (
                self._daily.get("tokens_estimated", 0) + estimated
            )
            self._save_daily()

    def stats(self) -> dict:
        """Return a snapshot of current usage statistics.

        Returns:
            Dictionary with ``calls_today``, ``calls_remaining``,
            ``calls_this_process``, ``tokens_estimated``, and ``quota_date``.
        """
        with self._lock:
            self._maybe_reset_daily()
            calls_today = self._daily.get("calls", 0)
            return {
                "model_key": self.model_key,
                "calls_today": calls_today,
                "calls_remaining_today": max(0, self.rpd - calls_today),
                "calls_this_process": self.total_calls,
                "tokens_estimated_today": self._daily.get("tokens_estimated", 0),
                "tokens_estimated_process": self.total_tokens_estimated,
                "rpm_limit": self.rpm,
                "rpd_limit": self.rpd,
                "quota_date": self._daily.get("date", str(date.today())),
            }

    def warn_if_low(self, threshold: float = 0.10) -> None:
        """Log a warning if remaining daily quota is below *threshold*.

        Args:
            threshold: Fraction of daily quota considered "low" (default 10%).
        """
        s = self.stats()
        remaining_pct = s["calls_remaining_today"] / self.rpd
        if remaining_pct <= threshold:
            logger.warning(
                "NVIDIA NIM quota low for %s: %d calls remaining today (%.0f%% of %d).",
                self.model_key,
                s["calls_remaining_today"],
                remaining_pct * 100,
                self.rpd,
            )

    def guard(self, fn):
        """Decorator that wraps *fn* with rate-limit acquisition.

        Args:
            fn: Callable to wrap.

        Returns:
            Wrapped callable.
        """
        from functools import wraps

        @wraps(fn)
        def wrapper(*args, **kwargs):
            self.acquire()
            return fn(*args, **kwargs)

        return wrapper

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _enforce_rpm(self) -> None:
        """Block until there is room in the current 60-second window."""
        while True:
            now = time.monotonic()
            # Evict timestamps older than 60 s
            while self._window and now - self._window[0] > 60.0:
                self._window.popleft()

            if len(self._window) < self.rpm:
                break  # slot available

            # Need to wait until the oldest request expires
            sleep_for = 60.0 - (now - self._window[0]) + 0.05
            logger.debug(
                "RPM limit (%d) reached for %s; sleeping %.2f s.",
                self.rpm, self.model_key, sleep_for,
            )
            self._lock.release()
            try:
                time.sleep(sleep_for)
            finally:
                self._lock.acquire()

    def _check_daily_quota(self) -> None:
        self._maybe_reset_daily()
        if self._daily.get("calls", 0) >= self.rpd:
            raise QuotaExceededError(
                f"Daily NVIDIA NIM quota of {self.rpd} requests exceeded for "
                f"model key '{self.model_key}'. Quota resets at midnight UTC."
            )

    def _record_call(self) -> None:
        now = time.monotonic()
        self._window.append(now)
        self.total_calls += 1
        self._daily["calls"] = self._daily.get("calls", 0) + 1
        self._save_daily()
        logger.debug(
            "API call recorded [%s]. Today: %d/%d  Process: %d",
            self.model_key, self._daily["calls"], self.rpd, self.total_calls,
        )

    def _maybe_reset_daily(self) -> None:
        today = str(date.today())
        if self._daily.get("date") != today:
            logger.info(
                "New UTC day detected for %s - resetting daily API usage counter "
                "(previous: %d calls on %s).",
                self.model_key,
                self._daily.get("calls", 0),
                self._daily.get("date", "unknown"),
            )
            self._daily = {"date": today, "calls": 0, "tokens_estimated": 0}
            self._save_daily()

    def _load_daily(self) -> dict:
        """Load the per-model section from the shared usage JSON file."""
        try:
            # Try new file first; fall back to legacy gemini_usage.json (old installs)
            active_file = self._usage_file
            if not active_file.exists() and _LEGACY_USAGE_FILE.exists():
                active_file = _LEGACY_USAGE_FILE
            if active_file.exists():
                with active_file.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)

                today = str(date.today())

                # New two-key format: {"primary": {...}, "bulk": {...}}
                if self.model_key in data and isinstance(data[self.model_key], dict):
                    model_data = data[self.model_key]
                    if model_data.get("date") == today:
                        return model_data

                # Legacy flat format: {"date": ..., "calls": ..., ...}
                # (only matches the default "primary" key for backwards compat)
                elif "date" in data and self.model_key == _DEFAULT_KEY:
                    if data.get("date") == today:
                        logger.debug(
                            "Migrating legacy flat gemini_usage.json to two-key format."
                        )
                        return data

        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not load API usage file for %s: %s", self.model_key, exc)

        return {"date": str(date.today()), "calls": 0, "tokens_estimated": 0}

    def _save_daily(self) -> None:
        """Merge this model's data into the shared usage JSON file."""
        try:
            self._usage_file.parent.mkdir(parents=True, exist_ok=True)

            # Load existing file so we don't clobber the other model's data
            existing: dict = {}
            if self._usage_file.exists():
                try:
                    with self._usage_file.open("r", encoding="utf-8") as fh:
                        existing = json.load(fh)
                except (json.JSONDecodeError, OSError):
                    existing = {}

            # Ensure structure is the new two-key format
            if "date" in existing and "calls" in existing:
                # Legacy flat format — wrap it under its key before writing
                existing = {_DEFAULT_KEY: existing}

            existing[self.model_key] = self._daily

            with self._usage_file.open("w", encoding="utf-8") as fh:
                json.dump(existing, fh, indent=2)

        except OSError as exc:
            logger.warning("Could not save API usage file for %s: %s", self.model_key, exc)
