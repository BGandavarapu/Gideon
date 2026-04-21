"""GideonAgent — conversational AI agent for the Gideon job pipeline.

Uses NVIDIA NIM's Nemotron model with native function calling to orchestrate
scraping, analysis, resume generation, and status queries through natural
language.  Tool execution calls existing Flask API endpoints internally via
``test_client()``.

Sessions are persisted to SQLite (``chat_sessions`` / ``chat_messages`` tables)
and cached in memory for the duration of the server process.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
_NVIDIA_ORCHESTRATOR_MODEL = "nvidia/nemotron-3-super-120b-a12b"
_NVIDIA_WORKER_MODEL = "nvidia/llama-3.3-nemotron-super-49b-v1.5"

_MAX_AGENT_ITERATIONS = 6
_SESSION_TTL = 1800        # 30 minutes (memory cache eviction only)
_POLL_INTERVAL = 5.0       # seconds between task status checks
_POLL_TIMEOUT_SCRAPE = 600.0    # 120 × 5 s
_POLL_TIMEOUT_ANALYZE = 300.0   # 60 × 5 s
_POLL_TIMEOUT_GENERATE = 600.0  # 120 × 5 s

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "scrape_jobs",
            "description": (
                "Scrape LinkedIn jobs based on the active resume's "
                "domain and search configs"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_jobs",
            "description": (
                "Analyze all unanalyzed scraped jobs to extract "
                "required and preferred skills"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_resumes",
            "description": (
                "Generate tailored resumes for all analyzed jobs "
                "using the active resume"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "switch_resume",
            "description": (
                "Switch the active resume to a different domain or "
                "upload mode"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["sample", "own"],
                        "description": "sample or own",
                    },
                    "domain": {
                        "type": "string",
                        "description": (
                            "Domain key e.g. marketing, "
                            "software_engineering, finance"
                        ),
                    },
                },
                "required": ["mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_active_context",
            "description": (
                "Get current active resume, domain, and what jobs "
                "will be scraped"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_jobs",
            "description": (
                "Get list of scraped jobs with match scores and status"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["new", "analyzed", "applied"],
                        "description": "Filter by job status",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max jobs to return, default 10",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_skill_gap",
            "description": (
                "Analyze skill gaps between the user's resume "
                "and a specific job — shows matched, missing "
                "required, missing preferred skills"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "integer",
                        "description": "The job ID to analyze gaps for",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_learning_resources",
            "description": (
                "Search YouTube and the web for tutorials, courses, "
                "and documentation to learn a specific skill. Use when "
                "user wants to learn a skill or asks for resources, "
                "videos, or courses."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skill": {
                        "type": "string",
                        "description": (
                            "The exact skill name to search for e.g. "
                            "'Kubernetes', 'React hooks', 'SQL joins'"
                        ),
                    },
                },
                "required": ["skill"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_url",
            "description": (
                "Open a specific URL in the user's browser. Use when "
                "user asks to open a link, video, or resource by name "
                "or number from a previous search."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The full URL to open in the browser",
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "What is being opened e.g. "
                            "'Kubernetes tutorial by TechWorld'"
                        ),
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_assessment",
            "description": (
                "Start a 10-question skills assessment for a specific "
                "skill. Use when user asks to be tested, quizzed, or "
                "assessed on a skill. Generates MC and open-ended questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skill": {
                        "type": "string",
                        "description": (
                            "The skill to assess e.g. 'Kubernetes', "
                            "'Python', 'SQL'"
                        ),
                    },
                },
                "required": ["skill"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_answer",
            "description": (
                "Submit the user's answer to the current assessment "
                "question. Use ONLY when there is an active assessment "
                "and the user has provided an answer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "answer": {
                        "type": "string",
                        "description": (
                            "The user's answer. For MC: 'A', 'B', 'C', "
                            "or 'D'. For open ended: the full answer text."
                        ),
                    },
                    "assessment_id": {
                        "type": "integer",
                        "description": (
                            "The active assessment ID from session data"
                        ),
                    },
                },
                "required": ["answer", "assessment_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_interview_prep",
            "description": (
                "Start interview preparation for a specific job. "
                "mode='browse' shows all 15 questions as a browsable list. "
                "mode='mock' runs a live mock interview one question at a time. "
                "Use when user asks to prep for an interview, practice interview "
                "questions, or do a mock interview for a job."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "integer",
                        "description": (
                            "The job ID to prep for. If user doesn't specify, "
                            "call get_jobs first to find the right job."
                        ),
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["browse", "mock"],
                        "description": (
                            "browse = show all questions as a list. "
                            "mock = live mock interview one question at a time."
                        ),
                    },
                },
                "required": ["job_id", "mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_interview_answer",
            "description": (
                "Submit user's answer during a live mock interview. "
                "Use ONLY when there is an active mock interview and the user "
                "has provided an answer. Check session data for 'active_interview_id'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "answer": {
                        "type": "string",
                        "description": "The user's full answer to the current interview question",
                    },
                    "interview_session_id": {
                        "type": "integer",
                        "description": "The active interview session ID from session data",
                    },
                },
                "required": ["answer", "interview_session_id"],
            },
        },
    },
]

SYSTEM_PROMPT = """\
You are Gideon, a personal career agent and coach.
You are intelligent, conversational, and helpful — like a knowledgeable friend \
who happens to be an expert in hiring, careers, and job searching.

You can do two things:
1. Have natural conversations about careers, resumes, job searching, skills, \
industries, interview prep, salary negotiation, and anything work-related.
2. Automate the user's job search pipeline using your tools — scraping jobs, \
analyzing them, generating tailored resumes, switching resume targets, and \
analyzing skill gaps for specific jobs.

Conversation rules:
- Be warm, direct, and personable — not robotic.
- Answer questions fully even if no tool is needed.
- Give real, specific advice — not generic platitudes.
- Remember what the user has told you in this session.
- Proactively suggest next steps after completing tasks.
- If the user asks something unrelated to careers, still answer helpfully — \
you are a general assistant who specializes in careers.
- Never say "I cannot do that" for conversational questions — just answer them.
- Format your responses using markdown: **bold** for emphasis, bullet points, \
numbered lists, and headings where appropriate.

IMPORTANT: Use tools ONLY when the user wants to perform a specific pipeline \
action. For all other questions and conversations, respond directly without \
calling any tools. Never call get_active_context unless the user specifically \
asks what resume is active or you need that info before running a pipeline task \
like scraping or generating.

When you DO use tools:
- Tell the user what you are doing before calling a tool.
- After tool calls, summarize what happened in plain English with counts.
- Before scraping jobs, check the active context first using get_active_context \
so you know which resume and domain are active.
- If the domain is "other", warn the user that no search configs exist for that \
domain and ask them to pick a specific industry.
- When showing jobs, format them clearly with title, company, and match score.
- After completing a tool action, always follow up with a conversational \
response explaining what happened and what they could do next.
- If the user asks about skill gaps, missing skills, or what they need for a job, \
call analyze_skill_gap with the job_id. If they don't specify a job, first call \
get_jobs to show them options, then analyze the one they pick.

LEARNING TUTOR RULES:
- When the user asks to learn a skill or wants resources, tutorials, videos, or \
courses, call find_learning_resources with that skill name.
- When the user says "open the first one", "open link 2", "open the YouTube video", \
or similar, extract the URL from the previous search results in conversation history \
and call open_url with that URL.
- After showing resources, always offer: (1) to open any of them, (2) to search for \
a different skill, (3) to test their knowledge when ready.
- If the user says "help me learn missing skills" for a job, first call \
analyze_skill_gap to get the gaps, then ask which specific skill they want to start \
with before searching — do NOT search all skills at once.

ASSESSMENT RULES:
- When the user says "test me", "quiz me", "assess me", or "check my knowledge" on \
a skill, call start_assessment with that skill name.
- During an active assessment, ANY message from the user that looks like an answer \
(A/B/C/D or free text) should be treated as submit_answer. Check session data for \
'active_assessment_id' to know if an assessment is in progress.
- NEVER reveal correct answers or give feedback during the assessment — only after \
all 10 questions are answered.
- After assessment completes, offer to: (1) find learning resources for weak areas, \
(2) start another assessment on a different skill, (3) check job matches.
- If the user wants to quit mid-assessment, say they can type "quit assessment" and \
you will abandon it.

INTERVIEW PREP RULES:
- 'prep me for interview', 'interview questions', 'show questions for job X' → \
call start_interview_prep with mode='browse'.
- 'mock interview', 'practice interview', 'interview simulation' → \
call start_interview_prep with mode='mock'.
- If the user doesn't specify a job, call get_jobs first to show options, then ask \
which job they want to prep for.
- During an active mock interview (check session_data for 'active_interview_id'), \
treat ANY substantive response as submit_interview_answer.
- After browse mode, if the user says 'practice question 3', extract that question \
and ask them to answer it, then give feedback using your knowledge.
- After a mock interview completes, offer to: (1) redo the mock, \
(2) browse questions to review, (3) find study resources for weak areas.
"""


class GideonAgent:
    """Conversational agent that orchestrates the Gideon pipeline."""

    # Shared session store across instances (in-memory cache; DB is source of truth).
    _sessions: Dict[str, Dict[str, Any]] = {}

    def __init__(self, flask_app) -> None:
        self._app = flask_app
        self._test_client = None
        self._lock = threading.Lock()

        key = os.getenv("NVIDIA_API_KEY")
        if key:
            try:
                from openai import OpenAI
                self._ai_client = OpenAI(
                    base_url=_NVIDIA_BASE_URL,
                    api_key=key,
                )
            except Exception as exc:
                logger.warning("Failed to initialise NVIDIA client: %s", exc)
                self._ai_client = None
        else:
            self._ai_client = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def chat(self, user_message: str, session_id: Optional[str] = None) -> dict:
        if self._ai_client is None:
            return {
                "response": "AI is not available — NVIDIA_API_KEY not configured.",
                "actions_taken": [],
                "tool_calls": [],
                "session_id": session_id,
            }

        session = self._get_or_create_session(session_id)
        sid = session["id"]
        messages = session["messages"]
        actions: List[str] = session["actions_taken"]
        tool_names: List[str] = []

        messages.append({"role": "user", "content": user_message})
        self._save_message(sid, {"role": "user", "content": user_message})

        for _ in range(_MAX_AGENT_ITERATIONS):
            try:
                response = self._ai_client.chat.completions.create(
                    model=_NVIDIA_ORCHESTRATOR_MODEL,
                    messages=messages,
                    tools=TOOLS,
                    tool_choice="auto",
                )
            except Exception as exc:
                logger.exception("NIM chat call failed")
                return {
                    "response": f"AI backend error: {exc}",
                    "actions_taken": actions,
                    "tool_calls": tool_names,
                    "session_id": sid,
                }

            choice = response.choices[0]
            assistant_msg = choice.message

            if not assistant_msg.tool_calls:
                content = assistant_msg.content or ""
                msg_dict = {"role": "assistant", "content": content}
                messages.append(msg_dict)
                self._save_message(sid, msg_dict, actions_taken=list(actions))
                session["last_access"] = time.time()
                return {
                    "response": content,
                    "actions_taken": actions,
                    "tool_calls": tool_names,
                    "session_id": sid,
                }

            tc_list = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in assistant_msg.tool_calls
            ]
            msg_with_tc = {
                "role": "assistant",
                "content": assistant_msg.content or "",
                "tool_calls": tc_list,
            }
            messages.append(msg_with_tc)
            self._save_message(sid, msg_with_tc)

            for tc in assistant_msg.tool_calls:
                fn_name = tc.function.name
                try:
                    fn_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    fn_args = {}

                tool_names.append(fn_name)
                logger.info("Agent calling tool: %s(%s)", fn_name, fn_args)

                result_str = self._execute_tool(fn_name, fn_args, session_id=sid)
                action_summary = self._summarize_action(fn_name, result_str)
                if action_summary:
                    actions.append(action_summary)

                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_str,
                }
                messages.append(tool_msg)
                self._save_message(sid, tool_msg, tool_name=fn_name)

        final = messages[-1].get("content", "") if messages else ""
        session["last_access"] = time.time()
        return {
            "response": final or "I completed the requested actions.",
            "actions_taken": actions,
            "tool_calls": tool_names,
            "session_id": sid,
        }

    def reset(self, session_id: str) -> dict:
        """Drop a session from cache and DB."""
        with self._lock:
            self._sessions.pop(session_id, None)
        self._delete_session_from_db(session_id)
        return {"status": "reset", "session_id": session_id}

    def get_all_sessions(self) -> List[dict]:
        """Return all persisted chat sessions ordered by most recent."""
        try:
            from database.database import get_db
            from database.models import ChatSession
            with get_db() as db:
                rows = (
                    db.query(ChatSession)
                    .order_by(ChatSession.updated_at.desc())
                    .all()
                )
                return [r.to_dict() for r in rows]
        except Exception as exc:
            logger.warning("Failed to load chat sessions: %s", exc)
            return []

    def get_session_messages(self, session_id: str) -> Optional[dict]:
        """Return session metadata + user/assistant messages for display."""
        try:
            from database.database import get_db
            from database.models import ChatMessage, ChatSession
            with get_db() as db:
                session = db.query(ChatSession).filter(
                    ChatSession.id == session_id
                ).first()
                if not session:
                    return None
                msgs = (
                    db.query(ChatMessage)
                    .filter(
                        ChatMessage.session_id == session_id,
                        ChatMessage.role.in_(["user", "assistant"]),
                    )
                    .order_by(ChatMessage.created_at)
                    .all()
                )
                return {
                    "session": session.to_dict(),
                    "messages": [m.to_dict() for m in msgs],
                }
        except Exception as exc:
            logger.warning("Failed to load session messages: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    def _execute_tool(self, name: str, args: dict, session_id: str = None) -> str:
        client = self._get_test_client()

        try:
            if name == "get_active_context":
                resp = client.get("/api/active-context")
                return resp.get_data(as_text=True)

            if name == "get_jobs":
                params = []
                if args.get("status"):
                    params.append(f"status={args['status']}")
                limit = args.get("limit", 10)
                params.append(f"limit={limit}")
                url = "/api/jobs?" + "&".join(params)
                resp = client.get(url)
                return resp.get_data(as_text=True)

            if name == "switch_resume":
                payload = {"mode": args.get("mode", "sample")}
                if args.get("domain"):
                    payload["domain"] = args["domain"]
                resp = client.patch(
                    "/api/resume/mode",
                    data=json.dumps(payload),
                    content_type="application/json",
                )
                return resp.get_data(as_text=True)

            if name == "scrape_jobs":
                return self._run_and_wait("scrape", "/api/run/scrape", _POLL_TIMEOUT_SCRAPE)

            if name == "analyze_jobs":
                return self._run_and_wait("analyze", "/api/run/analyze", _POLL_TIMEOUT_ANALYZE)

            if name == "generate_resumes":
                return self._run_and_wait("generate", "/api/run/generate", _POLL_TIMEOUT_GENERATE)

            if name == "analyze_skill_gap":
                job_id = args.get("job_id")
                if not job_id:
                    return json.dumps({"error": "No job_id provided"})
                resp = client.get(f"/api/jobs/{job_id}/skill-gap")
                if resp.status_code != 200:
                    return resp.get_data(as_text=True)
                gap = json.loads(resp.get_data(as_text=True))
                from analyzer.gap_analyzer import SkillGapAnalyzer
                return SkillGapAnalyzer().format_for_chat(gap)

            if name == "find_learning_resources":
                skill = args.get("skill", "").strip()
                if not skill:
                    return "Please specify which skill to search for."
                from web.tutor import tutor
                resources = tutor.find_resources(skill)
                session_data = self._get_session_data(session_id)
                session_data["last_resources"] = resources
                session_data["last_skill"] = skill
                return tutor.format_resources_for_chat(resources)

            if name == "open_url":
                url = args.get("url", "").strip()
                if not url:
                    return "No URL provided to open."
                from web.tutor import tutor
                return tutor.open_url(url)

            if name == "start_assessment":
                skill = args.get("skill", "").strip()
                if not skill:
                    return "Please specify which skill to assess."
                resp = client.post(
                    "/api/assessment/start",
                    data=json.dumps({"session_id": session_id, "skill": skill}),
                    content_type="application/json",
                )
                data = json.loads(resp.get_data(as_text=True))
                if resp.status_code != 200:
                    return f"Could not start assessment: {data.get('error', 'unknown error')}"
                session_data = self._get_session_data(session_id)
                session_data["active_assessment_id"] = data["assessment_id"]
                session_data["active_assessment_skill"] = skill
                return (
                    f"Assessment started for **{skill}**!\n\n"
                    f"I'll ask you 10 questions \u2014 6 multiple choice "
                    f"and 4 open ended. Take your time.\n\n"
                    f"{data['question']}"
                )

            if name == "submit_answer":
                answer = args.get("answer", "").strip()
                assessment_id = args.get("assessment_id")
                if not answer:
                    return "Please provide an answer."
                if not assessment_id:
                    session_data = self._get_session_data(session_id)
                    assessment_id = session_data.get("active_assessment_id")
                if not assessment_id:
                    return "No active assessment found. Say 'test me on [skill]' to start one."
                resp = client.post(
                    f"/api/assessment/{assessment_id}/answer",
                    data=json.dumps({"answer": answer, "session_id": session_id}),
                    content_type="application/json",
                )
                data = json.loads(resp.get_data(as_text=True))
                if resp.status_code != 200:
                    return f"Error: {data.get('error', 'unknown error')}"
                if data["status"] == "completed":
                    session_data = self._get_session_data(session_id)
                    session_data.pop("active_assessment_id", None)
                    session_data.pop("active_assessment_skill", None)
                    return data["results"]
                return data["question"]

            if name == "start_interview_prep":
                job_id = args.get("job_id")
                mode = args.get("mode", "browse")
                if not job_id:
                    return (
                        "Please specify which job to prep for. "
                        "Say 'show my jobs' and I'll list them."
                    )
                r = client.post(
                    "/api/interview/start",
                    data=json.dumps({"session_id": session_id, "job_id": job_id, "mode": mode}),
                    content_type="application/json",
                )
                data = json.loads(r.get_data(as_text=True))
                if r.status_code != 200:
                    return f"Could not start interview prep: {data.get('error')}"
                sess = self._get_session_data(session_id)
                sess["active_interview_id"] = data["interview_session_id"]
                sess["active_interview_job"] = data.get("job_title", "")
                sess["active_interview_mode"] = mode
                if mode == "browse":
                    return data.get("questions_formatted", "")
                return f"{data.get('intro', '')}\n\n---\n\n{data.get('first_question', '')}"

            if name == "submit_interview_answer":
                answer = args.get("answer", "").strip()
                interview_id = args.get("interview_session_id")
                if not answer:
                    return "Please provide your answer."
                if not interview_id:
                    sess = self._get_session_data(session_id)
                    interview_id = sess.get("active_interview_id")
                if not interview_id:
                    return (
                        "No active mock interview. "
                        "Say 'mock interview for job X' to start."
                    )
                r = client.post(
                    f"/api/interview/{interview_id}/answer",
                    data=json.dumps({"answer": answer, "session_id": session_id}),
                    content_type="application/json",
                )
                data = json.loads(r.get_data(as_text=True))
                if r.status_code != 200:
                    return f"Error: {data.get('error')}"
                if data["status"] == "continue":
                    return data.get("feedback", "")
                if data["status"] == "completed":
                    sess = self._get_session_data(session_id)
                    sess.pop("active_interview_id", None)
                    sess.pop("active_interview_job", None)
                    sess.pop("active_interview_mode", None)
                    return data.get("results", "")

            return json.dumps({"error": f"Unknown tool: {name}"})

        except Exception as exc:
            logger.exception("Tool execution error: %s", name)
            return json.dumps({"error": str(exc)})

    def _run_and_wait(self, task_key: str, endpoint: str, timeout: float) -> str:
        client = self._get_test_client()

        resp = client.post(endpoint)
        data = json.loads(resp.get_data(as_text=True))

        if resp.status_code == 409:
            logger.info("Task %s already running, waiting for it.", task_key)
        elif resp.status_code != 200:
            return json.dumps({"error": data.get("error", "Failed to start task")})

        start = time.time()
        while time.time() - start < timeout:
            time.sleep(_POLL_INTERVAL)
            status_resp = client.get("/api/run/status")
            status = json.loads(status_resp.get_data(as_text=True))
            if not status.get(task_key, False):
                result_resp = client.get(f"/api/run/result/{task_key}")
                run_result = json.loads(result_resp.get_data(as_text=True)).get("result")
                stats_resp = client.get("/api/stats")
                stats = json.loads(stats_resp.get_data(as_text=True))
                last_run_resp = client.get("/api/run/last-run")
                last_run = json.loads(last_run_resp.get_data(as_text=True))
                return json.dumps({
                    "status": "completed",
                    "task": task_key,
                    "run_result": run_result,
                    "stats": {
                        "total_jobs": stats.get("total_jobs", 0),
                        "new_jobs": stats.get("new_jobs", 0),
                        "analyzed_jobs": stats.get("analyzed_jobs", 0),
                        "total_resumes": stats.get("total_resumes", 0),
                    },
                    "completed_at": last_run.get(task_key),
                })

        return json.dumps({"status": "timeout", "task": task_key})

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def _get_or_create_session(self, session_id: Optional[str]) -> dict:
        with self._lock:
            self._cleanup_expired_sessions()

            if session_id and session_id in self._sessions:
                session = self._sessions[session_id]
                session["last_access"] = time.time()
                return session

        # Not in cache — try loading from DB
        if session_id:
            session = self._load_session_from_db(session_id)
            if session:
                with self._lock:
                    self._sessions[session_id] = session
                return session

        # Create new session
        sid = session_id or str(uuid.uuid4())
        session = {
            "id": sid,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}],
            "last_access": time.time(),
            "actions_taken": [],
        }
        self._create_session_in_db(sid)
        with self._lock:
            self._sessions[sid] = session
        return session

    def _cleanup_expired_sessions(self) -> None:
        now = time.time()
        expired = [
            sid for sid, s in self._sessions.items()
            if now - s["last_access"] > _SESSION_TTL
        ]
        for sid in expired:
            del self._sessions[sid]
            logger.debug("Evicted chat session from cache: %s", sid)

    # ------------------------------------------------------------------
    # DB persistence helpers
    # ------------------------------------------------------------------

    def _create_session_in_db(self, session_id: str) -> None:
        try:
            from database.database import get_db
            from database.models import ChatSession
            with get_db() as db:
                existing = db.query(ChatSession).filter(
                    ChatSession.id == session_id
                ).first()
                if not existing:
                    db.add(ChatSession(id=session_id))
        except Exception as exc:
            logger.warning("Failed to create chat session in DB: %s", exc)

    def _load_session_from_db(self, session_id: str) -> Optional[dict]:
        try:
            from database.database import get_db
            from database.models import ChatMessage, ChatSession
            with get_db() as db:
                cs = db.query(ChatSession).filter(
                    ChatSession.id == session_id
                ).first()
                if not cs:
                    return None

                rows = (
                    db.query(ChatMessage)
                    .filter(ChatMessage.session_id == session_id)
                    .order_by(ChatMessage.created_at)
                    .all()
                )

                messages: List[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
                actions: List[str] = []

                for row in rows:
                    msg: Dict[str, Any] = {"role": row.role, "content": row.content}
                    if row.tool_calls_json:
                        try:
                            msg["tool_calls"] = json.loads(row.tool_calls_json)
                        except json.JSONDecodeError:
                            pass
                    if row.tool_call_id:
                        msg["tool_call_id"] = row.tool_call_id
                    if row.actions_taken:
                        actions.extend(row.actions_taken)
                    messages.append(msg)

                return {
                    "id": session_id,
                    "messages": messages,
                    "last_access": time.time(),
                    "actions_taken": actions,
                }
        except Exception as exc:
            logger.warning("Failed to load chat session from DB: %s", exc)
            return None

    def _save_message(
        self,
        session_id: str,
        msg: dict,
        *,
        tool_name: Optional[str] = None,
        actions_taken: Optional[List[str]] = None,
    ) -> None:
        try:
            from database.database import get_db
            from database.models import ChatMessage, ChatSession
            with get_db() as db:
                cs = db.query(ChatSession).filter(
                    ChatSession.id == session_id
                ).first()
                if not cs:
                    return

                cm = ChatMessage(
                    session_id=session_id,
                    role=msg["role"],
                    content=msg.get("content", ""),
                    tool_call_id=msg.get("tool_call_id"),
                    tool_name=tool_name,
                    actions_taken=actions_taken,
                )
                if "tool_calls" in msg:
                    cm.tool_calls_json = json.dumps(msg["tool_calls"])

                db.add(cm)
                cs.message_count = (cs.message_count or 0) + 1
                cs.updated_at = datetime.now(timezone.utc)

                if cs.title is None and msg["role"] == "user":
                    text = msg.get("content", "").strip()
                    cs.title = (text[:57] + "...") if len(text) > 60 else text
        except Exception as exc:
            logger.warning("Failed to save chat message: %s", exc)

    def _delete_session_from_db(self, session_id: str) -> None:
        try:
            from database.database import get_db
            from database.models import ChatSession
            with get_db() as db:
                cs = db.query(ChatSession).filter(
                    ChatSession.id == session_id
                ).first()
                if cs:
                    db.delete(cs)
        except Exception as exc:
            logger.warning("Failed to delete chat session: %s", exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_session_data(self, session_id: str) -> dict:
        if not session_id or session_id not in self._sessions:
            return {}
        sess = self._sessions[session_id]
        if "data" not in sess:
            sess["data"] = {}
        return sess["data"]

    def _get_test_client(self):
        if self._test_client is None:
            self._test_client = self._app.test_client()
        return self._test_client

    @staticmethod
    def _summarize_action(tool_name: str, result_str: str) -> Optional[str]:
        if tool_name == "analyze_skill_gap":
            return "Analyzed skill gap"

        if tool_name == "find_learning_resources":
            return "Found learning resources"

        if tool_name == "open_url":
            return "Opened link"

        if tool_name == "start_assessment":
            return "Started skill assessment"

        if tool_name == "submit_answer":
            return "Submitted answer"

        if tool_name == "start_interview_prep":
            return "Started interview prep"

        if tool_name == "submit_interview_answer":
            return "Submitted interview answer"

        try:
            data = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            return None

        if data.get("status") == "timeout":
            return f"{tool_name} timed out"

        if tool_name == "scrape_jobs" and data.get("status") == "completed":
            run = data.get("run_result") or {}
            found = run.get("new_jobs", 0)
            total = data.get("stats", {}).get("total_jobs", "?")
            return f"Scraped {found} new job(s) ({total} total)"

        if tool_name == "analyze_jobs" and data.get("status") == "completed":
            run = data.get("run_result") or {}
            analyzed_count = run.get("analyzed", run.get("jobs_analyzed", 0))
            return f"Analyzed {analyzed_count} job(s)"

        if tool_name == "generate_resumes" and data.get("status") == "completed":
            run = data.get("run_result") or {}
            generated = run.get("generated", run.get("resumes_generated", 0))
            return f"Generated {generated} resume(s)"

        if tool_name == "switch_resume" and data.get("status") == "switched":
            name = data.get("active_resume", "?")
            return f"Switched to {name}"

        return None


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

gideon: Optional[GideonAgent] = None


def init_agent(flask_app) -> Optional[GideonAgent]:
    """Initialise the module-level ``gideon`` singleton.

    Failures are logged and swallowed so a broken agent never prevents Flask
    from booting.
    """
    global gideon
    if gideon is None:
        try:
            gideon = GideonAgent(flask_app)
        except Exception as exc:
            logger.warning("Failed to initialise GideonAgent: %s", exc)
            gideon = None
    return gideon
