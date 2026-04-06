"""
Database package for the Gideon application.

Public surface
--------------
Models (ORM classes):
    Job, MasterResume, TailoredResume, Application, Base

Session / connection management:
    get_db()        – context manager yielding a Session
    create_tables() – idempotent schema initialisation
    drop_tables()   – destructive teardown (tests / dev only)
    health_check()  – connectivity probe returning bool
    reset_manager() – replace the singleton (used in tests)

Example
-------
    from database import Job, get_db

    with get_db() as db:
        jobs = db.query(Job).filter(Job.status == "new").all()
"""

from database.models import Application, Base, Job, MasterResume, TailoredResume
from database.database import (
    create_tables,
    drop_tables,
    get_db,
    get_database_url,
    health_check,
    reset_manager,
)

__all__ = [
    # Models
    "Base",
    "Job",
    "MasterResume",
    "TailoredResume",
    "Application",
    # Connection helpers
    "get_db",
    "create_tables",
    "drop_tables",
    "get_database_url",
    "health_check",
    "reset_manager",
]
