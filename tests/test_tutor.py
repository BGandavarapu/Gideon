"""Tests for LearningTutor — SerpAPI-powered skill learning resources."""

from unittest.mock import MagicMock, patch

import pytest

from web.tutor import LearningTutor


class TestLearningTutor:

    def test_tutor_initializes_with_key(self, monkeypatch):
        monkeypatch.setenv("SERPAPI_KEY", "test_key_123")
        t = LearningTutor()
        assert t.api_key == "test_key_123"

    def test_tutor_warns_without_key(self, monkeypatch):
        monkeypatch.delenv("SERPAPI_KEY", raising=False)
        t = LearningTutor()
        assert t.api_key is None

    def test_extract_domain(self):
        t = LearningTutor()
        assert t._extract_domain("https://www.youtube.com/watch?v=abc") == "youtube.com"
        assert t._extract_domain("https://freecodecamp.org/learn") == "freecodecamp.org"
        assert t._extract_domain("https://developer.mozilla.org/en-US/docs/Web") == "developer.mozilla.org"

    def test_format_resources_with_results(self):
        resources = {
            "skill": "Docker",
            "youtube": [
                {
                    "title": "Docker Tutorial for Beginners",
                    "url": "https://youtube.com/watch?v=xyz",
                    "channel": "TechWorld with Nana",
                    "duration": "2:10:00",
                    "views": "3.2M views",
                    "thumbnail": "",
                    "description": "Learn Docker from scratch",
                }
            ],
            "articles": [
                {
                    "title": "Docker Get Started",
                    "url": "https://docs.docker.com/get-started",
                    "source": "docs.docker.com",
                    "description": "Official Docker documentation",
                }
            ],
            "total": 2,
        }
        t = LearningTutor()
        text = t.format_resources_for_chat(resources)
        assert "Docker" in text
        assert "YouTube" in text
        assert "youtube.com/watch?v=xyz" in text
        assert "TechWorld with Nana" in text
        assert "docs.docker.com" in text
        assert "open" in text.lower()

    def test_format_resources_no_results(self):
        resources = {"skill": "XYZ123", "youtube": [], "articles": [], "total": 0}
        t = LearningTutor()
        text = t.format_resources_for_chat(resources)
        assert "couldn't find" in text.lower()

    def test_format_resources_error_state(self):
        resources = {
            "skill": "Python",
            "youtube": [],
            "articles": [],
            "total": 0,
            "error": "SERPAPI_KEY not configured",
        }
        t = LearningTutor()
        text = t.format_resources_for_chat(resources)
        assert "SERPAPI_KEY" in text or "configured" in text.lower()

    def test_find_resources_no_key(self):
        t = LearningTutor()
        t.api_key = None
        result = t.find_resources("Python")
        assert result["total"] == 0
        assert "error" in result

    def test_open_url_invalid_scheme(self):
        t = LearningTutor()
        result = t.open_url("javascript:alert(1)")
        assert "Invalid" in result or "http" in result

    @patch("web.tutor.webbrowser.open")
    def test_open_url_valid(self, mock_wb):
        t = LearningTutor()
        result = t.open_url("https://youtube.com/watch?v=abc")
        assert mock_wb.called
        assert "Opened" in result

    @patch("web.tutor.requests.get")
    def test_serpapi_youtube_search_mocked(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "video_results": [
                {
                    "title": "Kubernetes Tutorial",
                    "link": "https://youtube.com/watch?v=k8s",
                    "channel": {"name": "TechWorld"},
                    "length": "3:22:00",
                    "views": "2M views",
                    "thumbnail": {"static": "https://img"},
                    "description": "Learn Kubernetes",
                    "type": "video",
                }
            ]
        }
        mock_get.return_value = mock_resp

        t = LearningTutor()
        t.api_key = "test_key"
        results = t._search_youtube("Kubernetes")
        assert len(results) == 1
        assert results[0]["title"] == "Kubernetes Tutorial"
        assert results[0]["url"] == "https://youtube.com/watch?v=k8s"
        assert results[0]["channel"] == "TechWorld"

    @patch("web.tutor.requests.get")
    def test_serpapi_article_search_mocked(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "organic_results": [
                {
                    "title": "Kubernetes Docs",
                    "link": "https://kubernetes.io/docs",
                    "snippet": "Official documentation",
                },
                {
                    "title": "YouTube K8s",
                    "link": "https://youtube.com/watch?v=x",
                    "snippet": "Video tutorial",
                },
            ]
        }
        mock_get.return_value = mock_resp

        t = LearningTutor()
        t.api_key = "test_key"
        results = t._search_articles("Kubernetes")
        urls = [r["url"] for r in results]
        assert not any("youtube.com" in u for u in urls)
        assert any("kubernetes.io" in u for u in urls)


class TestTutorAPI:

    @pytest.fixture()
    def client(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        from database.database import create_tables
        create_tables()
        from web.app import app
        app.config["TESTING"] = True
        with app.test_client() as c:
            yield c

    def test_api_tutor_status_configured(self, client, monkeypatch):
        monkeypatch.setenv("SERPAPI_KEY", "test_key_abc")
        resp = client.get("/api/tutor/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["configured"] is True
        assert data["provider"] == "serpapi"

    def test_api_tutor_status_not_configured(self, client, monkeypatch):
        monkeypatch.delenv("SERPAPI_KEY", raising=False)
        resp = client.get("/api/tutor/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["configured"] is False
        assert data["provider"] == "none"
