"""
Typed exception hierarchy for the scraper package.

Having dedicated exception types lets callers catch scraper-specific errors
without accidentally swallowing unrelated exceptions (e.g. programming bugs).

Hierarchy
---------
ScraperError                   – base for all scraper failures
├── NetworkError               – HTTP / connection-level failure
│   └── RateLimitError         – HTTP 429 / 503 (too many requests)
├── ParseError                 – could not extract required fields from HTML
├── BlockedError               – site returned a CAPTCHA / bot-detection page
└── DatabasePersistenceError   – job could not be saved to the database
"""


class ScraperError(Exception):
    """Base class for all scraper-specific exceptions.

    Args:
        message: Human-readable description of the failure.
        url: The URL that was being processed when the error occurred.
    """

    def __init__(self, message: str, url: str = "") -> None:
        super().__init__(message)
        self.url = url

    def __str__(self) -> str:
        base = super().__str__()
        if self.url:
            return f"{base} [url={self.url}]"
        return base


class NetworkError(ScraperError):
    """Raised when an HTTP request fails after all retries are exhausted.

    Args:
        message: Description of the network failure.
        url: URL that triggered the failure.
        status_code: HTTP status code if a response was received.
    """

    def __init__(self, message: str, url: str = "", status_code: int = 0) -> None:
        super().__init__(message, url)
        self.status_code = status_code

    def __str__(self) -> str:
        base = super().__str__()
        if self.status_code:
            return f"{base} [HTTP {self.status_code}]"
        return base


class RateLimitError(NetworkError):
    """Raised when the site responds with HTTP 429 or 503.

    Signals that the caller should back off significantly before retrying.
    """


class ParseError(ScraperError):
    """Raised when required fields cannot be extracted from a page.

    Args:
        message: Description of what could not be parsed.
        url: URL of the page that failed.
        missing_fields: List of field names that were absent or empty.
    """

    def __init__(
        self,
        message: str,
        url: str = "",
        missing_fields: list | None = None,
    ) -> None:
        super().__init__(message, url)
        self.missing_fields: list = missing_fields or []

    def __str__(self) -> str:
        base = super().__str__()
        if self.missing_fields:
            return f"{base} [missing={self.missing_fields}]"
        return base


class BlockedError(ScraperError):
    """Raised when the site returns a CAPTCHA or bot-detection page.

    This is non-retryable in the current session; the caller should
    pause for a significant period before trying again.
    """


class DatabasePersistenceError(ScraperError):
    """Raised when a :class:`~scraper.base_scraper.JobPosting` cannot be saved.

    Args:
        message: Description of the persistence failure.
        url: Application URL of the posting that failed to save.
    """
