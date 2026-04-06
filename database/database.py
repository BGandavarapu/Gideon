"""
Database connection management for the Gideon application.

Provides:
    - :class:`DatabaseManager` – engine + session factory with connection pooling.
    - :func:`get_db` – context manager yielding a scoped ``Session``; safe for
      use in both application code and tests.
    - :func:`create_tables` – idempotent table creation (``CREATE TABLE IF NOT EXISTS``).
    - :func:`drop_tables` – test/dev utility to tear everything down.
    - :func:`get_database_url` – resolves the URL from env / config with fallback.

Environment variables
---------------------
DATABASE_URL
    Full SQLAlchemy connection string, e.g.
    ``sqlite:///data/jobs.db`` or ``postgresql+psycopg2://user:pw@host/db``.
    Takes priority over everything else.

DATABASE_PATH
    SQLite file path relative to the project root (e.g. ``data/jobs.db``).
    Used when ``DATABASE_URL`` is not set.

If neither variable is set the module falls back to ``config.yaml``
``database.path``, and finally to the hard-coded default ``data/jobs.db``.
"""

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional

import yaml
from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from database.models import Base

logger = logging.getLogger(__name__)

# Resolved path to the repository-root config file.
_CONFIG_FILE = Path(__file__).resolve().parent.parent / "config.yaml"
_DEFAULT_DB_PATH = "data/jobs.db"


# ---------------------------------------------------------------------------
# URL resolution
# ---------------------------------------------------------------------------


def get_database_url() -> str:
    """Resolve the database connection URL from the environment or config.

    Priority order:
    1. ``DATABASE_URL`` environment variable (full SQLAlchemy URL).
    2. ``DATABASE_PATH`` environment variable (SQLite path shorthand).
    3. ``database.path`` key in ``config.yaml``.
    4. Built-in default: ``sqlite:///data/jobs.db``.

    Returns:
        A valid SQLAlchemy database URL string.
    """
    # 1. Explicit full URL (e.g. for PostgreSQL in production)
    full_url = os.getenv("DATABASE_URL", "").strip()
    if full_url:
        logger.debug("Using DATABASE_URL from environment.")
        return full_url

    # 2. SQLite path shorthand
    db_path = os.getenv("DATABASE_PATH", "").strip()
    if db_path:
        logger.debug("Using DATABASE_PATH=%s from environment.", db_path)
        return _sqlite_url(db_path)

    # 3. config.yaml
    if _CONFIG_FILE.exists():
        try:
            with _CONFIG_FILE.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
            cfg_path = raw.get("database", {}).get("path", "")
            if cfg_path:
                logger.debug("Using database.path=%s from config.yaml.", cfg_path)
                return _sqlite_url(cfg_path)
        except yaml.YAMLError as exc:
            logger.warning("Could not read config.yaml: %s – using default.", exc)

    # 4. Hard-coded default
    logger.debug("Using built-in default database path: %s", _DEFAULT_DB_PATH)
    return _sqlite_url(_DEFAULT_DB_PATH)


def _sqlite_url(path: str) -> str:
    """Convert a file path to an absolute ``sqlite:///`` URL.

    Creates the parent directory if it does not already exist.

    Args:
        path: Relative or absolute SQLite file path.

    Returns:
        Absolute ``sqlite:///`` URL string.
    """
    resolved = Path(path).resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{resolved}"


# ---------------------------------------------------------------------------
# DatabaseManager
# ---------------------------------------------------------------------------


class DatabaseManager:
    """Manages a SQLAlchemy engine, connection pool, and session factory.

    Designed as a lightweight singleton.  Call :func:`get_db` (the module-
    level convenience wrapper) rather than instantiating this class directly
    in application code.

    Args:
        database_url: SQLAlchemy connection URL.  Defaults to the result of
            :func:`get_database_url`.
        echo: If ``True``, SQLAlchemy logs all emitted SQL (useful for
            debugging; reads ``echo_sql`` from ``config.yaml`` by default).
        pool_size: Number of connections to keep open (ignored for SQLite
            which uses a ``StaticPool`` / ``NullPool``).

    Attributes:
        engine: The underlying :class:`~sqlalchemy.engine.Engine`.
        SessionFactory: A bound :class:`~sqlalchemy.orm.sessionmaker`.
    """

    def __init__(
        self,
        database_url: Optional[str] = None,
        echo: Optional[bool] = None,
        pool_size: int = 5,
    ) -> None:
        self._url = database_url or get_database_url()
        self._echo = echo if echo is not None else self._load_echo_flag()
        self._pool_size = pool_size
        self.engine: Engine = self._create_engine()
        self.SessionFactory: sessionmaker[Session] = sessionmaker(
            bind=self.engine,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,  # Allows attribute access after commit
        )
        logger.info("DatabaseManager initialised: %s", self._safe_url())

    # ------------------------------------------------------------------
    # Engine construction
    # ------------------------------------------------------------------

    def _create_engine(self) -> Engine:
        """Build and configure the SQLAlchemy engine.

        SQLite receives special treatment:
        - ``check_same_thread=False`` allows the connection to be used
          from multiple threads (safe with our session-per-request pattern).
        - A ``PRAGMA foreign_keys = ON`` event listener activates FK
          enforcement, which SQLite disables by default.

        Returns:
            Configured :class:`~sqlalchemy.engine.Engine`.
        """
        is_sqlite = self._url.startswith("sqlite")
        connect_args: dict = {}

        if is_sqlite:
            connect_args["check_same_thread"] = False
            # Avoid indefinite hangs when another process holds the DB lock
            # (e.g. two Flask dev servers on the same port / same jobs.db).
            connect_args["timeout"] = 30
            engine = create_engine(
                self._url,
                echo=self._echo,
                connect_args=connect_args,
            )
            # Enable FK enforcement for every new SQLite connection
            @event.listens_for(engine, "connect")
            def _set_sqlite_pragma(dbapi_conn, _connection_record) -> None:  # type: ignore[misc]
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")  # Better concurrency
                cursor.close()

        else:
            engine = create_engine(
                self._url,
                echo=self._echo,
                pool_size=self._pool_size,
                pool_pre_ping=True,  # Detect stale connections
                pool_recycle=3600,   # Recycle connections after 1 hour
            )

        logger.debug("Engine created for %s (echo=%s).", self._safe_url(), self._echo)
        return engine

    # ------------------------------------------------------------------
    # Session context manager
    # ------------------------------------------------------------------

    @contextmanager
    def session(self) -> Generator[Session, None, None]:
        """Yield a database session, committing on success or rolling back on error.

        Usage::

            with db_manager.session() as sess:
                job = sess.get(Job, 1)
                job.status = "analyzed"
                # Commit happens automatically on context-manager exit

        Yields:
            An active :class:`~sqlalchemy.orm.Session`.

        Raises:
            SQLAlchemyError: Re-raised after rollback for the caller to handle.
        """
        sess: Session = self.SessionFactory()
        try:
            yield sess
            sess.commit()
            logger.debug("Session committed successfully.")
        except SQLAlchemyError as exc:
            sess.rollback()
            logger.error("Session rolled back due to error: %s", exc)
            raise
        except Exception as exc:
            sess.rollback()
            logger.error("Unexpected error; session rolled back: %s", exc)
            raise
        finally:
            sess.close()

    # ------------------------------------------------------------------
    # Schema management
    # ------------------------------------------------------------------

    def create_tables(self) -> None:
        """Create all tables defined on :class:`~database.models.Base`.

        This is idempotent (``checkfirst=True`` / ``CREATE TABLE IF NOT
        EXISTS``), so it is safe to call on every startup.

        Also applies safe column migrations for columns added after initial
        schema creation (e.g. ``is_sample`` on ``master_resumes``).

        Raises:
            SQLAlchemyError: If the engine cannot connect or DDL fails.
        """
        try:
            Base.metadata.create_all(self.engine, checkfirst=True)
            table_names = list(Base.metadata.tables.keys())
            logger.info("Tables ensured: %s", table_names)
        except SQLAlchemyError as exc:
            logger.error("Failed to create tables: %s", exc)
            raise

        # Safe migrations — add columns that may be missing in existing DBs.
        self._migrate_add_column_if_missing(
            "master_resumes", "is_sample", "BOOLEAN DEFAULT 0 NOT NULL"
        )
        self._migrate_add_column_if_missing(
            "master_resumes", "style_fingerprint", "JSON"
        )
        self._migrate_add_column_if_missing(
            "master_resumes", "domain", "VARCHAR(50)"
        )
        self._migrate_add_column_if_missing(
            "jobs", "domain", "VARCHAR(50)"
        )
        self._migrate_add_column_if_missing(
            "master_resumes", "domains", "JSON"
        )
        self._migrate_add_column_if_missing(
            "jobs", "analyzed_with_resume_id", "INTEGER"
        )
        self._migrate_add_column_if_missing(
            "tailored_resumes", "score_breakdown", "JSON"
        )
        # Mark existing 'Master Resume v1' rows as sample if not already done.
        self._seed_sample_flag()
        # Seed one sample resume per industry from data/sample_resumes/.
        self._seed_sample_resumes()

    def _migrate_add_column_if_missing(
        self, table: str, column: str, col_def: str
    ) -> None:
        """Add *column* to *table* if it does not already exist.

        Uses ``PRAGMA table_info`` for SQLite.  Logs but never raises so
        startup is not blocked on a non-critical migration.
        """
        try:
            from sqlalchemy import text as _text

            with self.engine.connect() as conn:
                # SQLite-compatible introspection
                rows = conn.execute(
                    _text(f"PRAGMA table_info({table})")
                ).fetchall()
                existing = {row[1] for row in rows}  # column names
                if column not in existing:
                    conn.execute(
                        _text(
                            f"ALTER TABLE {table} ADD COLUMN {column} {col_def}"
                        )
                    )
                    conn.commit()
                    logger.info(
                        "Migration: added column '%s' to '%s'.", column, table
                    )
        except Exception as exc:
            logger.warning(
                "Migration warning for %s.%s: %s", table, column, exc
            )

    def _seed_sample_flag(self) -> None:
        """Mark known sample resume rows as ``is_sample=True``.

        Handles:
        - Legacy "Master Resume v1" row
        - Any row whose name ends with "Sample Resume"

        Safe to call multiple times — only updates rows where the flag is
        still 0.
        """
        try:
            from sqlalchemy import text as _text

            with self.engine.connect() as conn:
                # Legacy row
                conn.execute(
                    _text(
                        "UPDATE master_resumes SET is_sample = 1 "
                        "WHERE name = 'Master Resume v1' AND is_sample = 0"
                    )
                )
                # Any row whose name ends with "Sample Resume"
                conn.execute(
                    _text(
                        "UPDATE master_resumes SET is_sample = 1 "
                        "WHERE name LIKE '%Sample Resume' AND is_sample = 0"
                    )
                )
                conn.commit()
        except Exception as exc:
            logger.warning("Could not seed is_sample flag: %s", exc)

    def _seed_sample_resumes(self) -> None:
        """Seed one sample MasterResume per industry from ``data/sample_resumes/``.

        Fully idempotent — skips any domain that already has a sample resume.
        Also ensures the legacy "Master Resume v1" has its domain set.
        """
        import json as _json
        from pathlib import Path as _Path

        sample_dir = _Path("data/sample_resumes")
        if not sample_dir.exists():
            logger.debug("data/sample_resumes/ not found — skipping sample seeding.")
            return

        # Map filename stem → domain key (must match DOMAINS keys)
        domain_name_map = {
            "software_engineering": "Software Engineering Sample Resume",
            "ai_ml": "AI / ML Sample Resume",
            "product_management": "Product Management Sample Resume",
            "marketing": "Marketing Sample Resume",
            "data_analytics": "Data Analytics Sample Resume",
            "design": "Design Sample Resume",
            "finance": "Finance Sample Resume",
            "sales": "Sales Sample Resume",
            "operations": "Operations Sample Resume",
        }

        try:
            from sqlalchemy.orm import Session as _Session
            from database.models import MasterResume as _MR

            with _Session(self.engine) as session:
                # Ensure legacy sample has domain set
                legacy = (
                    session.query(_MR)
                    .filter(_MR.is_sample.is_(True), _MR.domain.is_(None))
                    .first()
                )
                if legacy:
                    legacy.domain = "software_engineering"
                    session.commit()
                    logger.info("Updated legacy sample resume domain to 'software_engineering'.")

                for domain, resume_name in domain_name_map.items():
                    json_file = sample_dir / f"{domain}.json"
                    if not json_file.exists():
                        logger.debug("Sample resume file missing: %s", json_file)
                        continue

                    # Skip if already seeded for this domain
                    existing = (
                        session.query(_MR)
                        .filter(_MR.is_sample.is_(True), _MR.domain == domain)
                        .first()
                    )
                    if existing:
                        logger.debug("Sample resume already seeded for domain: %s", domain)
                        continue

                    try:
                        content = _json.loads(json_file.read_text(encoding="utf-8"))
                    except Exception as _je:
                        logger.warning("Could not load sample resume %s: %s", json_file, _je)
                        continue

                    # Extract style fingerprint
                    style = None
                    try:
                        from resume_engine.style_extractor import StyleExtractor as _SE
                        style = _SE().extract(content)
                    except Exception as _se:
                        logger.debug("Style extraction failed for %s: %s", domain, _se)

                    mr = _MR(
                        name=resume_name,
                        content=content,
                        is_active=False,
                        is_sample=True,
                        domain=domain,
                        style_fingerprint=style,
                    )
                    session.add(mr)
                    session.commit()
                    logger.info("Seeded sample resume: %s", resume_name)

        except Exception as exc:
            logger.warning("Could not seed sample resumes: %s", exc)

    def drop_tables(self) -> None:
        """Drop all application tables (destructive – use only in tests/dev).

        Raises:
            SQLAlchemyError: If the DDL operation fails.
        """
        try:
            Base.metadata.drop_all(self.engine)
            logger.warning("All application tables dropped.")
        except SQLAlchemyError as exc:
            logger.error("Failed to drop tables: %s", exc)
            raise

    def table_names(self) -> list[str]:
        """Return the names of tables that currently exist in the database.

        Returns:
            Sorted list of table name strings.
        """
        inspector = inspect(self.engine)
        return sorted(inspector.get_table_names())

    def health_check(self) -> bool:
        """Verify the engine can connect and execute a trivial query.

        Returns:
            ``True`` if the connection is healthy, ``False`` otherwise.
        """
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            logger.debug("Database health check passed.")
            return True
        except SQLAlchemyError as exc:
            logger.error("Database health check failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _safe_url(self) -> str:
        """Return the database URL with any password redacted."""
        if "@" in self._url:
            # e.g. postgresql+psycopg2://user:SECRET@host/db
            scheme, rest = self._url.split("://", 1)
            credentials, host_db = rest.rsplit("@", 1)
            user = credentials.split(":")[0]
            return f"{scheme}://{user}:***@{host_db}"
        return self._url

    @staticmethod
    def _load_echo_flag() -> bool:
        """Read the ``database.echo_sql`` key from config.yaml.

        Returns:
            ``True`` if SQL echo is enabled, ``False`` otherwise.
        """
        if not _CONFIG_FILE.exists():
            return False
        try:
            with _CONFIG_FILE.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
            return bool(raw.get("database", {}).get("echo_sql", False))
        except yaml.YAMLError:
            return False


# ---------------------------------------------------------------------------
# Module-level singleton and convenience wrappers
# ---------------------------------------------------------------------------

# Lazily initialised on first access so tests can patch the URL before import.
_manager: Optional[DatabaseManager] = None


def _get_manager() -> DatabaseManager:
    """Return (or create) the module-level :class:`DatabaseManager` singleton.

    Returns:
        The shared :class:`DatabaseManager` instance.
    """
    global _manager
    if _manager is None:
        _manager = DatabaseManager()
    return _manager


@contextmanager
def get_db() -> Generator[Session, None, None]:
    """Module-level context manager for database sessions.

    Delegates to the shared :class:`DatabaseManager` singleton.  This is
    the primary interface used by application code and CLI commands.

    Usage::

        from database.database import get_db
        from database.models import Job

        with get_db() as db:
            jobs = db.query(Job).filter(Job.status == "new").all()

    Yields:
        An active :class:`~sqlalchemy.orm.Session`.
    """
    with _get_manager().session() as sess:
        yield sess


def create_tables() -> None:
    """Create all application tables in the configured database.

    Idempotent – safe to call on every application startup.

    Raises:
        SQLAlchemyError: If table creation fails.
    """
    _get_manager().create_tables()


def drop_tables() -> None:
    """Drop all application tables (destructive).

    Intended for use in automated tests and development resets only.
    """
    _get_manager().drop_tables()


def health_check() -> bool:
    """Return whether the database connection is healthy.

    Returns:
        ``True`` if a SELECT 1 succeeds, ``False`` otherwise.
    """
    return _get_manager().health_check()


def reset_manager(database_url: Optional[str] = None) -> None:
    """Replace the module-level singleton with a fresh instance.

    Used by tests that need an isolated in-memory database.

    Args:
        database_url: Optional URL for the new instance (e.g.
            ``"sqlite:///:memory:"`` for in-memory testing).
    """
    global _manager
    if _manager is not None:
        try:
            _manager.engine.dispose()
        except Exception:  # noqa: BLE001
            pass
    _manager = DatabaseManager(database_url=database_url)
