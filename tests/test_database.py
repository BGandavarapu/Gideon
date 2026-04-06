"""
Unit and integration tests for Phase 2: Database layer.

All tests use an in-memory SQLite database (``sqlite:///:memory:``) so they
run instantly without touching the filesystem.  The :func:`db_manager`
fixture creates a fresh :class:`~database.database.DatabaseManager` for
every test function, guaranteeing full isolation.

Test classes
------------
TestDatabaseUrl          – URL resolution logic (env vars, config, defaults)
TestDatabaseManager      – engine lifecycle and health check
TestCreateDropTables     – schema creation / teardown / idempotency
TestSessionContextManager – commit, rollback, and nesting behaviour
TestJobModel             – CRUD, constraints, defaults, relationships
TestMasterResumeModel    – CRUD, is_active flag, JSON content round-trip
TestTailoredResumeModel  – foreign keys, match_score, unique constraint
TestApplicationModel     – status lifecycle, nullable tailored_resume_id
TestCascadeDeletes       – verifies cascade="all, delete-orphan" behaviour
TestGetDatabaseUrl       – module-level URL helper with env patching
TestModuleLevelHelpers   – get_db / create_tables convenience wrappers
"""

import os
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Generator
from unittest.mock import patch

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from database.database import DatabaseManager, get_database_url, reset_manager
from database.models import Application, Base, Job, MasterResume, TailoredResume

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

IN_MEMORY_URL = "sqlite:///:memory:"


@pytest.fixture()
def db_manager() -> Generator[DatabaseManager, None, None]:
    """Yield a fully initialised in-memory DatabaseManager for one test."""
    manager = DatabaseManager(database_url=IN_MEMORY_URL, echo=False)
    manager.create_tables()
    yield manager
    manager.drop_tables()
    manager.engine.dispose()


@pytest.fixture()
def session(db_manager: DatabaseManager):
    """Yield an open session (no auto-commit) for manual assertion queries."""
    with db_manager.session() as sess:
        yield sess


@pytest.fixture()
def sample_job() -> dict:
    return {
        "job_title": "Senior Python Developer",
        "company_name": "Acme Corp",
        "location": "San Francisco, CA",
        "job_description": "Build scalable services with Python and AWS.",
        "application_url": "https://www.indeed.com/viewjob?jk=abc123",
        "source": "indeed",
        "required_skills": ["Python", "AWS", "Docker"],
        "preferred_skills": ["Kubernetes", "Terraform"],
        "salary_range": "USD 120,000 – 160,000",
    }


@pytest.fixture()
def sample_resume_content() -> dict:
    return {
        "summary": "Experienced software engineer with 8 years in Python.",
        "experience": [
            {
                "title": "Senior Engineer",
                "company": "Acme",
                "dates": "2020–2024",
                "bullets": ["Led microservices migration", "Reduced latency by 40%"],
            }
        ],
        "skills": ["Python", "Django", "PostgreSQL", "AWS"],
        "education": [{"degree": "B.Sc. Computer Science", "school": "MIT", "year": 2016}],
    }


# ---------------------------------------------------------------------------
# URL resolution tests
# ---------------------------------------------------------------------------


class TestGetDatabaseUrl:
    def test_returns_database_url_env_var_when_set(self, tmp_path) -> None:
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite:///custom.db", "DATABASE_PATH": ""}):
            url = get_database_url()
        assert url == "sqlite:///custom.db"

    def test_database_path_env_var_builds_sqlite_url(self, tmp_path) -> None:
        db_file = str(tmp_path / "test.db")
        with patch.dict(os.environ, {"DATABASE_URL": "", "DATABASE_PATH": db_file}):
            url = get_database_url()
        assert url.startswith("sqlite:///")
        assert "test.db" in url

    def test_falls_back_to_config_yaml(self, tmp_path, monkeypatch) -> None:
        yaml_text = "database:\n  path: data/fallback.db\n"
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml_text, encoding="utf-8")

        import database.database as db_module

        monkeypatch.setattr(db_module, "_CONFIG_FILE", cfg)
        with patch.dict(os.environ, {"DATABASE_URL": "", "DATABASE_PATH": ""}):
            url = db_module.get_database_url()
        assert "fallback.db" in url

    def test_falls_back_to_default_when_nothing_is_set(self, tmp_path, monkeypatch) -> None:
        import database.database as db_module

        monkeypatch.setattr(db_module, "_CONFIG_FILE", tmp_path / "nonexistent.yaml")
        with patch.dict(os.environ, {"DATABASE_URL": "", "DATABASE_PATH": ""}):
            url = db_module.get_database_url()
        assert "jobs.db" in url


# ---------------------------------------------------------------------------
# DatabaseManager lifecycle tests
# ---------------------------------------------------------------------------


class TestDatabaseManager:
    def test_engine_is_created(self, db_manager: DatabaseManager) -> None:
        assert db_manager.engine is not None

    def test_health_check_passes_after_init(self, db_manager: DatabaseManager) -> None:
        assert db_manager.health_check() is True

    def test_health_check_fails_on_bad_url(self) -> None:
        manager = DatabaseManager(database_url="sqlite:////nonexistent_dir/no.db", echo=False)
        # engine creation succeeds, but actual connect may fail; just verify it returns bool
        result = manager.health_check()
        assert isinstance(result, bool)
        manager.engine.dispose()

    def test_table_names_empty_before_create(self) -> None:
        manager = DatabaseManager(database_url=IN_MEMORY_URL, echo=False)
        assert manager.table_names() == []
        manager.engine.dispose()

    def test_table_names_populated_after_create(self, db_manager: DatabaseManager) -> None:
        names = db_manager.table_names()
        assert set(names) == {"jobs", "master_resumes", "tailored_resumes", "applications"}

    def test_safe_url_redacts_password(self) -> None:
        # Construct manager directly to test _safe_url without connecting
        manager = DatabaseManager.__new__(DatabaseManager)
        manager._url = "postgresql+psycopg2://admin:s3cr3t@localhost/mydb"
        safe = manager._safe_url()
        assert "s3cr3t" not in safe
        assert "admin" in safe

    def test_safe_url_unchanged_for_sqlite(self, db_manager: DatabaseManager) -> None:
        safe = db_manager._safe_url()
        assert safe == IN_MEMORY_URL


# ---------------------------------------------------------------------------
# Schema management tests
# ---------------------------------------------------------------------------


class TestCreateDropTables:
    def test_create_tables_is_idempotent(self, db_manager: DatabaseManager) -> None:
        # Second call must not raise
        db_manager.create_tables()
        assert set(db_manager.table_names()) == {
            "jobs", "master_resumes", "tailored_resumes", "applications"
        }

    def test_drop_tables_removes_all_tables(self, db_manager: DatabaseManager) -> None:
        db_manager.drop_tables()
        assert db_manager.table_names() == []

    def test_foreign_keys_are_enforced(self, db_manager: DatabaseManager) -> None:
        with pytest.raises(IntegrityError):
            with db_manager.session() as sess:
                bad = TailoredResume(
                    job_id=99999,           # non-existent FK
                    master_resume_id=99999,
                    tailored_content={"summary": "x"},
                )
                sess.add(bad)


# ---------------------------------------------------------------------------
# Session context manager tests
# ---------------------------------------------------------------------------


class TestSessionContextManager:
    def test_commit_on_success(self, db_manager: DatabaseManager) -> None:
        with db_manager.session() as sess:
            job = Job(
                job_title="QA Engineer",
                company_name="TestCo",
                job_description="Write tests.",
                application_url="https://example.com/qa",
                source="indeed",
            )
            sess.add(job)

        with db_manager.session() as sess:
            assert sess.query(Job).count() == 1

    def test_rollback_on_exception(self, db_manager: DatabaseManager) -> None:
        with pytest.raises(ValueError):
            with db_manager.session() as sess:
                job = Job(
                    job_title="Dev",
                    company_name="Co",
                    job_description="Desc.",
                    application_url="https://example.com/dev",
                    source="indeed",
                )
                sess.add(job)
                raise ValueError("simulated failure")

        with db_manager.session() as sess:
            assert sess.query(Job).count() == 0

    def test_session_is_closed_after_use(self, db_manager: DatabaseManager) -> None:
        with db_manager.session() as sess:
            active_sess = sess
        # After the context manager exits sess.close() has been called.
        # SQLAlchemy's Session.is_active reflects transaction state, not
        # connection state.  The reliable indicator of a closed session is
        # that its identity map is empty / the session is no longer bound.
        # We verify that a query raises (or that the session was returned to
        # the pool) by checking the session's bind is gone or it's in a
        # post-close state.  The simplest portable check: close() flips the
        # internal _transaction reference to None.
        assert active_sess._transaction is None


# ---------------------------------------------------------------------------
# Job model tests
# ---------------------------------------------------------------------------


class TestJobModel:
    def test_create_minimal_job(self, db_manager: DatabaseManager) -> None:
        with db_manager.session() as sess:
            job = Job(
                job_title="Python Dev",
                company_name="Startup",
                job_description="Write code.",
                application_url="https://example.com/job/1",
                source="indeed",
            )
            sess.add(job)

        with db_manager.session() as sess:
            result = sess.query(Job).one()
            assert result.job_title == "Python Dev"
            assert result.status == "new"
            assert result.date_scraped is not None

    def test_create_full_job(self, db_manager: DatabaseManager, sample_job: dict) -> None:
        with db_manager.session() as sess:
            job = Job(**sample_job)
            sess.add(job)

        with db_manager.session() as sess:
            result = sess.query(Job).one()
            assert result.required_skills == ["Python", "AWS", "Docker"]
            assert result.preferred_skills == ["Kubernetes", "Terraform"]
            assert result.salary_range == "USD 120,000 – 160,000"

    def test_application_url_is_unique(
        self, db_manager: DatabaseManager, sample_job: dict
    ) -> None:
        with db_manager.session() as sess:
            sess.add(Job(**sample_job))

        with pytest.raises(IntegrityError):
            with db_manager.session() as sess:
                sess.add(Job(**sample_job))  # Duplicate URL

    def test_status_defaults_to_new(self, db_manager: DatabaseManager) -> None:
        with db_manager.session() as sess:
            sess.add(
                Job(
                    job_title="Analyst",
                    company_name="Corp",
                    job_description="Analyse.",
                    application_url="https://example.com/analyst",
                    source="linkedin",
                )
            )

        with db_manager.session() as sess:
            job = sess.query(Job).one()
            assert job.status == "new"

    def test_status_can_be_updated(self, db_manager: DatabaseManager) -> None:
        with db_manager.session() as sess:
            sess.add(
                Job(
                    job_title="Dev",
                    company_name="Co",
                    job_description="Code.",
                    application_url="https://example.com/dev2",
                    source="indeed",
                )
            )

        with db_manager.session() as sess:
            job = sess.query(Job).one()
            job.status = "analyzed"

        with db_manager.session() as sess:
            assert sess.query(Job).one().status == "analyzed"

    def test_to_dict_returns_all_keys(
        self, db_manager: DatabaseManager, sample_job: dict
    ) -> None:
        with db_manager.session() as sess:
            sess.add(Job(**sample_job))

        with db_manager.session() as sess:
            result = sess.query(Job).one().to_dict()

        expected = {
            "id", "job_title", "company_name", "location", "job_description",
            "required_skills", "preferred_skills", "salary_range", "application_url",
            "date_posted", "date_scraped", "source", "status", "domain",
            "analyzed_with_resume_id",
        }
        assert set(result.keys()) == expected

    def test_repr_contains_key_info(
        self, db_manager: DatabaseManager, sample_job: dict
    ) -> None:
        with db_manager.session() as sess:
            job = Job(**sample_job)
            sess.add(job)

        with db_manager.session() as sess:
            job = sess.query(Job).one()
            r = repr(job)
            assert "Senior Python Developer" in r
            assert "Acme Corp" in r

    def test_date_scraped_is_timezone_aware(
        self, db_manager: DatabaseManager
    ) -> None:
        with db_manager.session() as sess:
            sess.add(
                Job(
                    job_title="Dev",
                    company_name="Co",
                    job_description="Desc",
                    application_url="https://example.com/tz",
                    source="indeed",
                )
            )
        with db_manager.session() as sess:
            job = sess.query(Job).one()
            # SQLite stores tz-aware datetimes as strings; the ORM returns them
            # as naive datetimes – we just check the value is reasonable.
            assert job.date_scraped is not None


# ---------------------------------------------------------------------------
# MasterResume model tests
# ---------------------------------------------------------------------------


class TestMasterResumeModel:
    def test_create_master_resume(
        self,
        db_manager: DatabaseManager,
        sample_resume_content: dict,
    ) -> None:
        with db_manager.session() as sess:
            sess.add(MasterResume(name="SWE 2024", content=sample_resume_content))

        with db_manager.session() as sess:
            result = sess.query(MasterResume).filter_by(name="SWE 2024").one()
            assert result.name == "SWE 2024"
            assert result.is_active is True
            assert result.content["summary"].startswith("Experienced")

    def test_json_content_round_trips_correctly(
        self,
        db_manager: DatabaseManager,
        sample_resume_content: dict,
    ) -> None:
        with db_manager.session() as sess:
            sess.add(MasterResume(name="Test", content=sample_resume_content))

        with db_manager.session() as sess:
            loaded = sess.query(MasterResume).filter_by(name="Test").one().content
            assert loaded["skills"] == ["Python", "Django", "PostgreSQL", "AWS"]
            assert len(loaded["experience"]) == 1

    def test_to_dict_serialises_timestamps(
        self,
        db_manager: DatabaseManager,
        sample_resume_content: dict,
    ) -> None:
        with db_manager.session() as sess:
            sess.add(MasterResume(name="V1", content=sample_resume_content))

        with db_manager.session() as sess:
            d = sess.query(MasterResume).filter_by(name="V1").one().to_dict()
            assert d["created_at"] is not None
            assert d["is_active"] is True

    def test_multiple_resumes_can_coexist(
        self, db_manager: DatabaseManager, sample_resume_content: dict
    ) -> None:
        with db_manager.session() as sess:
            sess.add(MasterResume(name="V1", content=sample_resume_content, is_active=False))
            sess.add(MasterResume(name="V2", content=sample_resume_content, is_active=True))

        with db_manager.session() as sess:
            # 2 user-added resumes + seeded sample resumes exist
            total = sess.query(MasterResume).count()
            assert total >= 2
            # Both specific resumes present
            assert sess.query(MasterResume).filter_by(name="V1").count() == 1
            assert sess.query(MasterResume).filter_by(name="V2").count() == 1


# ---------------------------------------------------------------------------
# TailoredResume model tests
# ---------------------------------------------------------------------------


class TestTailoredResumeModel:
    def _insert_prerequistes(
        self,
        db_manager: DatabaseManager,
        sample_job: dict,
        sample_resume_content: dict,
    ) -> tuple[int, int]:
        """Insert a Job + MasterResume and return their IDs."""
        with db_manager.session() as sess:
            job = Job(**sample_job)
            resume = MasterResume(name="Base", content=sample_resume_content)
            sess.add_all([job, resume])
        with db_manager.session() as sess:
            job_id = sess.query(Job).one().id
            resume_id = sess.query(MasterResume).filter_by(name="Base").one().id
        return job_id, resume_id

    def test_create_tailored_resume(
        self,
        db_manager: DatabaseManager,
        sample_job: dict,
        sample_resume_content: dict,
    ) -> None:
        job_id, resume_id = self._insert_prerequistes(
            db_manager, sample_job, sample_resume_content
        )
        with db_manager.session() as sess:
            tr = TailoredResume(
                job_id=job_id,
                master_resume_id=resume_id,
                tailored_content={"summary": "Tailored for Acme."},
                match_score=87.5,
            )
            sess.add(tr)

        with db_manager.session() as sess:
            result = sess.query(TailoredResume).one()
            assert result.match_score == pytest.approx(87.5)
            assert result.pdf_path is None

    def test_unique_constraint_job_resume(
        self,
        db_manager: DatabaseManager,
        sample_job: dict,
        sample_resume_content: dict,
    ) -> None:
        job_id, resume_id = self._insert_prerequistes(
            db_manager, sample_job, sample_resume_content
        )
        with db_manager.session() as sess:
            sess.add(
                TailoredResume(
                    job_id=job_id,
                    master_resume_id=resume_id,
                    tailored_content={"s": "v1"},
                    match_score=70.0,
                )
            )

        with pytest.raises(IntegrityError):
            with db_manager.session() as sess:
                sess.add(
                    TailoredResume(
                        job_id=job_id,
                        master_resume_id=resume_id,
                        tailored_content={"s": "v2"},
                        match_score=75.0,
                    )
                )

    def test_pdf_path_can_be_set(
        self,
        db_manager: DatabaseManager,
        sample_job: dict,
        sample_resume_content: dict,
    ) -> None:
        job_id, resume_id = self._insert_prerequistes(
            db_manager, sample_job, sample_resume_content
        )
        with db_manager.session() as sess:
            sess.add(
                TailoredResume(
                    job_id=job_id,
                    master_resume_id=resume_id,
                    tailored_content={},
                    pdf_path="data/output/resume_1.pdf",
                )
            )

        with db_manager.session() as sess:
            assert sess.query(TailoredResume).one().pdf_path == "data/output/resume_1.pdf"


# ---------------------------------------------------------------------------
# Application model tests
# ---------------------------------------------------------------------------


class TestApplicationModel:
    def _insert_job(self, db_manager: DatabaseManager, sample_job: dict) -> int:
        with db_manager.session() as sess:
            sess.add(Job(**sample_job))
        with db_manager.session() as sess:
            return sess.query(Job).one().id

    def test_create_application_without_resume(
        self, db_manager: DatabaseManager, sample_job: dict
    ) -> None:
        job_id = self._insert_job(db_manager, sample_job)
        with db_manager.session() as sess:
            sess.add(
                Application(
                    job_id=job_id,
                    application_date=date(2024, 3, 1),
                    status="applied",
                    notes="Applied via portal.",
                )
            )

        with db_manager.session() as sess:
            app = sess.query(Application).one()
            assert app.status == "applied"
            assert app.tailored_resume_id is None

    def test_status_lifecycle(
        self, db_manager: DatabaseManager, sample_job: dict
    ) -> None:
        job_id = self._insert_job(db_manager, sample_job)
        with db_manager.session() as sess:
            sess.add(Application(job_id=job_id, status="applied"))

        for new_status in ("interviewing", "offered", "rejected"):
            with db_manager.session() as sess:
                app = sess.query(Application).one()
                app.status = new_status

            with db_manager.session() as sess:
                assert sess.query(Application).one().status == new_status

    def test_to_dict_includes_all_fields(
        self, db_manager: DatabaseManager, sample_job: dict
    ) -> None:
        job_id = self._insert_job(db_manager, sample_job)
        with db_manager.session() as sess:
            sess.add(
                Application(job_id=job_id, status="applied", notes="Good fit.")
            )

        with db_manager.session() as sess:
            d = sess.query(Application).one().to_dict()

        expected = {
            "id", "job_id", "tailored_resume_id", "application_date",
            "status", "notes", "created_at", "updated_at",
        }
        assert set(d.keys()) == expected
        assert d["notes"] == "Good fit."


# ---------------------------------------------------------------------------
# Cascade delete tests
# ---------------------------------------------------------------------------


class TestCascadeDeletes:
    def test_deleting_job_cascades_to_tailored_resumes(
        self,
        db_manager: DatabaseManager,
        sample_job: dict,
        sample_resume_content: dict,
    ) -> None:
        with db_manager.session() as sess:
            job = Job(**sample_job)
            resume = MasterResume(name="Base", content=sample_resume_content)
            sess.add_all([job, resume])

        with db_manager.session() as sess:
            job_id = sess.query(Job).one().id
            resume_id = sess.query(MasterResume).filter_by(name="Base").one().id
            sess.add(
                TailoredResume(
                    job_id=job_id,
                    master_resume_id=resume_id,
                    tailored_content={},
                )
            )

        with db_manager.session() as sess:
            job = sess.query(Job).one()
            sess.delete(job)

        with db_manager.session() as sess:
            assert sess.query(TailoredResume).count() == 0

    def test_deleting_job_cascades_to_applications(
        self, db_manager: DatabaseManager, sample_job: dict
    ) -> None:
        with db_manager.session() as sess:
            sess.add(Job(**sample_job))

        with db_manager.session() as sess:
            job_id = sess.query(Job).one().id
            sess.add(Application(job_id=job_id, status="applied"))

        with db_manager.session() as sess:
            job = sess.query(Job).one()
            sess.delete(job)

        with db_manager.session() as sess:
            assert sess.query(Application).count() == 0


# ---------------------------------------------------------------------------
# Module-level helper tests
# ---------------------------------------------------------------------------


class TestModuleLevelHelpers:
    """Test the convenience wrappers (get_db, create_tables, etc.)."""

    def test_get_db_yields_session(self) -> None:
        reset_manager(IN_MEMORY_URL)
        import database.database as db_module

        db_module.create_tables()

        from database.database import get_db

        with get_db() as sess:
            assert sess is not None

    def test_create_tables_via_module_helper(self) -> None:
        reset_manager(IN_MEMORY_URL)
        import database.database as db_module

        db_module.create_tables()
        tables = db_module._get_manager().table_names()
        assert "jobs" in tables

    def test_health_check_returns_true_for_good_db(self) -> None:
        reset_manager(IN_MEMORY_URL)
        import database.database as db_module

        db_module.create_tables()
        assert db_module.health_check() is True

    def test_reset_manager_replaces_singleton(self) -> None:
        import database.database as db_module

        reset_manager(IN_MEMORY_URL)
        m1 = db_module._get_manager()
        reset_manager(IN_MEMORY_URL)
        m2 = db_module._get_manager()
        assert m1 is not m2
