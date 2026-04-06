"""
Shared utility functions for the scraper package.

Provides:
- Randomised, rate-limiting delays between requests.
- A configurable retry decorator with exponential back-off.
- User-agent rotation helpers.
- Raw HTML persistence for debugging.
- HTML text-cleaning utilities.
- Relative-date parsing ("Posted 3 days ago" → datetime.date).
- robots.txt compliance check.
"""

import functools
import html as html_module
import logging
import random
import re
import time
import urllib.robotparser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional, TypeVar

from scraper.config import CONFIG

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

# Where raw HTML debug dumps are stored (relative to project root).
_RAW_HTML_DIR = Path(__file__).resolve().parent.parent / "data" / "jobs"


# ---------------------------------------------------------------------------
# Delay helpers
# ---------------------------------------------------------------------------


def random_delay(
    min_seconds: Optional[float] = None,
    max_seconds: Optional[float] = None,
) -> None:
    """Sleep for a random duration to mimic human browsing behaviour.

    Reads defaults from :data:`scraper.config.CONFIG` so callers rarely
    need to pass explicit values.

    Args:
        min_seconds: Lower bound of the sleep range (>= 2.0 enforced).
        max_seconds: Upper bound of the sleep range.
    """
    low = max(min_seconds if min_seconds is not None else CONFIG.delay_min, 2.0)
    high = max_seconds if max_seconds is not None else CONFIG.delay_max
    high = max(high, low)

    duration = random.uniform(low, high)
    logger.debug("Sleeping %.2f seconds (rate limiting).", duration)
    time.sleep(duration)


# ---------------------------------------------------------------------------
# Retry decorator
# ---------------------------------------------------------------------------


def retry(
    max_attempts: Optional[int] = None,
    exceptions: tuple = (Exception,),
    base_delay: float = 1.0,
    backoff_factor: float = 2.0,
) -> Callable[[F], F]:
    """Decorator that retries a function on specified exceptions.

    Uses exponential back-off between attempts: after attempt *n* the
    wait is ``base_delay * (backoff_factor ** (n - 1))`` seconds.

    Args:
        max_attempts: Total attempts before re-raising the last exception.
            Defaults to :attr:`scraper.config.ScrapingConfig.max_retries`.
        exceptions: Tuple of exception types that trigger a retry.
        base_delay: Initial delay in seconds before the first retry.
        backoff_factor: Multiplier applied to the delay after each attempt.

    Returns:
        A decorator that wraps the target function with retry logic.

    Example:
        >>> @retry(max_attempts=3, exceptions=(requests.HTTPError,))
        ... def fetch(url: str) -> str: ...
    """
    attempts = max_attempts if max_attempts is not None else CONFIG.max_retries

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Optional[Exception] = None
            for attempt in range(1, attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    wait = base_delay * (backoff_factor ** (attempt - 1))
                    if attempt < attempts:
                        logger.warning(
                            "%s failed (attempt %d/%d): %s – retrying in %.1fs.",
                            func.__qualname__,
                            attempt,
                            attempts,
                            exc,
                            wait,
                        )
                        time.sleep(wait)
                    else:
                        logger.error(
                            "%s failed after %d attempts: %s",
                            func.__qualname__,
                            attempts,
                            exc,
                        )
            raise last_exc  # type: ignore[misc]

        return wrapper  # type: ignore[return-value]

    return decorator


# ---------------------------------------------------------------------------
# User-agent rotation
# ---------------------------------------------------------------------------


def get_random_user_agent() -> str:
    """Return a random User-Agent string from the configured pool.

    Falls back to a hardcoded Chrome string if the pool is empty.

    Returns:
        A browser User-Agent header value.
    """
    agents = CONFIG.user_agents
    if not agents:
        fallback = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
        logger.debug("User-agent pool is empty; using fallback.")
        return fallback
    chosen = random.choice(agents)
    logger.debug("Selected user agent: %s", chosen[:60])
    return chosen


def build_request_headers(extra: Optional[dict] = None) -> dict:
    """Build a minimal set of HTTP request headers.

    Combines a randomised User-Agent with Accept/Language headers that
    mimic a real browser.  Caller-supplied *extra* headers are merged
    last and will override defaults.

    Args:
        extra: Optional mapping of additional headers to include.

    Returns:
        Dictionary of HTTP header name-value pairs.
    """
    headers = {
        "User-Agent": get_random_user_agent(),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    if extra:
        headers.update(extra)
    return headers


# ---------------------------------------------------------------------------
# Raw HTML persistence
# ---------------------------------------------------------------------------


def save_raw_html(filename: str, html_content: str) -> Optional[Path]:
    """Persist raw HTML to disk for debugging purposes.

    Only writes to disk when ``CONFIG.store_raw_html`` is ``True``.
    The file is saved under ``data/jobs/<filename>``.

    Args:
        filename: Target filename (e.g. ``"linkedin_page_1.html"``).
        html_content: Raw HTML string to persist.

    Returns:
        The :class:`~pathlib.Path` that was written, or ``None`` if
        ``store_raw_html`` is disabled.
    """
    if not CONFIG.store_raw_html:
        return None

    try:
        _RAW_HTML_DIR.mkdir(parents=True, exist_ok=True)
        target = _RAW_HTML_DIR / filename
        target.write_text(html_content, encoding="utf-8")
        logger.debug("Saved raw HTML to %s (%d bytes).", target, len(html_content))
        return target
    except OSError as exc:
        logger.warning("Could not save raw HTML to %s: %s", filename, exc)
        return None


# ---------------------------------------------------------------------------
# Text-cleaning utilities
# ---------------------------------------------------------------------------


def clean_text(text: Optional[str]) -> str:
    """Normalise whitespace and strip leading/trailing space from *text*.

    Args:
        text: Raw string, possibly ``None``.

    Returns:
        Cleaned string, or an empty string if *text* was ``None``.
    """
    if not text:
        return ""
    lines = (line.strip() for line in text.splitlines())
    return " ".join(word for line in lines for word in line.split())


def truncate_text(text: str, max_length: int = 5000) -> str:
    """Truncate *text* to *max_length* characters without cutting mid-word.

    Args:
        text: Input string.
        max_length: Maximum character count.

    Returns:
        Original string if shorter than *max_length*, otherwise a truncated
        version ending with ``"…"``.
    """
    if len(text) <= max_length:
        return text
    truncated = text[:max_length].rsplit(" ", 1)[0]
    return truncated + "…"


def extract_salary_range(raw_salary: Optional[str]) -> Optional[str]:
    """Attempt to normalise a salary string scraped from a job board.

    Strips surrounding whitespace and returns ``None`` when the input is
    empty or contains only placeholder text such as ``"Not provided"``.

    Args:
        raw_salary: Raw salary text extracted from a job listing.

    Returns:
        Cleaned salary string, or ``None`` if the value is not meaningful.
    """
    if not raw_salary:
        return None
    cleaned = raw_salary.strip()
    placeholders = {"not provided", "n/a", "-", "—", ""}
    if cleaned.lower() in placeholders:
        return None
    return cleaned


def clean_html_text(text: Optional[str]) -> str:
    """Strip HTML tags and decode HTML entities from *text*.

    Useful for job descriptions that arrive with inline markup even after
    BeautifulSoup's ``get_text()`` is called (e.g. ``&amp;``, ``&nbsp;``).

    Args:
        text: Raw string, possibly containing HTML fragments.

    Returns:
        Plain-text string with entities decoded and tags removed.
    """
    if not text:
        return ""
    # Decode HTML entities first (e.g. &amp; → &, &nbsp; → space)
    decoded = html_module.unescape(text)
    # Strip any residual HTML tags
    stripped = re.sub(r"<[^>]+>", " ", decoded)
    # Collapse whitespace
    return re.sub(r"\s+", " ", stripped).strip()


# ---------------------------------------------------------------------------
# Relative-date parsing
# ---------------------------------------------------------------------------

_RELATIVE_DATE_PATTERNS: list[tuple[re.Pattern, int]] = [
    (re.compile(r"\bjust\s+posted\b", re.I), 0),
    (re.compile(r"\btoday\b", re.I), 0),
    (re.compile(r"(\d+)\s+hour", re.I), 0),      # same day
    (re.compile(r"(\d+)\s+day", re.I), None),     # N days ago  (captured)
    (re.compile(r"\byesterday\b", re.I), 1),
    (re.compile(r"\ba\s+week\s+ago\b", re.I), 7),
    (re.compile(r"(\d+)\s+week", re.I), None),    # N weeks ago (captured × 7)
    (re.compile(r"\ba\s+month\s+ago\b", re.I), 30),
    (re.compile(r"(\d+)\s+month", re.I), None),   # N months ago (captured × 30)
    (re.compile(r"\b30\+\s+days\b", re.I), 30),
]


def extract_relative_date(raw: Optional[str]) -> Optional[date]:
    """Convert a relative date string to an absolute :class:`datetime.date`.

    Handles the informal strings that job boards use instead of ISO dates,
    e.g. ``"Posted 3 days ago"``, ``"Just posted"``, ``"30+ days ago"``.

    Args:
        raw: Raw date text from a job card or posting page.

    Returns:
        Approximate :class:`datetime.date`, or ``None`` if the string cannot
        be recognised.

    Examples:
        >>> extract_relative_date("Posted 2 days ago")
        datetime.date(2026, 3, 17)   # assuming today is 2026-03-19
        >>> extract_relative_date("Just posted")
        datetime.date(2026, 3, 19)
        >>> extract_relative_date("unknown format")
        None
    """
    if not raw:
        return None

    today = datetime.now(timezone.utc).date()

    # Try ISO date first (YYYY-MM-DD or YYYY-MM-DDThh…)
    iso_match = re.search(r"\d{4}-\d{2}-\d{2}", raw)
    if iso_match:
        try:
            return date.fromisoformat(iso_match.group())
        except ValueError:
            pass

    for pattern, delta_days in _RELATIVE_DATE_PATTERNS:
        match = pattern.search(raw)
        if not match:
            continue

        if delta_days is not None:
            # Fixed offset
            return today - timedelta(days=delta_days)

        # Variable offset – first capture group holds the number
        try:
            n = int(match.group(1))
        except (IndexError, ValueError):
            continue

        if "week" in pattern.pattern:
            return today - timedelta(days=n * 7)
        if "month" in pattern.pattern:
            return today - timedelta(days=n * 30)
        # days
        return today - timedelta(days=n)

    logger.debug("Could not parse relative date from: %r", raw)
    return None


# ---------------------------------------------------------------------------
# robots.txt compliance
# ---------------------------------------------------------------------------


def is_scraping_allowed(base_url: str, path: str = "/jobs", user_agent: str = "*") -> bool:
    """Check whether ``robots.txt`` permits scraping the given path.

    Fetches ``<base_url>/robots.txt`` once and inspects the ``Disallow``
    directives.  Returns ``True`` on any network or parse failure (fail-open)
    so that transient errors do not block the whole scrape; the caller is
    expected to log the result.

    Args:
        base_url: Scheme + host of the target site (e.g. ``"https://www.indeed.com"``).
        path: URL path to check (e.g. ``"/jobs"``).
        user_agent: User-agent token to check against (default ``"*"``).

    Returns:
        ``True`` if the path is allowed or robots.txt cannot be fetched,
        ``False`` if explicitly disallowed.
    """
    robots_url = base_url.rstrip("/") + "/robots.txt"
    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(robots_url)
    try:
        parser.read()
        allowed = parser.can_fetch(user_agent, base_url + path)
        if not allowed:
            logger.warning(
                "robots.txt at %s disallows scraping %s for user-agent %r.",
                robots_url,
                path,
                user_agent,
            )
        else:
            logger.debug("robots.txt allows scraping %s%s.", base_url, path)
        return allowed
    except Exception as exc:
        logger.warning("Could not fetch robots.txt from %s: %s – proceeding.", robots_url, exc)
        return True
