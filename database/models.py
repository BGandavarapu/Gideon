"""
SQLAlchemy ORM models for the Gideon application.

All four tables from the Phase 2 schema are defined here:

    jobs               – scraped job postings
    master_resumes     – user-uploaded base resumes
    tailored_resumes   – AI-generated per-job resume variants
    applications       – application tracking records

Design decisions
----------------
- SQLAlchemy 2.0 ``DeclarativeBase`` syntax is used throughout.
- JSON columns store structured list/dict data (skills, resume sections).
  SQLite serialises these as TEXT; PostgreSQL would use native JSONB.
- ``server_default`` / ``default`` on timestamp columns ensures values
  are populated whether the row is inserted via ORM or raw SQL.
- Indexes are added to the columns most likely to appear in WHERE clauses:
  ``application_url`` (unique + dedup key), ``status``, ``date_scraped``,
  and the foreign-key columns on child tables.
- All relationships use ``back_populates`` (explicit, symmetric) rather
  than ``backref`` (implicit, harder to type-check).
- ``cascade="all, delete-orphan"`` on child collections means deleting a
  Job cascades through its TailoredResumes and Applications automatically.
"""

import logging
from datetime import date, datetime, timezone
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Declarative base
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    """Project-wide SQLAlchemy declarative base.

    All ORM models inherit from this class.  The ``type_annotation_map``
    is left at its default so standard Python types resolve automatically.
    """


# ---------------------------------------------------------------------------
# Job model
# ---------------------------------------------------------------------------


class Job(Base):
    """Represents a single scraped job posting.

    This is the central table; both :class:`TailoredResume` and
    :class:`Application` reference it via foreign key.

    Attributes:
        id: Auto-incremented primary key.
        job_title: Normalised position title (e.g. ``"Senior Python Developer"``).
        company_name: Name of the hiring organisation.
        location: Office location or ``"Remote"``.
        job_description: Full body text of the posting.
        required_skills: JSON array of required skill strings extracted by the
            analyser; ``None`` until Phase 3 processing completes.
        preferred_skills: JSON array of preferred / nice-to-have skills.
        salary_range: Human-readable salary string (e.g. ``"$120k – $160k"``).
        application_url: Canonical apply URL; enforced UNIQUE for deduplication.
        date_posted: Calendar date the listing was published, if available.
        date_scraped: UTC timestamp of when this row was inserted.
        source: Origin job board (``"linkedin"`` | ``"indeed"``).
        status: Processing lifecycle flag:
            ``"new"``      – just scraped, not yet analysed
            ``"analyzed"`` – keyword extraction complete
            ``"applied"``  – application submitted
        tailored_resumes: All :class:`TailoredResume` rows generated for this job.
        applications: All :class:`Application` rows linked to this job.
    """

    __tablename__ = "jobs"

    # Primary key
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Core fields (NOT NULL in DB)
    job_title: Mapped[str] = mapped_column(String(255), nullable=False)
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    job_description: Mapped[str] = mapped_column(Text, nullable=False)
    application_url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False)

    # Optional fields
    location: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    required_skills: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    preferred_skills: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    salary_range: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    date_posted: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    # Domain classification (set during scraping / analysis)
    domain: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, default=None)

    # Which master resume was active when this job was analyzed.
    # NULL means the job was analyzed before this feature was added (allow generation).
    # SET NULL on delete so removing a resume doesn't orphan jobs.
    analyzed_with_resume_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("master_resumes.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
    )

    # Match score: computed at analysis time (resume skills vs job skills).
    # NULL for jobs analyzed before this column was added.
    match_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=None)

    # Timestamps & status
    date_scraped: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="new",
        server_default="new",
    )

    # Relationships
    tailored_resumes: Mapped[List["TailoredResume"]] = relationship(
        "TailoredResume",
        back_populates="job",
        cascade="all, delete-orphan",
        lazy="select",
    )
    applications: Mapped[List["Application"]] = relationship(
        "Application",
        back_populates="job",
        cascade="all, delete-orphan",
        lazy="select",
    )

    # Composite indexes
    __table_args__ = (
        Index("ix_jobs_status", "status"),
        Index("ix_jobs_date_scraped", "date_scraped"),
        Index("ix_jobs_company_title", "company_name", "job_title"),
    )

    def __repr__(self) -> str:
        return (
            f"<Job(id={self.id}, title={self.job_title!r}, "
            f"company={self.company_name!r}, status={self.status!r})>"
        )

    def to_dict(self) -> dict:
        """Return a JSON-serialisable dictionary of this job's fields.

        Returns:
            Dictionary suitable for API responses or JSON export.
        """
        return {
            "id": self.id,
            "job_title": self.job_title,
            "company_name": self.company_name,
            "location": self.location,
            "job_description": self.job_description,
            "required_skills": self.required_skills,
            "preferred_skills": self.preferred_skills,
            "salary_range": self.salary_range,
            "application_url": self.application_url,
            "date_posted": self.date_posted.isoformat() if self.date_posted else None,
            "date_scraped": self.date_scraped.isoformat() if self.date_scraped else None,
            "source": self.source,
            "status": self.status,
            "domain": self.domain,
            "analyzed_with_resume_id": self.analyzed_with_resume_id,
        }


# ---------------------------------------------------------------------------
# MasterResume model
# ---------------------------------------------------------------------------


class MasterResume(Base):
    """Represents a user-uploaded master (base) resume.

    The ``content`` field stores the full resume as a structured JSON object
    with named sections (``summary``, ``experience``, ``skills``, etc.).
    Only one resume is the active base at a time (``is_active=True``), though
    the system supports multiple stored versions.

    Attributes:
        id: Auto-incremented primary key.
        name: Human-readable label for this version (e.g. ``"SWE 2024"``).
        content: JSON dict with sections:
            ``{"summary": "...", "experience": [...], "skills": [...],
               "education": [...], "certifications": [...]}``
        created_at: UTC timestamp when this record was created.
        is_active: Whether this is the resume used for new tailoring jobs.
        tailored_resumes: All :class:`TailoredResume` rows derived from this.
    """

    __tablename__ = "master_resumes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="1",
    )
    is_sample: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )
    style_fingerprint: Mapped[Optional[dict]] = mapped_column(
        JSON,
        nullable=True,
        default=None,
    )
    domain: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, default=None)
    # JSON list of domain strings — allows a resume to target multiple industries.
    # When set, `domain` always holds domains[0] for backwards compatibility.
    domains: Mapped[Optional[list]] = mapped_column(JSON, nullable=True, default=None)

    # Relationships
    tailored_resumes: Mapped[List["TailoredResume"]] = relationship(
        "TailoredResume",
        back_populates="master_resume",
        cascade="all, delete-orphan",
        lazy="select",
    )

    __table_args__ = (Index("ix_master_resumes_is_active", "is_active"),)

    def __repr__(self) -> str:
        return (
            f"<MasterResume(id={self.id}, name={self.name!r}, "
            f"is_active={self.is_active})>"
        )

    def to_dict(self) -> dict:
        """Return a JSON-serialisable dictionary.

        Returns:
            Dictionary of all fields (``content`` already JSON-compatible).
        """
        content = self.content if isinstance(self.content, dict) else {}
        return {
            "id": self.id,
            "name": self.name,
            "content": self.content,
            "content_keys": list(content.keys()) if content else [],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "is_active": self.is_active,
            "is_sample": self.is_sample,
            "style_fingerprint": self.style_fingerprint,
            "domain": self.domain,
            "domains": self.domains,
        }


# ---------------------------------------------------------------------------
# TailoredResume model
# ---------------------------------------------------------------------------


class TailoredResume(Base):
    """An AI-generated resume variant tailored to a specific job.

    Each row links a :class:`MasterResume` to a :class:`Job` and stores
    the NIM-modified resume content, the match score calculated by the
    analyser, and the path to the generated PDF on disk.

    Attributes:
        id: Auto-incremented primary key.
        job_id: FK → :attr:`Job.id`.
        master_resume_id: FK → :attr:`MasterResume.id`.
        tailored_content: JSON dict with the same structure as
            :attr:`MasterResume.content` but with AI-modified text.
        match_score: Float 0–100 indicating how closely the tailored resume
            matches the job description keywords.
        generated_at: UTC timestamp of generation.
        pdf_path: Filesystem path to the rendered PDF file, or ``None`` if
            PDF generation has not yet been run.
        job: Linked :class:`Job` instance.
        master_resume: Linked :class:`MasterResume` instance.
        applications: :class:`Application` rows that used this resume.
    """

    __tablename__ = "tailored_resumes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    master_resume_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("master_resumes.id", ondelete="CASCADE"), nullable=False
    )
    tailored_content: Mapped[dict] = mapped_column(JSON, nullable=False)
    match_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    score_breakdown: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True, default=None)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    pdf_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Relationships
    job: Mapped["Job"] = relationship("Job", back_populates="tailored_resumes")
    master_resume: Mapped["MasterResume"] = relationship(
        "MasterResume", back_populates="tailored_resumes"
    )
    applications: Mapped[List["Application"]] = relationship(
        "Application",
        back_populates="tailored_resume",
        cascade="all, delete-orphan",
        lazy="select",
    )

    __table_args__ = (
        Index("ix_tailored_resumes_job_id", "job_id"),
        Index("ix_tailored_resumes_match_score", "match_score"),
        UniqueConstraint("job_id", "master_resume_id", name="uq_tailored_job_resume"),
    )

    def __repr__(self) -> str:
        return (
            f"<TailoredResume(id={self.id}, job_id={self.job_id}, "
            f"match_score={self.match_score})>"
        )

    def to_dict(self) -> dict:
        """Return a JSON-serialisable dictionary.

        Returns:
            Dictionary of all fields with ISO-formatted timestamp.
        """
        return {
            "id": self.id,
            "job_id": self.job_id,
            "master_resume_id": self.master_resume_id,
            "tailored_content": self.tailored_content,
            "match_score": self.match_score,
            "score_breakdown": self.score_breakdown,
            "generated_at": self.generated_at.isoformat() if self.generated_at else None,
            "pdf_path": self.pdf_path,
        }


# ---------------------------------------------------------------------------
# Application model
# ---------------------------------------------------------------------------


class Application(Base):
    """Tracks the submission and outcome of a job application.

    Links a :class:`TailoredResume` to the :class:`Job` it was created for
    and records the lifecycle of the application (submitted → interviewing
    → offered / rejected).

    Attributes:
        id: Auto-incremented primary key.
        job_id: FK → :attr:`Job.id`.
        tailored_resume_id: FK → :attr:`TailoredResume.id`.
        application_date: Calendar date the application was submitted.
        status: Lifecycle state:
            ``"applied"``      – submitted
            ``"interviewing"`` – interview stage
            ``"offered"``      – offer received
            ``"rejected"``     – rejected
            ``"withdrawn"``    – candidate withdrew
        notes: Free-form notes (interview feedback, follow-up reminders, etc.).
        created_at: UTC timestamp this record was first created.
        updated_at: UTC timestamp of the most recent status change.
        job: Linked :class:`Job`.
        tailored_resume: Linked :class:`TailoredResume`.
    """

    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    tailored_resume_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("tailored_resumes.id", ondelete="SET NULL"),
        nullable=True,
    )
    application_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="applied",
        server_default="applied",
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    job: Mapped["Job"] = relationship("Job", back_populates="applications")
    tailored_resume: Mapped[Optional["TailoredResume"]] = relationship(
        "TailoredResume", back_populates="applications"
    )

    __table_args__ = (
        Index("ix_applications_job_id", "job_id"),
        Index("ix_applications_status", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<Application(id={self.id}, job_id={self.job_id}, "
            f"status={self.status!r})>"
        )

    def to_dict(self) -> dict:
        """Return a JSON-serialisable dictionary.

        Returns:
            Dictionary of all fields with ISO-formatted date/timestamps.
        """
        return {
            "id": self.id,
            "job_id": self.job_id,
            "tailored_resume_id": self.tailored_resume_id,
            "application_date": (
                self.application_date.isoformat() if self.application_date else None
            ),
            "status": self.status,
            "notes": self.notes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# ---------------------------------------------------------------------------
# Chat persistence models
# ---------------------------------------------------------------------------


class ChatSession(Base):
    """A single conversation between the user and Gideon."""

    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    messages: Mapped[List["ChatMessage"]] = relationship(
        "ChatMessage", back_populates="session",
        order_by="ChatMessage.created_at",
        cascade="all, delete-orphan",
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title or "New conversation",
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "message_count": self.message_count,
        }


class ChatMessage(Base):
    """A single message within a chat session."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_calls_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tool_call_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    tool_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    actions_taken: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    session: Mapped["ChatSession"] = relationship("ChatSession", back_populates="messages")

    __table_args__ = (Index("ix_chat_messages_session_id", "session_id"),)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "role": self.role,
            "content": self.content,
            "tool_name": self.tool_name,
            "actions_taken": self.actions_taken,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ---------------------------------------------------------------------------
# Assessment models
# ---------------------------------------------------------------------------


class SkillAssessment(Base):
    """A skill assessment session — 10 questions generated and graded."""

    __tablename__ = "skill_assessments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    skill: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="in_progress", server_default="in_progress",
    )
    current_question: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    questions: Mapped[List["AssessmentQuestion"]] = relationship(
        "AssessmentQuestion",
        back_populates="assessment",
        order_by="AssessmentQuestion.question_number",
        cascade="all, delete-orphan",
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "skill": self.skill,
            "status": self.status,
            "current_question": self.current_question,
            "score": self.score,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class AssessmentQuestion(Base):
    """A single question within a skill assessment."""

    __tablename__ = "assessment_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    assessment_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("skill_assessments.id", ondelete="CASCADE"), nullable=False,
    )
    question_number: Mapped[int] = mapped_column(Integer, nullable=False)
    question_type: Mapped[str] = mapped_column(String(20), nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    options: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    correct_answer: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    user_answer: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_correct: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    score_awarded: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    feedback: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    assessment: Mapped["SkillAssessment"] = relationship(
        "SkillAssessment", back_populates="questions",
    )

    __table_args__ = (
        Index("ix_assessment_questions_assessment_id", "assessment_id"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "assessment_id": self.assessment_id,
            "question_number": self.question_number,
            "question_type": self.question_type,
            "question_text": self.question_text,
            "options": self.options,
            "correct_answer": self.correct_answer,
            "user_answer": self.user_answer,
            "is_correct": self.is_correct,
            "score_awarded": self.score_awarded,
            "feedback": self.feedback,
        }


# ---------------------------------------------------------------------------
# Interview Prep models
# ---------------------------------------------------------------------------


class InterviewSession(Base):
    """A job-specific interview prep session — 15 questions, browse or mock mode."""

    __tablename__ = "interview_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    job_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="in_progress", server_default="in_progress",
    )
    current_question: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    hiring_recommendation: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    questions: Mapped[List["InterviewQuestion"]] = relationship(
        "InterviewQuestion",
        back_populates="interview_session",
        order_by="InterviewQuestion.question_number",
        cascade="all, delete-orphan",
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "job_id": self.job_id,
            "mode": self.mode,
            "status": self.status,
            "current_question": self.current_question,
            "score": self.score,
            "hiring_recommendation": self.hiring_recommendation,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class InterviewQuestion(Base):
    """A single question within an interview prep session."""

    __tablename__ = "interview_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    interview_session_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("interview_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    question_number: Mapped[int] = mapped_column(Integer, nullable=False)
    question_type: Mapped[str] = mapped_column(String(20), nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    model_answer_tips: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    user_answer: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    feedback_strengths: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    feedback_gaps: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    feedback_suggestion: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    score_awarded: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    interview_session: Mapped["InterviewSession"] = relationship(
        "InterviewSession", back_populates="questions",
    )

    __table_args__ = (
        Index("ix_interview_questions_session_id", "interview_session_id"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "interview_session_id": self.interview_session_id,
            "question_number": self.question_number,
            "question_type": self.question_type,
            "question_text": self.question_text,
            "category": self.category,
            "model_answer_tips": self.model_answer_tips,
            "user_answer": self.user_answer,
            "feedback_strengths": self.feedback_strengths,
            "feedback_gaps": self.feedback_gaps,
            "feedback_suggestion": self.feedback_suggestion,
            "score_awarded": self.score_awarded,
        }
