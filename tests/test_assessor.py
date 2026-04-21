"""Tests for SkillAssessor — assessment generation, grading, and API routes."""

import json
from unittest.mock import MagicMock, patch

import pytest

from web.assessor import SkillAssessor

SAMPLE_QUESTIONS = [
    {
        "question_number": i,
        "question_type": "multiple_choice" if i <= 6 else "open_ended",
        "question_text": f"Test question {i}?",
        "options": {"A": "opt1", "B": "opt2", "C": "opt3", "D": "opt4"} if i <= 6 else None,
        "correct_answer": "B" if i <= 6 else "Key point: answer",
        "key_concepts": ["concept1"],
    }
    for i in range(1, 11)
]


class TestSkillAssessor:

    def test_assessor_imports(self):
        from web.assessor import assessor
        assert assessor is not None

    def test_calculate_score_perfect(self):
        questions = [{"score_awarded": 10} for _ in range(10)]
        assert SkillAssessor().calculate_score(questions) == 100.0

    def test_calculate_score_partial(self):
        questions = [{"score_awarded": 7} for _ in range(10)]
        assert SkillAssessor().calculate_score(questions) == 70.0

    def test_calculate_score_zero(self):
        questions = [{"score_awarded": 0} for _ in range(10)]
        assert SkillAssessor().calculate_score(questions) == 0.0

    def test_format_question_mc(self):
        a = SkillAssessor()
        text = a.format_question_for_chat(SAMPLE_QUESTIONS[0], 1, 10)
        assert "Question 1/10" in text
        assert "A)" in text
        assert "B)" in text

    def test_format_question_open_ended(self):
        a = SkillAssessor()
        text = a.format_question_for_chat(SAMPLE_QUESTIONS[6], 7, 10)
        assert "Question 7/10" in text
        assert "A)" not in text
        assert "2-4 sentences" in text

    @patch("web.assessor.SkillAssessor.__init__", lambda self: None)
    def test_generate_questions_mocked(self):
        a = SkillAssessor()
        mock_client = MagicMock()
        a._client = mock_client

        response_json = json.dumps({"skill": "Python", "questions": SAMPLE_QUESTIONS})
        mock_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content=response_json))]
        )

        questions = a.generate_questions("Python")
        assert len(questions) == 10
        mc = [q for q in questions if q["question_type"] == "multiple_choice"]
        oe = [q for q in questions if q["question_type"] == "open_ended"]
        assert len(mc) == 6
        assert len(oe) == 4

    @patch("web.assessor.SkillAssessor.__init__", lambda self: None)
    def test_grade_answer_mocked(self):
        a = SkillAssessor()
        mock_client = MagicMock()
        a._client = mock_client

        grade_json = json.dumps({
            "is_correct": True,
            "score_awarded": 10,
            "feedback": "Correct!",
            "key_concept_tested": "basics",
        })
        mock_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content=grade_json))]
        )

        result = a.grade_answer(SAMPLE_QUESTIONS[0], "B")
        assert result["score_awarded"] == 10
        assert result["is_correct"] is True

    def test_format_results_contains_score(self):
        graded = [
            {**q, "score_awarded": 7, "is_correct": True, "feedback": "Good answer"}
            for q in SAMPLE_QUESTIONS
        ]
        a = SkillAssessor()
        text = a.format_results_for_chat("Python", 70.0, graded, "Well done!", [])
        assert "70" in text
        assert "Python" in text
        assert "Assessment Complete" in text


class TestAssessmentAPI:

    @pytest.fixture()
    def client(self, tmp_path, monkeypatch):
        db_url = f"sqlite:///{tmp_path / 'test_assess.db'}"
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        from database.database import reset_manager, create_tables
        reset_manager(db_url)
        create_tables()
        from web.app import app
        app.config["TESTING"] = True
        with app.test_client() as c:
            yield c
        reset_manager(None)

    @patch("web.assessor.assessor.generate_questions", return_value=SAMPLE_QUESTIONS)
    def test_api_start_assessment(self, mock_gen, client):
        resp = client.post(
            "/api/assessment/start",
            data=json.dumps({"session_id": "test-sess", "skill": "Python"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "assessment_id" in data
        assert "question" in data
        assert "Question 1/10" in data["question"]

    @patch("web.assessor.assessor.generate_questions", return_value=SAMPLE_QUESTIONS)
    @patch("web.assessor.assessor.grade_answer", return_value={
        "is_correct": True, "score_awarded": 10,
        "feedback": "Correct!", "key_concept_tested": "basics",
    })
    def test_api_answer_continue(self, mock_grade, mock_gen, client):
        start_resp = client.post(
            "/api/assessment/start",
            data=json.dumps({"session_id": "test-sess2", "skill": "SQL"}),
            content_type="application/json",
        )
        aid = start_resp.get_json()["assessment_id"]

        resp = client.post(
            f"/api/assessment/{aid}/answer",
            data=json.dumps({"answer": "B", "session_id": "test-sess2"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "continue"
        assert "question" in data
        assert "Question 2/10" in data["question"]

    @patch("web.assessor.assessor.generate_questions", return_value=SAMPLE_QUESTIONS)
    @patch("web.assessor.assessor.grade_answer", return_value={
        "is_correct": True, "score_awarded": 10,
        "feedback": "Correct!", "key_concept_tested": "basics",
    })
    @patch("web.assessor.assessor.generate_summary", return_value="Great job overall!")
    def test_api_answer_complete(self, mock_summary, mock_grade, mock_gen, client):
        start_resp = client.post(
            "/api/assessment/start",
            data=json.dumps({"session_id": "test-sess3", "skill": "Go"}),
            content_type="application/json",
        )
        aid = start_resp.get_json()["assessment_id"]

        for i in range(10):
            resp = client.post(
                f"/api/assessment/{aid}/answer",
                data=json.dumps({"answer": "B", "session_id": "test-sess3"}),
                content_type="application/json",
            )

        data = resp.get_json()
        assert data["status"] == "completed"
        assert "score" in data
        assert "results" in data
        assert data["score"] == 100.0

    @patch("web.assessor.assessor.generate_questions", return_value=SAMPLE_QUESTIONS)
    def test_api_active_assessment_found(self, mock_gen, client):
        start_resp = client.post(
            "/api/assessment/start",
            data=json.dumps({"session_id": "test-active", "skill": "Docker"}),
            content_type="application/json",
        )
        assert start_resp.status_code == 200

        resp = client.get("/api/assessment/active/test-active")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "assessment" in data
        assert "formatted_question" in data

    def test_api_active_assessment_not_found(self, client):
        resp = client.get("/api/assessment/active/nonexistent")
        assert resp.status_code == 404
