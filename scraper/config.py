"""
Scraping configuration loader for the Gideon application.

Loads scraping parameters from config.yaml and exposes them as a typed
dataclass so the rest of the scraper package has a single, validated
source of truth for all tunable knobs.
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import yaml

logger = logging.getLogger(__name__)

# Resolved path to the repository-root config file.
_CONFIG_FILE = Path(__file__).resolve().parent.parent / "config.yaml"


@dataclass
class SeleniumConfig:
    """Configuration for Selenium WebDriver behaviour.

    Attributes:
        headless: Run browser without a visible window.
        window_width: Browser viewport width in pixels.
        window_height: Browser viewport height in pixels.
        page_load_timeout: Seconds to wait for a full page load.
        implicit_wait: Seconds the driver polls the DOM for elements.
    """

    headless: bool = True
    window_width: int = 1920
    window_height: int = 1080
    page_load_timeout: int = 30
    implicit_wait: int = 10


@dataclass
class ScrapingConfig:
    """Top-level scraping configuration.

    Attributes:
        delay_min: Minimum delay (seconds) injected between HTTP requests.
        delay_max: Maximum delay (seconds) injected between HTTP requests.
        max_retries: How many times to retry a failing request.
        timeout: HTTP request timeout in seconds.
        max_jobs_per_search: Hard cap on results returned per search query.
        store_raw_html: Persist raw HTML responses to disk for debugging.
        user_agents: Rotation pool of browser User-Agent strings.
        selenium: Nested Selenium-specific settings.
    """

    delay_min: float = 2.0
    delay_max: float = 5.0
    max_retries: int = 3
    timeout: int = 30
    max_jobs_per_search: int = 50
    store_raw_html: bool = False
    user_agents: List[str] = field(default_factory=list)
    selenium: SeleniumConfig = field(default_factory=SeleniumConfig)

    def __post_init__(self) -> None:
        if self.delay_min < 2.0:
            logger.warning(
                "delay_min %.1f is below the 2-second ethical minimum; "
                "clamping to 2.0",
                self.delay_min,
            )
            self.delay_min = 2.0
        if self.delay_max < self.delay_min:
            logger.warning(
                "delay_max %.1f is less than delay_min %.1f; "
                "setting delay_max = delay_min",
                self.delay_max,
                self.delay_min,
            )
            self.delay_max = self.delay_min


def load_scraping_config(config_path: Path = _CONFIG_FILE) -> ScrapingConfig:
    """Load and validate scraping configuration from a YAML file.

    Falls back to safe defaults if the file is missing or malformed so that
    the application can still run in CI/test environments without a config
    file present.

    Args:
        config_path: Absolute path to the YAML configuration file.

    Returns:
        A fully populated :class:`ScrapingConfig` instance.

    Raises:
        yaml.YAMLError: If the file exists but contains invalid YAML.
    """
    if not config_path.exists():
        logger.warning(
            "Config file not found at %s – using built-in defaults.",
            config_path,
        )
        return ScrapingConfig()

    try:
        with config_path.open("r", encoding="utf-8") as config_file:
            raw = yaml.safe_load(config_file) or {}
    except yaml.YAMLError as exc:
        logger.error("Failed to parse %s: %s – using defaults.", config_path, exc)
        return ScrapingConfig()

    scraping_raw: dict = raw.get("scraping", {})
    selenium_raw: dict = raw.get("selenium", {})

    selenium_cfg = SeleniumConfig(
        headless=selenium_raw.get("headless", True),
        window_width=selenium_raw.get("window_width", 1920),
        window_height=selenium_raw.get("window_height", 1080),
        page_load_timeout=selenium_raw.get("page_load_timeout", 30),
        implicit_wait=selenium_raw.get("implicit_wait", 10),
    )

    config = ScrapingConfig(
        delay_min=float(scraping_raw.get("delay_min", 2.0)),
        delay_max=float(scraping_raw.get("delay_max", 5.0)),
        max_retries=int(scraping_raw.get("max_retries", 3)),
        timeout=int(scraping_raw.get("timeout", 30)),
        max_jobs_per_search=int(scraping_raw.get("max_jobs_per_search", 50)),
        store_raw_html=bool(scraping_raw.get("store_raw_html", False)),
        user_agents=scraping_raw.get("user_agents", []),
        selenium=selenium_cfg,
    )

    logger.debug("Loaded scraping config from %s: %s", config_path, config)
    return config


# Module-level singleton so importers can do `from scraper.config import CONFIG`.
CONFIG: ScrapingConfig = load_scraping_config()
