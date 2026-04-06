"""Seed a demo job and master resume for Phase 4 CLI testing."""
import datetime
import sys
from pathlib import Path

# Make sure project root is on the path
sys.path.insert(0, str(Path(__file__).parent.parent))

from database.database import get_db
from database.models import Job, MasterResume

JOB_DATA = {
    "job_title": "Senior Python Developer",
    "company_name": "TechCorp",
    "application_url": "https://example.com/job/1",
    "job_description": (
        "We are looking for a Python expert with Django, REST API, PostgreSQL, Docker, "
        "and AWS experience. Must have strong communication skills and 5+ years of experience."
    ),
    "required_skills": ["python", "django", "rest api", "postgresql", "docker"],
    "preferred_skills": ["aws", "redis", "celery", "kubernetes"],
    "source": "test",
}

RESUME_CONTENT = {
    "personal_info": {"name": "Alex Smith", "email": "alex@example.com"},
    "professional_summary": (
        "Experienced software engineer with 6 years building scalable Python applications. "
        "Proficient in Django and REST APIs with strong communication and leadership skills."
    ),
    "work_experience": [
        {
            "company": "StartupCo",
            "title": "Software Engineer",
            "dates": "2018-2024",
            "bullets": [
                "Built Django REST APIs serving 50k daily active users",
                "Optimised PostgreSQL queries reducing latency by 40%",
                "Containerised services with Docker cutting deployment time by 30%",
                "Collaborated with cross-functional teams to deliver features on schedule",
            ],
        }
    ],
    "skills": ["python", "django", "rest api", "postgresql", "docker", "git", "linux"],
    "education": [
        {"degree": "B.Sc. Computer Science", "institution": "State University", "year": 2018}
    ],
    "projects": [
        {
            "name": "API Gateway",
            "description": "Built a high-throughput REST API gateway in Python/Django",
            "tech": ["python", "django", "redis"],
        }
    ],
}

with get_db() as db:
    job = db.query(Job).filter(Job.job_title == "Senior Python Developer").first()
    if not job:
        job = Job(**JOB_DATA)
        db.add(job)
        db.flush()
        print(f"[+] Created job id={job.id}")
    else:
        print(f"[=] Job already exists id={job.id}")

    resume = db.query(MasterResume).filter(MasterResume.name == "Demo Master Resume").first()
    if not resume:
        resume = MasterResume(
            name="Demo Master Resume",
            content=RESUME_CONTENT,
            is_active=True,
            created_at=datetime.datetime.utcnow(),
        )
        db.add(resume)
        db.flush()
        print(f"[+] Created resume id={resume.id}")
    else:
        print(f"[=] Resume already exists id={resume.id}")

    db.commit()
    print(f"\nREADY: --job-id {job.id} --resume-id {resume.id}")
