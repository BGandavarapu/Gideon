"""LearningTutor — search YouTube and web for skill-learning resources via SerpAPI."""

from __future__ import annotations

import logging
import os
import re
import webbrowser

import requests

logger = logging.getLogger(__name__)

TRUSTED_SOURCES = [
    "freecodecamp.org",
    "developer.mozilla.org",
    "docs.python.org",
    "w3schools.com",
    "coursera.org",
    "udemy.com",
    "dev.to",
    "roadmap.sh",
    "kaggle.com",
    "fast.ai",
    "github.com",
    "geeksforgeeks.org",
    "tutorialspoint.com",
    "realpython.com",
    "javascript.info",
    "learn.microsoft.com",
    "cloud.google.com/learn",
    "aws.amazon.com/training",
]


class LearningTutor:

    def __init__(self) -> None:
        self.api_key = os.getenv("SERPAPI_KEY")
        self.base_url = "https://serpapi.com/search"

        if not self.api_key:
            logger.warning("SERPAPI_KEY not set — learning tutor will not work")

    def find_resources(
        self, skill: str, max_youtube: int = 4, max_articles: int = 4
    ) -> dict:
        if not self.api_key:
            return {
                "skill": skill,
                "youtube": [],
                "articles": [],
                "total": 0,
                "error": "SERPAPI_KEY not configured",
            }

        youtube = self._search_youtube(skill, max_youtube)
        articles = self._search_articles(skill, max_articles)

        return {
            "skill": skill,
            "youtube": youtube,
            "articles": articles,
            "total": len(youtube) + len(articles),
        }

    def _search_youtube(self, skill: str, max_results: int = 4) -> list:
        try:
            params = {
                "engine": "youtube",
                "search_query": f"{skill} tutorial for beginners 2025",
                "api_key": self.api_key,
            }
            r = requests.get(self.base_url, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()

            results = []
            for video in data.get("video_results", []):
                if video.get("type") in ("ad", "playlist"):
                    continue

                url = video.get("link", "")
                if not url:
                    continue

                results.append(
                    {
                        "title": video.get("title", ""),
                        "url": url,
                        "channel": video.get("channel", {}).get("name", ""),
                        "duration": video.get("length", ""),
                        "views": video.get("views", ""),
                        "thumbnail": video.get("thumbnail", {}).get("static", ""),
                        "description": video.get("description", "")[:150],
                    }
                )

                if len(results) >= max_results:
                    break

            logger.info("[Tutor] YouTube search for '%s': %d results", skill, len(results))
            return results

        except requests.exceptions.RequestException as e:
            logger.error("[Tutor] YouTube search failed: %s", e)
            return []
        except Exception as e:
            logger.error("[Tutor] YouTube parse failed: %s", e)
            return []

    def _search_articles(self, skill: str, max_results: int = 4) -> list:
        try:
            params = {
                "engine": "google",
                "q": f"learn {skill} free tutorial documentation course 2025",
                "api_key": self.api_key,
                "num": 10,
                "hl": "en",
                "gl": "us",
            }
            r = requests.get(self.base_url, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()

            results = []
            for item in data.get("organic_results", []):
                url = item.get("link", "")
                if not url:
                    continue

                if "youtube.com" in url or "youtu.be" in url:
                    continue

                results.append(
                    {
                        "title": item.get("title", ""),
                        "url": url,
                        "source": self._extract_domain(url),
                        "description": item.get("snippet", "")[:150],
                    }
                )

            def trust_score(r):
                u = r.get("url", "")
                for i, src in enumerate(TRUSTED_SOURCES):
                    if src in u:
                        return i
                return 999

            results.sort(key=trust_score)

            logger.info(
                "[Tutor] Article search for '%s': %d results",
                skill,
                len(results[:max_results]),
            )
            return results[:max_results]

        except requests.exceptions.RequestException as e:
            logger.error("[Tutor] Article search failed: %s", e)
            return []
        except Exception as e:
            logger.error("[Tutor] Article parse failed: %s", e)
            return []

    def open_url(self, url: str) -> str:
        try:
            if not url.startswith(("http://", "https://")):
                return "Invalid URL — must start with http or https."
            webbrowser.open(url)
            domain = self._extract_domain(url)
            logger.info("[Tutor] Opened URL: %s", url)
            return f"Opened {domain} in your browser."
        except Exception as e:
            logger.error("[Tutor] Open URL failed: %s", e)
            return f"Could not open the URL: {e}"

    def format_resources_for_chat(self, resources: dict) -> str:
        skill = resources["skill"]
        youtube = resources.get("youtube", [])
        articles = resources.get("articles", [])

        if resources.get("error"):
            return f"Sorry, I couldn't search for {skill} resources: {resources['error']}"

        if resources["total"] == 0:
            return (
                f"I couldn't find any resources for **{skill}** right now. "
                f"Try asking for a slightly different term."
            )

        lines = [f"Here are the best resources I found for learning **{skill}**:\n"]

        if youtube:
            lines.append("\U0001f3a5 **YouTube Tutorials:**")
            for i, v in enumerate(youtube, 1):
                meta_parts = []
                if v.get("channel"):
                    meta_parts.append(v["channel"])
                if v.get("duration"):
                    meta_parts.append(v["duration"])
                if v.get("views"):
                    meta_parts.append(v["views"])
                meta = " \u00b7 ".join(meta_parts)

                lines.append(f"{i}. [{v['title']}]({v['url']})")
                if meta:
                    lines.append(f"   _{meta}_")
                if v.get("description"):
                    lines.append(f"   {v['description'][:100]}...")
            lines.append("")

        if articles:
            lines.append("\U0001f4da **Articles & Documentation:**")
            for i, a in enumerate(articles, 1):
                lines.append(f"{i}. [{a['title']}]({a['url']})")
                desc = f" \u2014 {a['description'][:80]}..." if a.get("description") else ""
                lines.append(f"   _{a['source']}_{desc}")
            lines.append("")

        lines.append(
            '\U0001f4a1 _Want me to open any of these? Say "open the first YouTube video" '
            'or "open link 2". Or say "test me on '
            + skill
            + '" when you\'re ready to check your knowledge._'
        )

        return "\n".join(lines)

    def _extract_domain(self, url: str) -> str:
        match = re.search(r"https?://(?:www\.)?([^/]+)", url)
        return match.group(1) if match else url


tutor = LearningTutor()
