"""SkillAssessor — generates, administers, and grades skill assessments via NVIDIA NIM."""

from __future__ import annotations

import json
import logging
import os
from typing import List

logger = logging.getLogger(__name__)

_NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
_WORKER_MODEL = "nvidia/llama-3.3-nemotron-super-49b-v1.5"

QUESTION_GENERATION_PROMPT = """\
You are an expert technical interviewer and educator.
Generate exactly 10 assessment questions for the skill: {skill}

Requirements:
- Questions 1-6: Multiple choice (test conceptual understanding)
- Questions 7-10: Open ended (test practical application)
- Difficulty progression: Q1-3 beginner, Q4-7 intermediate, Q8-10 advanced
- Questions must be specific and unambiguous
- Multiple choice must have exactly 4 options (A, B, C, D)
- Only one correct answer per MC question
- Open ended questions should require 2-4 sentences to answer well

Return ONLY valid JSON in this exact format, nothing else:
{{
  "skill": "{skill}",
  "questions": [
    {{
      "question_number": 1,
      "question_type": "multiple_choice",
      "question_text": "What is...?",
      "options": {{
        "A": "First option",
        "B": "Second option",
        "C": "Third option",
        "D": "Fourth option"
      }},
      "correct_answer": "B",
      "key_concepts": ["concept1", "concept2"]
    }},
    {{
      "question_number": 7,
      "question_type": "open_ended",
      "question_text": "Explain how you would...?",
      "options": null,
      "correct_answer": "Key points: 1) ... 2) ... 3) ...",
      "key_concepts": ["concept1", "concept2"]
    }}
  ]
}}
"""

GRADING_PROMPT = """\
You are an expert technical interviewer grading an assessment.
Skill being tested: {skill}

Grade the following answer and return ONLY valid JSON:

Question {question_number}: {question_text}
Question type: {question_type}
{options_text}
Correct answer / key points: {correct_answer}
User's answer: {user_answer}

Return ONLY this JSON:
{{
  "is_correct": true,
  "score_awarded": 10,
  "feedback": "2-3 sentences of specific feedback explaining what was right/wrong and why",
  "key_concept_tested": "the main concept this question tested"
}}

Scoring guide:
- Multiple choice: 10 if correct, 0 if wrong
- Open ended:
  0-3: Missing key concepts entirely
  4-6: Partially correct, missing important points
  7-9: Good answer with minor gaps
  10: Complete, accurate, well-explained answer
"""

RESULTS_SUMMARY_PROMPT = """\
You are Gideon, a personal career agent giving feedback on a skills assessment.

Skill assessed: {skill}
Final score: {score}/100
Questions and results: {results_summary}

Write an encouraging but honest assessment summary that:
1. Opens with their score and a brief overall verdict
2. Highlights 1-2 things they did well
3. Identifies 2-3 specific weak areas to improve
4. Gives 1 actionable study tip
5. Ends with encouragement

Keep it conversational, warm, and specific.
Maximum 200 words. Do NOT use bullet points — write in flowing paragraphs.
"""


class SkillAssessor:

    def __init__(self) -> None:
        key = os.getenv("NVIDIA_API_KEY")
        self._client = None
        if key:
            try:
                from openai import OpenAI
                self._client = OpenAI(base_url=_NVIDIA_BASE_URL, api_key=key)
            except Exception as exc:
                logger.warning("Failed to initialise assessor NIM client: %s", exc)

    def generate_questions(self, skill: str) -> list:
        if self._client is None:
            raise ValueError("NVIDIA_API_KEY not configured")

        prompt = QUESTION_GENERATION_PROMPT.format(skill=skill)

        try:
            response = self._client.chat.completions.create(
                model=_WORKER_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=3000,
            )
            raw = response.choices[0].message.content.strip()

            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1]
                raw = raw.rsplit("```", 1)[0]

            data = json.loads(raw)
            questions = data.get("questions", [])

            if len(questions) != 10:
                raise ValueError(f"Expected 10 questions, got {len(questions)}")

            logger.info("[Assessor] Generated 10 questions for skill: %s", skill)
            return questions

        except json.JSONDecodeError as e:
            logger.error("[Assessor] JSON parse failed: %s", e)
            raise ValueError(f"Failed to generate valid questions for {skill}")

    def grade_answer(self, question: dict, user_answer: str) -> dict:
        if self._client is None:
            return {
                "is_correct": False,
                "score_awarded": 0,
                "feedback": "Grading unavailable — AI not configured.",
                "key_concept_tested": "",
            }

        options_text = ""
        if question.get("question_type") == "multiple_choice" and question.get("options"):
            opts = question["options"]
            options_text = "Options:\n" + "\n".join(f"{k}: {v}" for k, v in opts.items())

        prompt = GRADING_PROMPT.format(
            skill=question.get("skill", ""),
            question_number=question.get("question_number", "?"),
            question_text=question.get("question_text", ""),
            question_type=question.get("question_type", ""),
            options_text=options_text,
            correct_answer=question.get("correct_answer", ""),
            user_answer=user_answer,
        )

        try:
            response = self._client.chat.completions.create(
                model=_WORKER_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=500,
            )
            raw = response.choices[0].message.content.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1]
                raw = raw.rsplit("```", 1)[0]

            return json.loads(raw)

        except Exception as e:
            logger.error("[Assessor] Grading failed: %s", e)
            return {
                "is_correct": False,
                "score_awarded": 0,
                "feedback": "Could not grade this answer.",
                "key_concept_tested": "",
            }

    def generate_summary(self, skill: str, score: float, questions: list) -> str:
        if self._client is None:
            return f"Assessment complete! You scored {round(score)}/100 on {skill}."

        results_summary = "\n".join(
            f"Q{q.get('question_number')}: "
            f"{q.get('question_text', '')[:60]}... "
            f"Score: {q.get('score_awarded', 0)}/10"
            for q in questions
        )

        prompt = RESULTS_SUMMARY_PROMPT.format(
            skill=skill, score=round(score), results_summary=results_summary,
        )

        try:
            response = self._client.chat.completions.create(
                model=_WORKER_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=400,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error("[Assessor] Summary generation failed: %s", e)
            return f"Assessment complete! You scored {round(score)}/100 on {skill}."

    def calculate_score(self, graded_questions: list) -> float:
        total = sum(q.get("score_awarded", 0) for q in graded_questions)
        return min(total, 100.0)

    def format_question_for_chat(
        self, question: dict, current: int, total: int = 10
    ) -> str:
        qtype = question.get("question_type", "")
        qtext = question.get("question_text", "")
        options = question.get("options", {})

        lines = [f"**Question {current}/{total}**", ""]

        if qtype == "multiple_choice":
            lines.append(qtext)
            lines.append("")
            for letter, text in (options or {}).items():
                lines.append(f"**{letter})** {text}")
            lines.append("")
            lines.append("_Type A, B, C, or D to answer_")
        else:
            lines.append(qtext)
            lines.append("")
            lines.append("_Type your answer below (2-4 sentences recommended)_")

        return "\n".join(lines)

    def format_results_for_chat(
        self,
        skill: str,
        score: float,
        graded_questions: list,
        summary: str,
        weak_areas: list,
    ) -> str:
        score_int = round(score)

        if score_int >= 85:
            emoji, verdict = "\U0001f3c6", "Excellent!"
        elif score_int >= 70:
            emoji, verdict = "\u2705", "Good job!"
        elif score_int >= 50:
            emoji, verdict = "\U0001f4c8", "Keep practicing!"
        else:
            emoji, verdict = "\U0001f4da", "More study needed"

        lines = [
            f"{emoji} **Assessment Complete \u2014 {skill}**",
            f"## Score: {score_int}/100 \u2014 {verdict}",
            "",
            "---",
            "",
            "**Question by question:**",
            "",
        ]

        for q in graded_questions:
            num = q.get("question_number", "?")
            qtext = q.get("question_text", "")[:60]
            awarded = q.get("score_awarded", 0)
            correct = q.get("is_correct", False)
            feedback = q.get("feedback", "")

            if correct or awarded >= 7:
                icon = "\u2705"
            elif awarded <= 3:
                icon = "\u274c"
            else:
                icon = "\u26a0\ufe0f"

            lines.append(f"{icon} **Q{num}** ({awarded}/10): {qtext}...")
            if feedback:
                lines.append(f"   _{feedback}_")
            lines.append("")

        lines.append("---")
        lines.append("")
        lines.append("**Overall feedback:**")
        lines.append("")
        lines.append(summary)

        if weak_areas:
            lines.append("")
            lines.append("\U0001f3af **Recommended resources for your weak areas:**")
            for area in weak_areas:
                lines.append(f"\u2022 {area}")

        return "\n".join(lines)


assessor = SkillAssessor()
