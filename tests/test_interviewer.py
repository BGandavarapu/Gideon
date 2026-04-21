"""Tests for Interviewer — question generation, grading, formatting, and API routes."""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from web.interviewer import Interviewer

SAMPLE_JOB_TITLE = "Backend Engineer"
SAMPLE_COMPANY = "Stripe"

SAMPLE_QUESTIONS_15 = [
    {
        "question_number": i,
        "question_type": "behavioral" if i <= 8 else "technical",
        "question_text": f"Test question {i}?",
        "category": "Teamwork" if i <= 8 else "Python",
        "model_answer_tips": "Key points: 1) A 2) B",
    }
    for i in range(1, 16)
]

SAMPLE_FEEDBACK = {
    "score_awarded": 8,
    "feedback_strengths": "Great specifics.",
    "feedback_gaps": "",
    "feedback_suggestion": "Add more metrics.",
    "interview_signal": "yes",
}


class TestInterviewer:

    def test_interviewer_imports_cleanly(self):
        from web.interviewer import interviewer
        assert interviewer is not None

    def test_calculate_score_perfect(self):
        questions = [{"score_awarded": 10} for _ in range(15)]
        assert Interviewer().calculate_score(questions) == 100.0

    def test_calculate_score_partial(self):
        questions = [{"score_awarded": 7} for _ in range(15)]
        assert Interviewer().calculate_score(questions) == 70.0

    def test_calculate_score_zero(self):
        questions = [{"score_awarded": 0} for _ in range(15)]
        assert Interviewer().calculate_score(questions) == 0.0

    def test_calculate_score_empty(self):
        assert Interviewer().calculate_score([]) == 0.0

    def test_calculate_recommendation_thresholds(self):
        iv = Interviewer()
        assert iv.calculate_recommendation(90) == "strong_yes"
        assert iv.calculate_recommendation(85) == "strong_yes"
        assert iv.calculate_recommendation(75) == "yes"
        assert iv.calculate_recommendation(70) == "yes"
        assert iv.calculate_recommendation(60) == "maybe"
        assert iv.calculate_recommendation(50) == "maybe"
        assert iv.calculate_recommendation(40) == "no"
        assert iv.calculate_recommendation(0) == "no"

    def test_format_browse_questions_structure(self):
        iv = Interviewer()
        text = iv.format_browse_questions(
            SAMPLE_QUESTIONS_15, SAMPLE_JOB_TITLE, SAMPLE_COMPANY
        )
        assert SAMPLE_JOB_TITLE in text
        assert SAMPLE_COMPANY in text
        assert "Behavioral" in text
        assert "Technical" in text
        assert "mock interview" in text.lower()
        assert "**1.**" in text

    def test_format_browse_splits_behavioral_technical(self):
        iv = Interviewer()
        text = iv.format_browse_questions(
            SAMPLE_QUESTIONS_15, SAMPLE_JOB_TITLE, SAMPLE_COMPANY
        )
        behavioral = [q for q in SAMPLE_QUESTIONS_15 if q["question_type"] == "behavioral"]
        assert len(behavioral) == 8
        assert "**9.**" in text

    def test_format_browse_includes_answer_tips(self):
        iv = Interviewer()
        text = iv.format_browse_questions(
            SAMPLE_QUESTIONS_15, SAMPLE_JOB_TITLE, SAMPLE_COMPANY
        )
        assert "Key points" in text

    @patch("web.interviewer.Interviewer.__init__", lambda self: None)
    def test_generate_questions_mocked(self):
        iv = Interviewer()
        mock_client = MagicMock()
        iv._worker = mock_client

        response_json = json.dumps({
            "job_title": SAMPLE_JOB_TITLE,
            "company": SAMPLE_COMPANY,
            "questions": SAMPLE_QUESTIONS_15,
        })
        mock_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content=response_json))]
        )

        mock_job = MagicMock()
        mock_job.job_title = SAMPLE_JOB_TITLE
        mock_job.company_name = SAMPLE_COMPANY
        mock_job.job_description = "Build scalable APIs."
        mock_job.required_skills = ["Python", "PostgreSQL"]
        mock_job.preferred_skills = ["Go"]

        questions = iv.generate_questions(mock_job)
        assert len(questions) == 15
        behavioral = [q for q in questions if q["question_type"] == "behavioral"]
        technical = [q for q in questions if q["question_type"] == "technical"]
        assert len(behavioral) == 8
        assert len(technical) == 7

    @patch("web.interviewer.Interviewer.__init__", lambda self: None)
    def test_generate_questions_raises_on_too_few(self):
        iv = Interviewer()
        mock_client = MagicMock()
        iv._worker = mock_client

        response_json = json.dumps({"questions": SAMPLE_QUESTIONS_15[:5]})
        mock_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content=response_json))]
        )

        mock_job = MagicMock()
        mock_job.job_title = SAMPLE_JOB_TITLE
        mock_job.company_name = SAMPLE_COMPANY
        mock_job.job_description = ""
        mock_job.required_skills = []
        mock_job.preferred_skills = []

        with pytest.raises(ValueError):
            iv.generate_questions(mock_job)

    @patch("web.interviewer.Interviewer.__init__", lambda self: None)
    def test_grade_answer_mocked(self):
        iv = Interviewer()
        mock_client = MagicMock()
        iv._worker = mock_client

        grade_json = json.dumps(SAMPLE_FEEDBACK)
        mock_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content=grade_json))]
        )

        result = iv.grade_answer(
            SAMPLE_QUESTIONS_15[0],
            "I worked with a team to deliver a key project on time.",
            SAMPLE_JOB_TITLE,
            SAMPLE_COMPANY,
        )
        assert "score_awarded" in result
        assert "feedback_strengths" in result
        assert "feedback_gaps" in result
        assert "feedback_suggestion" in result
        assert 0 <= result["score_awarded"] <= 10

    @patch("web.interviewer.Interviewer.__init__", lambda self: None)
    def test_grade_answer_returns_fallback_on_error(self):
        iv = Interviewer()
        mock_client = MagicMock()
        iv._worker = mock_client
        mock_client.chat.completions.create.side_effect = Exception("NIM error")

        result = iv.grade_answer(
            SAMPLE_QUESTIONS_15[0], "Some answer", SAMPLE_JOB_TITLE, SAMPLE_COMPANY
        )
        assert result["score_awarded"] == 5
        assert "feedback_strengths" in result

    def test_format_mock_feedback_with_next(self):
        iv = Interviewer()
        text = iv.format_mock_feedback(
            SAMPLE_QUESTIONS_15[0],
            SAMPLE_FEEDBACK,
            1,
            15,
            SAMPLE_QUESTIONS_15[1],
        )
        assert "Feedback on Q1" in text
        assert "8/10" in text
        assert "Question 2/15" in text

    def test_format_mock_feedback_final_question(self):
        iv = Interviewer()
        text = iv.format_mock_feedback(
            SAMPLE_QUESTIONS_15[14],
            SAMPLE_FEEDBACK,
            15,
            15,
            None,
        )
        assert "last question" in text.lower() or "Calculating" in text

    def test_format_final_results_structure(self):
        iv = Interviewer()
        graded = [{**q, "score_awarded": 7} for q in SAMPLE_QUESTIONS_15]
        text = iv.format_final_results(
            SAMPLE_JOB_TITLE, SAMPLE_COMPANY,
            70.0, "yes",
            "Good performance overall.",
            graded,
        )
        assert "Mock Interview Complete" in text
        assert "70" in text
        assert "Hire" in text
        assert SAMPLE_JOB_TITLE in text
        assert SAMPLE_COMPANY in text

    def test_format_final_results_strong_hire(self):
        iv = Interviewer()
        graded = [{**q, "score_awarded": 9} for q in SAMPLE_QUESTIONS_15]
        text = iv.format_final_results(
            SAMPLE_JOB_TITLE, SAMPLE_COMPANY,
            90.0, "strong_yes",
            "Excellent!",
            graded,
        )
        assert "Strong Hire" in text


class TestInterviewAPI:

    @pytest.fixture()
    def client(self, tmp_path, monkeypatch):
        db_url = f"sqlite:///{tmp_path / 'test_interview.db'}"
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        from database.database import reset_manager, create_tables
        reset_manager(db_url)
        create_tables()
        from web.app import app
        app.config["TESTING"] = True
        with app.test_client() as c:
            yield c
        reset_manager(None)

    def _create_test_job(self, db_url: str) -> int:
        """Insert a minimal analyzed Job and return its id."""
        from database.database import get_db
        from database.models import Job
        with get_db() as db:
            job = Job(
                job_title=SAMPLE_JOB_TITLE,
                company_name=SAMPLE_COMPANY,
                job_description="Build scalable APIs with Python.",
                application_url=f"https://example.com/job/{id(db_url)}",
                source="linkedin",
                status="analyzed",
                required_skills=["Python", "PostgreSQL"],
                preferred_skills=["Go"],
            )
            db.add(job)
            db.commit()
            return job.id

    @patch("web.interviewer.interviewer.generate_questions", return_value=SAMPLE_QUESTIONS_15)
    def test_api_start_interview_browse(self, mock_gen, client, tmp_path):
        job_id = self._create_test_job(str(tmp_path))
        resp = client.post(
            "/api/interview/start",
            data=json.dumps({"session_id": "test-sess", "job_id": job_id, "mode": "browse"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["mode"] == "browse"
        assert "interview_session_id" in data
        assert "questions_formatted" in data
        assert SAMPLE_JOB_TITLE in data["questions_formatted"]

    @patch("web.interviewer.interviewer.generate_mock_intro", return_value="Welcome to the interview!")
    @patch("web.interviewer.interviewer.generate_questions", return_value=SAMPLE_QUESTIONS_15)
    def test_api_start_interview_mock(self, mock_gen, mock_intro, client, tmp_path):
        job_id = self._create_test_job(str(tmp_path))
        resp = client.post(
            "/api/interview/start",
            data=json.dumps({"session_id": "test-sess2", "job_id": job_id, "mode": "mock"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["mode"] == "mock"
        assert "interview_session_id" in data
        assert "intro" in data
        assert "first_question" in data
        assert data["question_number"] == 1
        assert data["total_questions"] == 15

    def test_api_start_interview_invalid_job(self, client):
        resp = client.post(
            "/api/interview/start",
            data=json.dumps({"session_id": "x", "job_id": 99999, "mode": "browse"}),
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_api_start_interview_missing_job_id(self, client):
        resp = client.post(
            "/api/interview/start",
            data=json.dumps({"session_id": "x", "mode": "browse"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    @patch("web.interviewer.interviewer.grade_answer", return_value=SAMPLE_FEEDBACK)
    @patch("web.interviewer.interviewer.generate_questions", return_value=SAMPLE_QUESTIONS_15)
    def test_api_answer_continue(self, mock_gen, mock_grade, client, tmp_path):
        job_id = self._create_test_job(str(tmp_path))
        start_resp = client.post(
            "/api/interview/start",
            data=json.dumps({"session_id": "test-ans", "job_id": job_id, "mode": "mock"}),
            content_type="application/json",
        )
        sid = start_resp.get_json()["interview_session_id"]

        resp = client.post(
            f"/api/interview/{sid}/answer",
            data=json.dumps({"answer": "My detailed answer here.", "session_id": "test-ans"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "continue"
        assert "feedback" in data
        assert data["question_number"] == 2

    @patch("web.interviewer.interviewer.generate_final_summary", return_value="Great job!")
    @patch("web.interviewer.interviewer.grade_answer", return_value=SAMPLE_FEEDBACK)
    @patch("web.interviewer.interviewer.generate_questions", return_value=SAMPLE_QUESTIONS_15)
    def test_api_answer_complete(self, mock_gen, mock_grade, mock_summary, client, tmp_path):
        job_id = self._create_test_job(str(tmp_path))
        start_resp = client.post(
            "/api/interview/start",
            data=json.dumps({"session_id": "test-complete", "job_id": job_id, "mode": "mock"}),
            content_type="application/json",
        )
        sid = start_resp.get_json()["interview_session_id"]

        last_resp = None
        for _ in range(15):
            last_resp = client.post(
                f"/api/interview/{sid}/answer",
                data=json.dumps({"answer": "My answer.", "session_id": "test-complete"}),
                content_type="application/json",
            )

        assert last_resp is not None
        data = last_resp.get_json()
        assert data["status"] == "completed"
        assert "score" in data
        assert "recommendation" in data
        assert "results" in data

    @patch("web.interviewer.interviewer.generate_questions", return_value=SAMPLE_QUESTIONS_15)
    def test_api_active_interview_found(self, mock_gen, client, tmp_path):
        job_id = self._create_test_job(str(tmp_path))
        client.post(
            "/api/interview/start",
            data=json.dumps({"session_id": "active-sess", "job_id": job_id, "mode": "mock"}),
            content_type="application/json",
        )
        resp = client.get("/api/interview/active/active-sess")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "interview_session" in data
        assert "current_question" in data
        assert "formatted_question" in data

    def test_api_active_interview_not_found(self, client):
        resp = client.get("/api/interview/active/nonexistent-session")
        assert resp.status_code == 404

    @patch("web.interviewer.interviewer.grade_answer", return_value={**SAMPLE_FEEDBACK, "score_awarded": 9})
    @patch("web.interviewer.interviewer.generate_final_summary", return_value="Excellent!")
    @patch("web.interviewer.interviewer.generate_questions", return_value=SAMPLE_QUESTIONS_15)
    def test_hiring_recommendation_strong_yes(self, mock_gen, mock_summary, mock_grade, client, tmp_path):
        job_id = self._create_test_job(str(tmp_path))
        start_resp = client.post(
            "/api/interview/start",
            data=json.dumps({"session_id": "strong-sess", "job_id": job_id, "mode": "mock"}),
            content_type="application/json",
        )
        sid = start_resp.get_json()["interview_session_id"]

        last_resp = None
        for _ in range(15):
            last_resp = client.post(
                f"/api/interview/{sid}/answer",
                data=json.dumps({"answer": "Excellent answer.", "session_id": "strong-sess"}),
                content_type="application/json",
            )

        data = last_resp.get_json()
        assert data["status"] == "completed"
        assert data["recommendation"] == "strong_yes"
        assert data["score"] >= 85
