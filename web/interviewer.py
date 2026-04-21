"""Interviewer — generates, administers, and grades job interview prep sessions via NVIDIA NIM."""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

_NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
_WORKER_MODEL = "nvidia/llama-3.3-nemotron-super-49b-v1.5"
_ORCHESTRATOR_MODEL = "nvidia/nemotron-3-super-120b-a12b"

QUESTION_GENERATION_PROMPT = """\
You are an expert technical recruiter and hiring manager.
Generate exactly 15 interview questions for this role:

Job Title: {job_title}
Company: {company}
Job Description: {job_description}
Required Skills: {required_skills}
Preferred Skills: {preferred_skills}

Requirements:
- Questions 1-8: Behavioral (situational, past experience, soft skills, leadership,
  teamwork, problem solving). Format: "Tell me about a time...",
  "Describe a situation where...", "How do you handle...", "Give me an example..."
- Questions 9-15: Technical (based on required_skills, specific to this role,
  testing real knowledge)
- Difficulty: Q1-5 common/easy, Q6-10 medium, Q11-15 hard
- Each question must have model_answer_tips explaining what a strong answer should
  include (2-3 key points)
- Each question must have a category label

Return ONLY valid JSON, nothing else:
{{
  "job_title": "{job_title}",
  "company": "{company}",
  "questions": [
    {{
      "question_number": 1,
      "question_type": "behavioral",
      "question_text": "Tell me about a time...",
      "category": "Teamwork",
      "model_answer_tips": "Strong answers should include: 1) specific situation 2) your role 3) outcome"
    }},
    {{
      "question_number": 9,
      "question_type": "technical",
      "question_text": "Explain how...",
      "category": "Python",
      "model_answer_tips": "Cover: 1) concept definition 2) practical example 3) trade-offs"
    }}
  ]
}}
"""

ANSWER_FEEDBACK_PROMPT = """\
You are an experienced interviewer giving feedback on an interview answer.

Role: {job_title} at {company}
Question ({question_type}): {question_text}
Category: {category}
What a strong answer includes: {model_answer_tips}
Candidate's answer: {user_answer}

Evaluate this answer and return ONLY valid JSON:
{{
  "score_awarded": 0,
  "feedback_strengths": "1-2 sentences on what was good",
  "feedback_gaps": "1-2 sentences on what was missing or weak. Empty string if nothing missing.",
  "feedback_suggestion": "1 specific sentence on how to improve this answer",
  "interview_signal": "strong_yes"
}}

score_awarded must be an integer 0-10.
interview_signal must be one of: strong_yes, yes, maybe, no

Scoring guide:
0-3: Vague, off-topic, or missing key elements
4-6: Partially addresses the question, lacks specifics
7-8: Good answer, covers main points, minor gaps
9-10: Excellent, specific, structured, complete
"""

MOCK_INTERVIEW_INTRO_PROMPT = """\
You are Gideon acting as a professional interviewer at {company} for the {job_title} role.

Write a brief, professional interview opening (3-4 sentences):
- Welcome the candidate warmly
- Mention the role they are interviewing for
- Set expectations (we'll go through some questions, take your time, be specific with examples)
- Ask them to start by introducing themselves briefly

Be conversational and encouraging, not robotic.
"""

FINAL_SUMMARY_PROMPT = """\
You are Gideon giving a final interview performance review.

Role: {job_title} at {company}
Overall score: {score}/100
Hiring signal: {recommendation}
Questions and scores:
{results_summary}

Write an honest, specific, encouraging performance review (250 words max) that includes:
1. Overall verdict with score
2. Top 2 strongest moments
3. Top 2 areas to work on before the real interview
4. One specific tip to dramatically improve performance
5. Encouragement to keep practicing

Write in flowing paragraphs, not bullet points.
Sound like a mentor who wants them to succeed.
"""


class Interviewer:

    def __init__(self) -> None:
        key = os.getenv("NVIDIA_API_KEY")
        self._worker = None
        self._orchestrator = None
        if key:
            try:
                from openai import OpenAI
                self._worker = OpenAI(base_url=_NVIDIA_BASE_URL, api_key=key)
                self._orchestrator = OpenAI(base_url=_NVIDIA_BASE_URL, api_key=key)
            except Exception as exc:
                logger.warning("Failed to initialise interviewer NIM client: %s", exc)

    def generate_questions(self, job) -> list:
        """Generate 15 interview questions for a job. Raises ValueError on failure."""
        if self._worker is None:
            raise ValueError("NVIDIA_API_KEY not configured")

        required = ", ".join(job.required_skills or [])
        preferred = ", ".join(job.preferred_skills or [])
        jd = (job.job_description or "")[:2000]

        prompt = QUESTION_GENERATION_PROMPT.format(
            job_title=job.job_title,
            company=job.company_name,
            job_description=jd,
            required_skills=required,
            preferred_skills=preferred,
        )

        try:
            response = self._worker.chat.completions.create(
                model=_WORKER_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=4000,
            )
            raw = response.choices[0].message.content.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1]
                raw = raw.rsplit("```", 1)[0]

            data = json.loads(raw)
            questions = data.get("questions", [])

            if len(questions) < 10:
                raise ValueError(f"Only got {len(questions)} questions")

            logger.info(
                "[Interviewer] Generated %d questions for %s at %s",
                len(questions), job.job_title, job.company_name,
            )
            return questions[:15]

        except json.JSONDecodeError as e:
            logger.error("[Interviewer] JSON parse failed: %s", e)
            raise ValueError("Failed to generate interview questions")

    def grade_answer(
        self,
        question: dict,
        user_answer: str,
        job_title: str,
        company: str,
    ) -> dict:
        """Grade a single interview answer. Returns a safe fallback dict on failure."""
        if self._worker is None:
            return {
                "score_awarded": 5,
                "feedback_strengths": "Answer received.",
                "feedback_gaps": "",
                "feedback_suggestion": "Be more specific with examples.",
                "interview_signal": "maybe",
            }

        prompt = ANSWER_FEEDBACK_PROMPT.format(
            job_title=job_title,
            company=company,
            question_type=question.get("question_type", ""),
            question_text=question.get("question_text", ""),
            category=question.get("category", ""),
            model_answer_tips=question.get("model_answer_tips", ""),
            user_answer=user_answer,
        )

        try:
            response = self._worker.chat.completions.create(
                model=_WORKER_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=600,
            )
            raw = response.choices[0].message.content.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1]
                raw = raw.rsplit("```", 1)[0]

            return json.loads(raw)

        except Exception as e:
            logger.error("[Interviewer] Grading failed: %s", e)
            return {
                "score_awarded": 5,
                "feedback_strengths": "Answer received.",
                "feedback_gaps": "",
                "feedback_suggestion": "Be more specific with examples.",
                "interview_signal": "maybe",
            }

    def generate_mock_intro(self, job_title: str, company: str) -> str:
        """Generate interviewer opening statement. Returns a hardcoded fallback on failure."""
        if self._orchestrator is None:
            return (
                f"Welcome! I'm interviewing you today for the {job_title} role at {company}. "
                f"Let's get started. Could you begin by telling me a bit about yourself?"
            )

        prompt = MOCK_INTERVIEW_INTRO_PROMPT.format(job_title=job_title, company=company)
        try:
            response = self._orchestrator.chat.completions.create(
                model=_ORCHESTRATOR_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.8,
                max_tokens=300,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error("[Interviewer] Intro generation failed: %s", e)
            return (
                f"Welcome! I'm interviewing you today for the {job_title} role at {company}. "
                f"Let's get started. Could you begin by telling me a bit about yourself?"
            )

    def generate_final_summary(
        self,
        job_title: str,
        company: str,
        score: float,
        recommendation: str,
        questions: list,
    ) -> str:
        """Generate final performance summary. Returns a score string fallback on failure."""
        if self._orchestrator is None:
            return f"Interview complete! You scored {round(score)}/100. Keep practicing!"

        results_summary = "\n".join(
            f"Q{q.get('question_number')} ({q.get('question_type', '')}) — "
            f"{q.get('category', '')} — Score: {q.get('score_awarded', 0)}/10"
            for q in questions
        )

        prompt = FINAL_SUMMARY_PROMPT.format(
            job_title=job_title,
            company=company,
            score=round(score),
            recommendation=recommendation,
            results_summary=results_summary,
        )

        try:
            response = self._orchestrator.chat.completions.create(
                model=_ORCHESTRATOR_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=500,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error("[Interviewer] Summary generation failed: %s", e)
            return f"Interview complete! You scored {round(score)}/100. Keep practicing!"

    def calculate_score(self, graded_questions: list) -> float:
        """Calculate score out of 100 from graded question list."""
        if not graded_questions:
            return 0.0
        total = sum(q.get("score_awarded", 0) for q in graded_questions)
        max_score = len(graded_questions) * 10
        return round((total / max_score) * 100, 1)

    def calculate_recommendation(self, score: float) -> str:
        """Map score to hiring recommendation label."""
        if score >= 85:
            return "strong_yes"
        if score >= 70:
            return "yes"
        if score >= 50:
            return "maybe"
        return "no"

    def format_browse_questions(
        self, questions: list, job_title: str, company: str
    ) -> str:
        """Format 15 questions as a browsable markdown list for chat."""
        behavioral = [q for q in questions if q.get("question_type") == "behavioral"]
        technical = [q for q in questions if q.get("question_type") == "technical"]

        lines = [
            f"**Interview Questions — {job_title} at {company}**\n",
            "Here are 15 likely questions for this role. You can practice any of them by "
            "saying **'practice question 3'** or start a full mock interview by saying "
            "**'start mock interview'**.\n",
            "---\n",
            "**🧠 Behavioral Questions (1-8):**\n",
        ]

        for q in behavioral:
            num = q.get("question_number", "?")
            cat = q.get("category", "")
            text = q.get("question_text", "")
            tips = q.get("model_answer_tips", "")
            lines.append(f"**{num}.** [{cat}] {text}")
            if tips:
                lines.append(f"   💡 _Strong answers include: {tips}_\n")

        lines.append("\n**⚙️ Technical Questions (9-15):**\n")

        for q in technical:
            num = q.get("question_number", "?")
            cat = q.get("category", "")
            text = q.get("question_text", "")
            tips = q.get("model_answer_tips", "")
            lines.append(f"**{num}.** [{cat}] {text}")
            if tips:
                lines.append(f"   💡 _Strong answers include: {tips}_\n")

        lines.append("---")
        lines.append(
            "_Say **'practice question N'** to get feedback on a specific answer, or "
            "**'start mock interview'** to do the full simulation._"
        )

        return "\n".join(lines)

    def format_mock_feedback(
        self,
        question: dict,
        feedback: dict,
        question_number: int,
        total: int,
        next_question: dict = None,
    ) -> str:
        """Format per-answer feedback for the mock interview flow."""
        score = feedback.get("score_awarded", 0)
        strengths = feedback.get("feedback_strengths", "")
        gaps = feedback.get("feedback_gaps", "")
        suggestion = feedback.get("feedback_suggestion", "")

        if score >= 8:
            score_label = "💪 Strong answer"
        elif score >= 6:
            score_label = "👍 Good answer"
        elif score >= 4:
            score_label = "📈 Developing"
        else:
            score_label = "📚 Needs work"

        lines = [f"**Feedback on Q{question_number}:** {score_label} ({score}/10)\n"]

        if strengths:
            lines.append(f"✅ **Strong:** {strengths}")
        if gaps:
            lines.append(f"⚠️ **Missing:** {gaps}")
        if suggestion:
            lines.append(f"💡 **Tip:** {suggestion}")

        if next_question:
            next_num = next_question.get("question_number", question_number + 1)
            next_text = next_question.get("question_text", "")
            next_type = next_question.get("question_type", "")
            type_label = "🧠 Behavioral" if next_type == "behavioral" else "⚙️ Technical"
            lines.append(
                f"\n---\n"
                f"**Question {next_num}/{total}** {type_label}\n\n"
                f"{next_text}"
            )
        else:
            lines.append(
                "\n_That was the last question! Calculating your results..._"
            )

        return "\n".join(lines)

    def format_final_results(
        self,
        job_title: str,
        company: str,
        score: float,
        recommendation: str,
        summary: str,
        questions: list,
    ) -> str:
        """Format complete interview results for chat."""
        rec_labels = {
            "strong_yes": "🏆 Strong Hire",
            "yes": "✅ Hire",
            "maybe": "⚠️ Borderline",
            "no": "❌ Not Ready Yet",
        }
        rec_label = rec_labels.get(recommendation, recommendation)
        score_int = round(score)

        lines = [
            "## 🎤 Mock Interview Complete\n",
            f"**Role:** {job_title} at {company}",
            f"**Score:** {score_int}/100",
            f"**Hiring Signal:** {rec_label}\n",
            "---\n",
            "**Question Breakdown:**\n",
        ]

        for q in questions:
            num = q.get("question_number", "?")
            qtype = q.get("question_type", "")
            cat = q.get("category", "")
            awarded = q.get("score_awarded", 0)
            icon = "✅" if awarded >= 8 else "⚠️" if awarded >= 5 else "❌"
            type_icon = "🧠" if qtype == "behavioral" else "⚙️"
            lines.append(f"{icon} {type_icon} Q{num} [{cat}]: {awarded}/10")

        lines.append("\n---\n")
        lines.append("**Overall Feedback:**\n")
        lines.append(summary)
        lines.append(
            "\n---\n"
            "_Want to try again? Say **'start mock interview'** to redo this._"
        )

        return "\n".join(lines)


# Module-level singleton
interviewer = Interviewer()
