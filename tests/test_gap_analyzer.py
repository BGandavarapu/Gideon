"""Tests for SkillGapAnalyzer — skill gap analysis between jobs and resumes."""

import pytest
from types import SimpleNamespace

from analyzer.gap_analyzer import SkillGapAnalyzer


def _make_job(required=None, preferred=None, job_id=1, title="Engineer", company="Acme", status="analyzed"):
    return SimpleNamespace(
        id=job_id,
        job_title=title,
        company_name=company,
        required_skills=required or [],
        preferred_skills=preferred or [],
        status=status,
    )


def _make_resume(skills=None, certs=None):
    content = {}
    if skills is not None:
        content["skills"] = skills
    if certs is not None:
        content["certifications"] = certs
    return SimpleNamespace(content=content, is_active=True)


REQUIRED = ["Python", "Django", "PostgreSQL", "Kubernetes", "Go"]
PREFERRED = ["Redis", "Kafka", "Terraform"]
RESUME_SKILLS = ["Python", "Django", "PostgreSQL", "REST APIs", "Docker", "AWS"]


class TestSkillGapAnalyzer:

    def test_matched_skills(self):
        job = _make_job(required=REQUIRED, preferred=PREFERRED)
        resume = _make_resume(skills=RESUME_SKILLS)
        result = SkillGapAnalyzer().analyze(job, resume)
        assert "Python" in result["matched_skills"]
        assert "Django" in result["matched_skills"]
        assert "PostgreSQL" in result["matched_skills"]

    def test_missing_required(self):
        job = _make_job(required=REQUIRED, preferred=PREFERRED)
        resume = _make_resume(skills=RESUME_SKILLS)
        result = SkillGapAnalyzer().analyze(job, resume)
        assert "Kubernetes" in result["missing_required"]
        assert "Go" in result["missing_required"]
        assert "Python" not in result["missing_required"]

    def test_missing_preferred(self):
        job = _make_job(required=REQUIRED, preferred=PREFERRED)
        resume = _make_resume(skills=RESUME_SKILLS)
        result = SkillGapAnalyzer().analyze(job, resume)
        assert "Redis" in result["missing_preferred"]
        assert "Kafka" in result["missing_preferred"]
        assert "Terraform" in result["missing_preferred"]

    def test_match_percentage(self):
        job = _make_job(required=REQUIRED, preferred=PREFERRED)
        resume = _make_resume(skills=RESUME_SKILLS)
        result = SkillGapAnalyzer().analyze(job, resume)
        assert result["match_percentage"] == 60.0

    def test_no_gaps_all_present(self):
        job = _make_job(required=["Python", "Django"], preferred=["Docker"])
        resume = _make_resume(skills=["Python", "Django", "Docker"])
        result = SkillGapAnalyzer().analyze(job, resume)
        assert result["missing_required"] == []
        assert result["match_percentage"] == 100.0
        assert result["has_gaps"] is False

    def test_priority_gaps_max_five(self):
        job = _make_job(required=["A", "B", "C", "D", "E", "F", "G"])
        resume = _make_resume(skills=[])
        result = SkillGapAnalyzer().analyze(job, resume)
        assert len(result["priority_gaps"]) == 5

    def test_case_insensitive(self):
        job = _make_job(required=["python", "DJANGO"])
        resume = _make_resume(skills=["Python", "Django"])
        result = SkillGapAnalyzer().analyze(job, resume)
        assert result["missing_required"] == []
        assert result["match_percentage"] == 100.0

    def test_dict_skills(self):
        job = _make_job(required=["Python", "SQL", "Go"])
        resume = _make_resume(skills={
            "Programming": ["Python", "JavaScript"],
            "Data": ["SQL", "Tableau"],
        })
        result = SkillGapAnalyzer().analyze(job, resume)
        assert "Python" in result["matched_skills"]
        assert "SQL" in result["matched_skills"]
        assert "Go" in result["missing_required"]
        assert result["matched_required_count"] == 2

    def test_certifications_counted(self):
        job = _make_job(required=["AWS Solutions Architect"])
        resume = _make_resume(skills=[], certs=["AWS Solutions Architect"])
        result = SkillGapAnalyzer().analyze(job, resume)
        assert result["missing_required"] == []

    def test_format_for_chat(self):
        job = _make_job(required=REQUIRED, preferred=PREFERRED)
        resume = _make_resume(skills=RESUME_SKILLS)
        analyzer = SkillGapAnalyzer()
        result = analyzer.analyze(job, resume)
        text = analyzer.format_for_chat(result)
        assert "Skill Gap Analysis" in text
        assert "60.0%" in text
        assert "Kubernetes" in text
        assert "Priority gaps" in text

    def test_no_required_skills(self):
        job = _make_job(required=[], preferred=["Redis"])
        resume = _make_resume(skills=["Python"])
        result = SkillGapAnalyzer().analyze(job, resume)
        assert result["match_percentage"] == 100.0
        assert result["has_gaps"] is False

    def test_return_schema_fields(self):
        job = _make_job(required=REQUIRED, preferred=PREFERRED)
        resume = _make_resume(skills=RESUME_SKILLS)
        result = SkillGapAnalyzer().analyze(job, resume)
        expected_keys = {
            "job_id", "job_title", "company",
            "matched_skills", "missing_required", "missing_preferred",
            "match_percentage", "required_total", "preferred_total",
            "matched_required_count", "matched_preferred_count",
            "priority_gaps", "has_gaps",
        }
        assert set(result.keys()) == expected_keys


class TestSkillGapAPI:

    @pytest.fixture()
    def client(self, tmp_path, monkeypatch):
        db_url = f"sqlite:///{tmp_path / 'test_gap.db'}"
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        from database.database import reset_manager, create_tables
        reset_manager(db_url)
        create_tables()
        from web.app import app
        app.config["TESTING"] = True
        with app.test_client() as c:
            yield c
        reset_manager(None)

    def test_api_skill_gap_analyzed_job(self, client):
        from database.database import get_db
        from database.models import Job, MasterResume
        with get_db() as db:
            resume = MasterResume(
                name="Test Resume",
                content={"skills": ["Python", "Django", "AWS"]},
                is_active=True,
                is_sample=False,
                domain="software_engineering",
            )
            db.add(resume)
            db.flush()
            job = Job(
                job_title="Backend Dev",
                company_name="TestCo",
                job_description="Test job description",
                source="linkedin",
                required_skills=["Python", "Go", "Kubernetes"],
                preferred_skills=["Redis"],
                status="analyzed",
                application_url="https://example.com/job-gap-test",
            )
            db.add(job)
            db.flush()
            job_id = job.id

        resp = client.get(f"/api/jobs/{job_id}/skill-gap")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "matched_skills" in data
        assert "missing_required" in data
        assert "match_percentage" in data
        assert "Python" in data["matched_skills"]
        assert "Go" in data["missing_required"]

    def test_api_skill_gap_unanalyzed_job(self, client):
        from database.database import get_db
        from database.models import Job, MasterResume
        with get_db() as db:
            resume = MasterResume(
                name="Test Resume 2",
                content={"skills": ["Python"]},
                is_active=True,
                is_sample=False,
                domain="software_engineering",
            )
            db.add(resume)
            job = Job(
                job_title="Test Job",
                company_name="TestCo",
                job_description="Test job description",
                source="linkedin",
                status="new",
                application_url="https://example.com/job-gap-new",
            )
            db.add(job)
            db.flush()
            job_id = job.id

        resp = client.get(f"/api/jobs/{job_id}/skill-gap")
        assert resp.status_code == 400
        assert "analyzed" in resp.get_json()["error"].lower()

    def test_api_skill_gap_not_found(self, client):
        resp = client.get("/api/jobs/99999/skill-gap")
        assert resp.status_code == 404
