"""
Gideon – application entry point.

Bootstraps logging, loads environment variables, and hosts the Click CLI.
Commands are added incrementally as phases are implemented:

    Phase 1  scrape          – scrape job postings from LinkedIn / Indeed
    Phase 2  init-db         – initialise the SQLite database schema
             db-status       – show connection health and row counts
             version         – print application version
             list-jobs       – query jobs stored in the database

Usage examples::

    python main.py version
    python main.py init-db
    python main.py db-status
    python main.py scrape --source indeed --keywords "python dev" --location "Remote"
    python main.py list-jobs --status new --limit 20
"""

import json
import logging
import logging.handlers
import sys
from pathlib import Path

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

# Load .env before any module that might read env vars.
load_dotenv()

__version__ = "0.2.0"

# Force UTF-8 output on Windows terminals that default to CP-1252
console = Console(highlight=False)


def _safe(text: str) -> str:
    """Replace characters that can't be encoded in cp1252 with '?'.

    LinkedIn and other sources embed invisible Unicode (e.g. U+200C zero-width
    non-joiner) that crashes the Windows console renderer.  This helper is
    called before any user-sourced text reaches ``console.print``.
    """
    try:
        text.encode("cp1252")
        return text
    except (UnicodeEncodeError, AttributeError):
        return "".join(
            c if ord(c) < 256 else "?"
            for c in str(text)
        )


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def configure_logging(log_level: str = "INFO") -> None:
    """Configure root logger with console and rotating file handlers.

    Prevents duplicate handlers when the CLI group is called multiple times
    (e.g. in tests) by checking if handlers are already registered.

    Args:
        log_level: String log level (``"DEBUG"``, ``"INFO"``, etc.).
    """
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    root_logger = logging.getLogger()

    # Avoid adding duplicate handlers on repeated calls
    if root_logger.handlers:
        root_logger.setLevel(numeric_level)
        return

    root_logger.setLevel(numeric_level)

    fmt = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(fmt)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "app.log",
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)


# ---------------------------------------------------------------------------
# CLI root group
# ---------------------------------------------------------------------------


@click.group()
@click.option(
    "--log-level",
    default="INFO",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    show_default=True,
    help="Set the logging verbosity level.",
)
@click.pass_context
def cli(ctx: click.Context, log_level: str) -> None:
    """Gideon – Your Personal Employer."""
    configure_logging(log_level)
    ctx.ensure_object(dict)
    ctx.obj["log_level"] = log_level


# ---------------------------------------------------------------------------
# Version command
# ---------------------------------------------------------------------------


@cli.command()
def version() -> None:
    """Print the application version and exit."""
    console.print(f"[bold cyan]Gideon[/bold cyan] v{__version__}")


# ---------------------------------------------------------------------------
# Database commands
# ---------------------------------------------------------------------------


@cli.command("init-db")
def init_db() -> None:
    """Initialise the database and create all required tables.

    Safe to run multiple times – existing tables are never dropped.
    """
    from database.database import create_tables, get_database_url, health_check

    db_url = get_database_url()
    console.print(f"[dim]Database:[/dim] {db_url}")

    try:
        create_tables()
        console.print("[green][OK][/green] Database initialised successfully.")
        console.print("[green][OK][/green] Tables created: jobs, master_resumes, tailored_resumes, applications")
    except Exception as exc:
        console.print(f"[red][!!][/red] Failed to initialise database: {exc}")
        sys.exit(1)

    if health_check():
        console.print("[green][OK][/green] Connection health check passed.")
    else:
        console.print("[yellow][!!][/yellow] Health check failed - check your database configuration.")


@cli.command("db-status")
def db_status() -> None:
    """Show database connection status and row counts per table."""
    from database.database import (
        DatabaseManager,
        get_database_url,
        health_check,
        reset_manager,
    )
    from database.models import Application, Job, MasterResume, TailoredResume

    db_url = get_database_url()
    console.print(f"\n[bold]Database URL:[/bold] {db_url}")

    if not health_check():
        console.print("[red][!!][/red] Cannot connect to database. Run [bold]init-db[/bold] first.")
        sys.exit(1)

    console.print("[green][OK][/green] Connection healthy\n")

    from database.database import get_db

    try:
        with get_db() as db:
            counts = {
                "jobs": db.query(Job).count(),
                "master_resumes": db.query(MasterResume).count(),
                "tailored_resumes": db.query(TailoredResume).count(),
                "applications": db.query(Application).count(),
            }
    except Exception as exc:
        console.print(f"[red][!!][/red] Could not query tables: {exc}")
        console.print("[dim]Hint: run [bold]init-db[/bold] to create the schema.[/dim]")
        sys.exit(1)

    table = Table(title="Table Row Counts", show_header=True, header_style="bold magenta")
    table.add_column("Table", style="cyan")
    table.add_column("Rows", justify="right")

    for table_name, count in counts.items():
        table.add_row(table_name, str(count))

    console.print(table)


# ---------------------------------------------------------------------------
# Job listing command (Phase 2 database integration)
# ---------------------------------------------------------------------------


@cli.command("list-jobs")
@click.option(
    "--status",
    default=None,
    type=click.Choice(["new", "analyzed", "applied"], case_sensitive=False),
    help="Filter by job status.",
)
@click.option(
    "--source",
    default=None,
    type=click.Choice(["linkedin", "indeed"], case_sensitive=False),
    help="Filter by scraping source.",
)
@click.option(
    "--limit",
    default=25,
    show_default=True,
    type=click.IntRange(1, 500),
    help="Maximum number of jobs to display.",
)
@click.option(
    "--output",
    default=None,
    type=click.Path(dir_okay=False, writable=True),
    help="Export results to a JSON file.",
)
def list_jobs(
    status: str | None,
    source: str | None,
    limit: int,
    output: str | None,
) -> None:
    """List scraped jobs stored in the database.

    Examples:

    \b
        python main.py list-jobs
        python main.py list-jobs --status new --limit 10
        python main.py list-jobs --source indeed --output jobs.json
    """
    from database.database import get_db
    from database.models import Job

    try:
        with get_db() as db:
            query = db.query(Job)
            if status:
                query = query.filter(Job.status == status.lower())
            if source:
                query = query.filter(Job.source == source.lower())
            query = query.order_by(Job.date_scraped.desc()).limit(limit)
            jobs = query.all()
    except Exception as exc:
        console.print(f"[red][!!][/red] Database error: {exc}")
        console.print("[dim]Hint: run [bold]init-db[/bold] first.[/dim]")
        sys.exit(1)

    if not jobs:
        console.print("[yellow]No jobs found matching the given filters.[/yellow]")
        return

    table = Table(
        title=f"Jobs ({len(jobs)} result{'s' if len(jobs) != 1 else ''})",
        show_header=True,
        header_style="bold magenta",
    )
    table.add_column("ID", style="dim", width=5)
    table.add_column("Title", style="cyan", max_width=35)
    table.add_column("Company", max_width=25)
    table.add_column("Location", max_width=20)
    table.add_column("Source", width=10)
    table.add_column("Status", width=10)
    table.add_column("Scraped", width=12)

    for job in jobs:
        scraped = job.date_scraped.strftime("%Y-%m-%d") if job.date_scraped else "-"
        table.add_row(
            str(job.id),
            job.job_title,
            job.company_name,
            job.location or "-",
            job.source,
            job.status,
            scraped,
        )

    console.print(table)

    if output:
        _save_jobs_to_json(jobs, Path(output))
        console.print(f"\n[green][OK][/green] Exported to {output}")


def _save_jobs_to_json(jobs: list, output_path: Path) -> None:
    """Serialise a list of Job ORM objects to a JSON file.

    Args:
        jobs: List of :class:`~database.models.Job` instances.
        output_path: Destination file path.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data = [job.to_dict() for job in jobs]
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    logging.getLogger(__name__).info("Exported %d jobs to %s.", len(jobs), output_path)


# ---------------------------------------------------------------------------
# Scrape command (Phase 1 + DB integration)
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--source",
    default="indeed",
    show_default=True,
    type=click.Choice(["linkedin", "indeed"], case_sensitive=False),
    help="Job board to scrape.",
)
@click.option(
    "--keywords",
    required=True,
    help='Search keywords (e.g. "python developer").',
)
@click.option(
    "--location",
    default="",
    show_default=True,
    help='Location filter (e.g. "San Francisco, CA"). Leave blank for remote/any.',
)
@click.option(
    "--max-results",
    default=20,
    show_default=True,
    type=click.IntRange(1, 50),
    help="Maximum number of job postings to collect per run.",
)
@click.option(
    "--save-db/--no-save-db",
    default=True,
    show_default=True,
    help="Persist scraped jobs to the database (requires init-db first).",
)
@click.option(
    "--output",
    default=None,
    type=click.Path(dir_okay=False, writable=True),
    help="Optional path to also save results as JSON.",
)
def scrape(
    source: str,
    keywords: str,
    location: str,
    max_results: int,
    save_db: bool,
    output: str | None,
) -> None:
    """Scrape job postings from a job board and save them to the database.

    A live progress bar shows each URL as it is parsed.  Jobs are saved
    (or updated if already present) automatically unless --no-save-db is set.

    Examples:

    \b
        python main.py scrape --keywords "python developer" --location "Austin, TX"
        python main.py scrape --source indeed --keywords "data analyst" --max-results 10
        python main.py scrape --keywords "MLOps" --no-save-db --output results.json
    """
    logger = logging.getLogger(__name__)
    logger.info(
        "Scrape started: source=%s, keywords=%r, location=%r, max=%d",
        source, keywords, location, max_results,
    )

    console.print(
        f"\nScraping [bold]{source.title()}[/bold] for "
        f"[cyan]{keywords!r}[/cyan]"
        + (f" in [cyan]{location}[/cyan]" if location else "")
        + f"  (max {max_results})\n"
    )

    postings = _run_scraper_with_progress(source, keywords, location, max_results)

    if not postings:
        console.print(
            "[yellow]No jobs found. "
            "The site layout may have changed - check the logs.[/yellow]"
        )
        sys.exit(1)

    _display_scrape_summary(postings)

    if save_db:
        _persist_postings_with_progress(postings)
    else:
        console.print("[dim](--no-save-db: results not persisted to database)[/dim]")

    if output:
        _save_postings_to_json(postings, Path(output))
        console.print(f"\n[green][OK][/green] Also exported to {output}")


def _run_scraper_with_progress(
    source: str, keywords: str, location: str, max_results: int
) -> list:
    """Run the appropriate scraper with a Rich live progress bar.

    Args:
        source: ``"linkedin"`` or ``"indeed"``.
        keywords: Search keyword string.
        location: Geographic filter string.
        max_results: Per-run cap on number of jobs.

    Returns:
        List of :class:`~scraper.base_scraper.JobPosting` objects.
    """
    from scraper.indeed_scraper import IndeedScraper
    from scraper.linkedin_scraper import LinkedInScraper

    ScraperClass = LinkedInScraper if source == "linkedin" else IndeedScraper

    postings: list = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        # Phase 1: collect URLs
        url_task = progress.add_task("Collecting job URLs...", total=None)

        def on_progress(completed: int, total: int, posting) -> None:
            progress.update(parse_task, completed=completed, total=total)
            if posting:
                progress.update(
                    parse_task,
                    description=f"Parsed: {posting.job_title[:40]}",
                )

        with ScraperClass() as scraper:
            # Kick off URL collection (spinner shows while paginating)
            urls = scraper._fetch_job_urls(keywords, location)
            progress.update(url_task, completed=1, total=1,
                            description=f"Found {len(urls)} URL(s)")

            if not urls:
                return postings

            # Phase 2: parse each URL
            parse_task = progress.add_task(
                "Parsing job pages...", total=len(urls)
            )
            postings = scraper.scrape(
                keywords,
                location,
                max_results=max_results,
                on_progress=on_progress,
            )

    return postings


def _display_scrape_summary(postings: list) -> None:
    """Print a Rich table summarising the freshly scraped postings.

    Args:
        postings: List of :class:`~scraper.base_scraper.JobPosting` objects.
    """
    table = Table(
        title=f"Scraped Jobs ({len(postings)})",
        show_header=True,
        header_style="bold magenta",
    )
    table.add_column("#", style="dim", width=4)
    table.add_column("Title", style="cyan", max_width=36)
    table.add_column("Company", max_width=26)
    table.add_column("Location", max_width=20)
    table.add_column("Posted", width=11)
    table.add_column("Salary", max_width=18)

    for idx, p in enumerate(postings, start=1):
        table.add_row(
            str(idx),
            p.job_title,
            p.company_name,
            p.location or "-",
            p.date_posted.isoformat() if p.date_posted else "-",
            p.salary_range or "-",
        )

    console.print(table)


def _persist_postings_with_progress(postings: list) -> None:
    """Save postings to the DB using db_handler with a progress bar.

    Delegates to :func:`~scraper.db_handler.save_postings_to_db` which
    handles upsert semantics and per-row error isolation.

    Args:
        postings: List of :class:`~scraper.base_scraper.JobPosting` objects.
    """
    from scraper.db_handler import save_postings_to_db

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Saving to database...", total=len(postings))

        def on_db_progress(idx: int, total: int, posting, is_new) -> None:
            verb = "Saved" if is_new else ("Updated" if is_new is False else "Failed")
            progress.update(
                task,
                completed=idx,
                description=f"{verb}: {posting.job_title[:35]}",
            )

        result = save_postings_to_db(postings, on_progress=on_db_progress)

    console.print(
        f"\n[green][OK][/green] Database: "
        f"[bold]{result.saved}[/bold] new  |  "
        f"[bold]{result.updated}[/bold] updated  |  "
        f"[bold]{result.failed}[/bold] failed"
    )
    if result.errors:
        for url, msg in result.errors:
            console.print(f"  [red][!!][/red] {url[:60]}  -  {msg}")


def _save_postings_to_json(postings: list, output_path: Path) -> None:
    """Serialise scraped postings to a JSON file.

    Args:
        postings: List of :class:`~scraper.base_scraper.JobPosting` objects.
        output_path: Destination file path.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data = [p.to_dict() for p in postings]
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    logging.getLogger(__name__).info(
        "Saved %d postings to %s.", len(postings), output_path
    )


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# view-job command (Test 2.3)
# ---------------------------------------------------------------------------


@cli.command("view-job")
@click.option("--job-id", required=True, type=int, help="Database ID of the job to display.")
def view_job(job_id: int) -> None:
    """Display full details for a single scraped job.

    \b
    Example:
        python main.py view-job --job-id 2
    """
    from database.database import get_db
    from database.models import Job
    from rich.panel import Panel
    from rich.rule import Rule

    with get_db() as db:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            console.print(f"[red][!!][/red] No job found with id={job_id}.")
            sys.exit(1)

        console.print()
        console.print(Panel(
            f"[bold]{job.job_title}[/bold]  at  [cyan]{job.company_name}[/cyan]",
            subtitle=f"ID: {job.id}  |  Status: {job.status}",
            expand=False,
        ))

        meta_table = Table(show_header=False, box=None, padding=(0, 1))
        meta_table.add_column("Field", style="bold cyan", width=18)
        meta_table.add_column("Value")
        meta_table.add_row("Location",    job.location or "-")
        meta_table.add_row("Source",      job.source)
        meta_table.add_row("Posted",      job.date_posted.isoformat() if job.date_posted else "-")
        meta_table.add_row("Scraped",     job.date_scraped.strftime("%Y-%m-%d %H:%M") if job.date_scraped else "-")
        meta_table.add_row("Salary",      job.salary_range or "-")
        meta_table.add_row("Apply URL",   (job.application_url or "-")[:80])
        console.print(meta_table)

        if job.required_skills:
            console.print(Rule("[cyan]Required Skills[/cyan]"))
            console.print("  " + _safe(", ".join(job.required_skills)))

        if job.preferred_skills:
            console.print(Rule("[cyan]Preferred Skills[/cyan]"))
            console.print("  " + _safe(", ".join(job.preferred_skills)))

        if job.job_description:
            console.print(Rule("[cyan]Job Description[/cyan]"))
            desc = _safe(job.job_description)
            preview = desc[:1000] + ("\n\n[dim]... (truncated)[/dim]" if len(desc) > 1000 else "")
            console.print(f"  {preview}")

        console.print()


# Phase 3: Analysis commands
# ---------------------------------------------------------------------------


@cli.command("analyze-job")
@click.option(
    "--job-id",
    required=True,
    type=int,
    help="Database ID of the job to analyse.",
)
@click.option(
    "--save/--no-save",
    "save_results",
    default=True,
    show_default=True,
    help="Persist extracted skills back to the jobs table.",
)
def analyze_job(job_id: int, save_results: bool) -> None:
    """Analyse a scraped job and extract structured keywords + requirements.

    Runs keyword extraction (spaCy + taxonomy) and requirement parsing
    (regex) on the stored job description.  Results are optionally saved
    back to ``jobs.required_skills`` and ``jobs.preferred_skills`` and the
    job status is updated to ``analyzed``.

    \b
    Examples:
        python main.py analyze-job --job-id 42
        python main.py analyze-job --job-id 42 --no-save
    """
    from analyzer import KeywordExtractor, RequirementParser
    from database.database import get_db
    from database.models import Job

    try:
        with get_db() as db:
            job = db.query(Job).filter(Job.id == job_id).first()
            if not job:
                console.print(f"[red][!!][/red] No job found with id={job_id}.")
                sys.exit(1)

            console.print(
                f"\n[bold]Analysing:[/bold] [cyan]{_safe(job.job_title)}[/cyan] @ {_safe(job.company_name)}"
            )

            extractor = KeywordExtractor()
            parser = RequirementParser()

            keywords_by_cat = extractor.extract_by_category(job.job_description)
            requirements = parser.parse(job.job_description)

            # ---- Display keywords table ----
            kw_table = Table(
                title="Extracted Keywords", show_header=True, header_style="bold magenta"
            )
            kw_table.add_column("Category", style="cyan", min_width=22)
            kw_table.add_column("Skills", max_width=70)

            for cat, skills in sorted(keywords_by_cat.items()):
                if skills:
                    kw_table.add_row(_safe(cat), _safe(", ".join(skills[:15])))

            console.print(kw_table)

            # ---- Display requirements summary ----
            req_table = Table(
                title="Parsed Requirements", show_header=True, header_style="bold magenta"
            )
            req_table.add_column("Type", style="cyan", min_width=18)
            req_table.add_column("Detail", max_width=74)

            if requirements.experience:
                for exp in requirements.experience[:5]:
                    skill_label = _safe(exp.skill or "(general)")
                    req_table.add_row(
                        "Experience",
                        f"{exp.min_years}+ yrs - {skill_label}",
                    )

            if requirements.education:
                for edu in requirements.education:
                    label = _safe(edu.level)
                    if edu.field_of_study:
                        label += f" in {_safe(edu.field_of_study)}"
                    req_table.add_row(
                        "Education",
                        f"{label} ({'required' if edu.is_required else 'preferred'})",
                    )

            if requirements.certifications:
                for cert in requirements.certifications[:5]:
                    req_table.add_row(
                        "Certification",
                        f"{_safe(cert.name)} ({'required' if cert.is_required else 'preferred'})",
                    )

            console.print(req_table)

            # ---- Persist results ----
            if save_results:
                all_tech_skills = []
                for cat, skills in keywords_by_cat.items():
                    if cat not in {"soft_skills", "education_keywords"}:
                        all_tech_skills.extend(skills)
                soft_skills = keywords_by_cat.get("soft_skills", [])

                job.required_skills = list(dict.fromkeys(all_tech_skills)) or None
                job.preferred_skills = soft_skills or None
                job.status = "analyzed"
                db.flush()
                console.print(
                    f"\n[green][OK][/green] Saved {len(all_tech_skills)} technical skills, "
                    f"{len(soft_skills)} soft skills - status set to 'analyzed'."
                )
            else:
                console.print("\n[dim][--no-save] Results not persisted.[/dim]")

    except SystemExit:
        raise
    except Exception as exc:
        console.print(f"[red][!!][/red] Analysis failed: {exc}")
        logging.getLogger(__name__).exception("analyze-job error for job_id=%d", job_id)
        sys.exit(1)


@cli.command("list-resumes")
def list_resumes() -> None:
    """List all master resumes stored in the database."""
    from database.database import get_db
    from database.models import MasterResume, TailoredResume

    with get_db() as db:
        resumes = db.query(MasterResume).order_by(MasterResume.id).all()
        if not resumes:
            console.print("[yellow]No master resumes found. Use upload-resume to add one.[/yellow]")
            return

        table = Table(title=f"Master Resumes ({len(resumes)})", show_header=True, header_style="bold magenta")
        table.add_column("ID", width=5)
        table.add_column("Name", min_width=25)
        table.add_column("Active", width=8)
        table.add_column("Tailored", width=9)
        table.add_column("Created", width=12)

        for r in resumes:
            tailored_count = db.query(TailoredResume).filter(TailoredResume.master_resume_id == r.id).count()
            created = r.created_at.strftime("%Y-%m-%d") if r.created_at else "-"
            active_str = "[green]Yes[/green]" if r.is_active else "No"
            table.add_row(str(r.id), r.name, active_str, str(tailored_count), created)

        console.print(table)


@cli.command("upload-resume")
@click.argument("json_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--name", default=None, help="Display name for this resume (defaults to filename).")
@click.option("--set-active", is_flag=True, default=False, help="Mark this resume as the active default.")
def upload_resume(json_file: str, name: str | None, set_active: bool) -> None:
    """Upload a master resume from a JSON file.

    JSON_FILE must be a path to a JSON file containing the resume content
    (personal_info, professional_summary, work_experience, skills, education,
    projects sections).

    \b
    Example:
        python main.py upload-resume my_resume.json --name "Master Resume" --set-active
    """
    import datetime

    from database.database import get_db
    from database.models import MasterResume

    json_path = Path(json_file)
    resume_name = name or json_path.stem.replace("_", " ").title()

    try:
        with json_path.open("r", encoding="utf-8") as fh:
            content = json.load(fh)
    except json.JSONDecodeError as exc:
        console.print(f"[red][!!][/red] Invalid JSON: {exc}")
        sys.exit(1)

    required_sections = {"personal_info", "work_experience", "skills"}
    missing = required_sections - set(content.keys())
    if missing:
        console.print(f"[yellow][!!][/yellow] JSON is missing sections: {', '.join(sorted(missing))}")
        console.print("Proceeding anyway - these sections will be empty in the tailored resume.")

    with get_db() as db:
        if set_active:
            db.query(MasterResume).update({"is_active": False})

        resume = MasterResume(
            name=resume_name,
            content=content,
            is_active=set_active,
            created_at=datetime.datetime.now(datetime.timezone.utc),
        )
        db.add(resume)
        db.flush()
        resume_id = resume.id
        db.commit()

    active_note = " [green](set as active)[/green]" if set_active else ""
    console.print(f"[green][OK][/green] Resume '{resume_name}' uploaded (ID: {resume_id}){active_note}")


@cli.command("match-score")
@click.option("--job-id", required=True, type=int, help="Database ID of the job.")
@click.option(
    "--resume-id",
    required=True,
    type=int,
    help="ID of a MasterResume or TailoredResume to compare.",
)
@click.option(
    "--type",
    "resume_type_hint",
    type=click.Choice(["auto", "master", "tailored"], case_sensitive=False),
    default="auto",
    show_default=True,
    help="Explicitly choose which table to look up the resume ID in. "
         "'auto' tries MasterResume first, then TailoredResume.",
)
@click.option(
    "--output",
    default=None,
    type=click.Path(dir_okay=False, writable=True),
    help="Export full score breakdown to a JSON file.",
)
def match_score(job_id: int, resume_id: int, resume_type_hint: str, output: str | None) -> None:
    """Calculate the match score between a job and a master or tailored resume.

    Accepts both master resume IDs and tailored resume IDs. Use --type to
    disambiguate when both tables share the same ID.
    checks MasterResume first, then TailoredResume, so you can compare
    before-and-after scores after running generate-resume.

    \b
    Examples:
        python main.py match-score --job-id 1 --resume-id 1
        python main.py match-score --job-id 1 --resume-id 2
        python main.py match-score --job-id 1 --resume-id 1 --output score.json
    """
    from analyzer import ScoringEngine
    from database.database import get_db
    from database.models import Job, MasterResume, TailoredResume
    from rich.panel import Panel

    try:
        with get_db() as db:
            job = db.query(Job).filter(Job.id == job_id).first()
            if not job:
                console.print(f"[red][!!][/red] No job found with id={job_id}.")
                sys.exit(1)

            # Resolve resume according to --type hint
            master = None
            tailored = None
            hint = resume_type_hint.lower()
            if hint in ("auto", "master"):
                master = db.query(MasterResume).filter(MasterResume.id == resume_id).first()
            if not master and hint in ("auto", "tailored"):
                tailored = (
                    db.query(TailoredResume)
                    .filter(TailoredResume.id == resume_id)
                    .first()
                )
            if not master and not tailored:
                console.print(
                    f"[red][!!][/red] No {'master' if hint == 'master' else 'tailored' if hint == 'tailored' else 'master or tailored'} "
                    f"resume found with id={resume_id}."
                )
                sys.exit(1)

            resume_type = "Master" if master else "Tailored"
            resume_label = master.name if master else f"Tailored #{tailored.id} (job #{tailored.job_id})"

            console.print(
                f"\n[bold]Job   :[/bold] [cyan]{job.job_title}[/cyan] @ {job.company_name}"
                f"\n[bold]Resume:[/bold] {resume_type} - {resume_label}\n"
            )

            # ---- Full weighted score via ScoringEngine (master resumes) ----
            if master:
                engine = ScoringEngine()
                result = engine.score(job, master)
                total = result.total_score
                breakdown = result.breakdown
                matched = result.matched_skills
                missing = result.missing_skills
                extra = result.extra_skills
                skill_details: dict = {}
            else:
                # Score tailored resume using score_from_text to avoid ORM proxy issues.
                engine = ScoringEngine()
                tc = tailored.tailored_content or {}
                resume_skills_list = engine._extract_resume_skills(tc)
                result = engine.score_from_text(
                    job_description=job.job_description or "",
                    resume_skills=resume_skills_list,
                    job_required_skills=job.required_skills,
                    job_preferred_skills=job.preferred_skills,
                    job_id=job.id,
                    resume_id=tailored.id,
                )
                total = result.total_score
                breakdown = result.breakdown
                matched = result.matched_skills
                missing = result.missing_skills
                extra = result.extra_skills
                skill_details: dict = {}

            # ---- Score panel ----
            score_colour = (
                "green" if total >= 70
                else "yellow" if total >= 40
                else "red"
            )
            console.print(
                Panel(
                    f"[bold {score_colour}]{total:.1f} / 100[/bold {score_colour}]",
                    title="Overall Match Score",
                    border_style=score_colour,
                    expand=False,
                )
            )

            # ---- Breakdown table (weighted components if available) ----
            if breakdown:
                breakdown_table = Table(
                    title="Score Breakdown", show_header=True, header_style="bold magenta"
                )
                breakdown_table.add_column("Component", style="cyan", min_width=22)
                breakdown_table.add_column("Score", justify="right", width=8)
                breakdown_table.add_column("Weight", justify="right", width=8)

                component_meta = {
                    "required_skills": ("Required Skills", "40%"),
                    "preferred_skills": ("Preferred Skills", "30%"),
                    "experience": ("Experience", "20%"),
                    "education": ("Education", "10%"),
                    "bonus": ("Extra Skills Bonus", "+cap"),
                }
                for key, (label, weight) in component_meta.items():
                    score_val = breakdown.get(key, 0.0)
                    c = "green" if score_val >= 70 else "yellow" if score_val >= 40 else "red"
                    breakdown_table.add_row(label, f"[{c}]{score_val:.1f}[/{c}]", weight)
                console.print(breakdown_table)

            elif skill_details:
                cat_table = Table(
                    title="Category Scores", show_header=True, header_style="bold magenta"
                )
                cat_table.add_column("Category", style="cyan")
                cat_table.add_column("Score", justify="right", width=8)
                for cat, val in skill_details.items():
                    c = "green" if val >= 70 else "yellow" if val >= 40 else "red"
                    cat_table.add_row(cat.replace("_", " ").title(), f"[{c}]{val:.1f}[/{c}]")
                console.print(cat_table)

            # ---- Skill lists ----
            if matched:
                console.print(
                    f"\n[green]Matched ({len(matched)}):[/green] "
                    + ", ".join(matched[:15])
                )
            if missing:
                console.print(
                    f"\n[yellow]Missing ({len(missing)}):[/yellow] "
                    + ", ".join(missing[:15])
                )
            else:
                console.print("\n[green]No missing required skills - great match![/green]")
            if extra:
                console.print(
                    f"\n[dim]Extra skills on resume ({len(extra)}): "
                    + ", ".join(extra[:10]) + "[/dim]"
                )

            # ---- Export ----
            if output:
                output_path = Path(output)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                export_data = (
                    result.to_dict() if result else {
                        "total_score": total,
                        "matched_skills": matched,
                        "missing_skills": missing,
                        "extra_skills": extra,
                        "category_scores": skill_details,
                        "resume_type": resume_type,
                        "resume_id": resume_id,
                        "job_id": job_id,
                    }
                )
                with output_path.open("w", encoding="utf-8") as fh:
                    json.dump(export_data, fh, indent=2, ensure_ascii=False)
                console.print(f"\n[green][OK][/green] Score breakdown exported to {output}")

    except SystemExit:
        raise
    except Exception as exc:
        console.print(f"[red][!!][/red] Scoring failed: {exc}")
        logging.getLogger(__name__).exception(
            "match-score error for job_id=%d resume_id=%d", job_id, resume_id
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Phase 4: Resume generation commands
# ---------------------------------------------------------------------------


@cli.command("generate-resume")
@click.option("--job-id", required=True, type=int, help="Job ID to tailor the resume for.")
@click.option(
    "--resume-id",
    default=None,
    type=int,
    help="Master resume ID. Uses the active resume if omitted.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview match score and plan without making API calls.",
)
@click.option(
    "--output",
    default=None,
    type=click.Path(dir_okay=False, writable=True),
    help="Export the tailored resume JSON to a file.",
)
def generate_resume(
    job_id: int,
    resume_id: int | None,
    dry_run: bool,
    output: str | None,
) -> None:
    """Generate a tailored resume for a specific job using NVIDIA NIM (Nemotron).

    \b
    Examples:
        python main.py generate-resume --job-id 1
        python main.py generate-resume --job-id 1 --resume-id 2 --dry-run
        python main.py generate-resume --job-id 1 --output tailored.json
    """
    from datetime import datetime, timezone

    from analyzer.scoring import ScoringEngine
    from database.database import get_db
    from database.models import Job, MasterResume, TailoredResume

    try:
        with get_db() as db:
            job = db.query(Job).filter(Job.id == job_id).first()
            if not job:
                console.print(f"[red][!!][/red] Job {job_id} not found.")
                sys.exit(1)

            if resume_id:
                resume = db.query(MasterResume).filter(MasterResume.id == resume_id).first()
            else:
                resume = (
                    db.query(MasterResume).filter(MasterResume.is_active.is_(True)).first()
                )

            if not resume:
                console.print("[red][!!][/red] No resume found. Add one with the import-resume command.")
                sys.exit(1)

            console.print(
                f"\n[bold]Job    :[/bold] [cyan]{job.job_title}[/cyan] @ {job.company_name}"
            )
            console.print(f"[bold]Resume :[/bold] {resume.name}")

            # ---- Pre-flight match score ----
            engine = ScoringEngine()
            score_result = engine.score(job, resume)
            score_colour = (
                "green" if score_result.total_score >= 70
                else "yellow" if score_result.total_score >= 40
                else "red"
            )
            console.print(
                f"[bold]Pre-tailor score:[/bold] "
                f"[{score_colour}]{score_result.total_score:.1f}/100[/{score_colour}]"
            )
            if score_result.missing_skills:
                console.print(
                    f"[dim]Missing: {', '.join(score_result.missing_skills[:8])}[/dim]"
                )

            if dry_run:
                console.print("\n[yellow][dry-run] No API calls made - exiting.[/yellow]")
                return

            # ---- Tailor the resume ----
            from resume_engine.modifier import ResumeModifier
            from resume_engine.rate_limiter import QuotaExceededError

            step_labels = {
                "summary": "Rewriting professional summary",
                "experience": "Tailoring work experience",
                "skills": "Reordering skills",
                "projects": "Selecting projects",
                "done": "Finalising",
            }

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                TimeElapsedColumn(),
                console=console,
                transient=True,
            ) as progress:
                task = progress.add_task("Starting...", total=5)

                def _on_progress(step: str, current: int, total: int) -> None:
                    label = step_labels.get(step, step)
                    progress.update(task, completed=current, description=label)

                modifier = ResumeModifier(on_progress=_on_progress)

                try:
                    result = modifier.modify_resume(
                        resume, job,
                        style_fingerprint=getattr(resume, "style_fingerprint", None),
                    )
                except QuotaExceededError as exc:
                    console.print(f"[red][!!][/red] {exc}")
                    sys.exit(1)

            # ---- Display metrics ----
            m = result.metrics
            console.print(
                f"\n[green][OK][/green] Tailoring complete in {result.api_calls_used} API call(s)."
            )
            console.print(
                f"[bold]Keyword coverage:[/bold] "
                f"{m['keyword_coverage_before']:.1f}% -> "
                f"[green]{m['keyword_coverage_after']:.1f}%[/green] "
                f"(+{m['keyword_coverage_improvement']:.1f}%)"
            )

            mods_made = len([e for e in result.modification_log if e.original != e.modified])
            console.print(f"[bold]Modifications:[/bold] {mods_made} section(s) changed")

            # ---- Validation summary ----
            vstats = result.validation_report.get("summary_stats", {})
            if vstats.get("invalid_bullets", 0):
                console.print(
                    f"[yellow][!!][/yellow] {vstats['invalid_bullets']} bullet(s) had validation warnings "
                    "(originals preserved for metric-loss issues)."
                )

            # ---- Save to database ----
            existing = (
                db.query(TailoredResume)
                .filter(
                    TailoredResume.job_id == job.id,
                    TailoredResume.master_resume_id == resume.id,
                )
                .first()
            )

            if existing:
                existing.tailored_content = result.content
                existing.match_score = score_result.total_score
                existing.generated_at = datetime.now(timezone.utc)
                tailored_id = existing.id
                console.print(f"[green][OK][/green] Updated tailored resume (ID: {tailored_id})")
            else:
                tailored_row = TailoredResume(
                    job_id=job.id,
                    master_resume_id=resume.id,
                    tailored_content=result.content,
                    match_score=score_result.total_score,
                )
                db.add(tailored_row)
                db.flush()
                tailored_id = tailored_row.id
                console.print(f"[green][OK][/green] Saved tailored resume (ID: {tailored_id})")

            # ---- Optional JSON export ----
            if output:
                output_path = Path(output)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                with output_path.open("w", encoding="utf-8") as fh:
                    json.dump(result.to_dict(), fh, indent=2, ensure_ascii=False)
                console.print(f"[green][OK][/green] Exported to {output}")

    except SystemExit:
        raise
    except Exception as exc:
        console.print(f"[red][!!][/red] Resume generation failed: {exc}")
        logging.getLogger(__name__).exception("generate-resume error for job_id=%d", job_id)
        sys.exit(1)


@cli.command("api-usage")
def api_usage() -> None:
    """Show NVIDIA NIM API usage statistics for today.

    \b
    Example:
        python main.py api-usage
    """
    from resume_engine.rate_limiter import RateLimiter

    nim_limiter = RateLimiter(rpm=60, rpd=5_000, model_key="nvidia")

    s = nim_limiter.stats()

    usage_table = Table(
        title="NVIDIA NIM API Usage",
        show_header=True,
        header_style="bold magenta",
    )
    usage_table.add_column("Metric", style="cyan", min_width=30)
    usage_table.add_column("llama-3.3-nemotron-super-49b-v1.5", justify="right", min_width=36)

    usage_table.add_row("Calls today", str(s["calls_today"]))
    usage_table.add_row("Calls remaining (daily)", str(s["calls_remaining_today"]))
    usage_table.add_row("Daily limit (RPD)", str(s["rpd_limit"]))
    usage_table.add_row("Per-minute limit (RPM)", str(s["rpm_limit"]))
    usage_table.add_row("Estimated tokens today", f"{s['tokens_estimated_today']:,}")
    usage_table.add_row("Quota date", s["quota_date"])

    def _pct_colour(remaining: int, limit: int) -> str:
        pct = remaining / limit * 100 if limit else 0
        colour = "green" if pct > 20 else "yellow" if pct > 5 else "red"
        return f"[{colour}]{pct:.0f}%[/{colour}]"

    usage_table.add_row(
        "Quota remaining %",
        _pct_colour(s["calls_remaining_today"], s["rpd_limit"]),
    )

    console.print()
    console.print(usage_table)
    console.print(
        "\n[dim]Usage data persisted in [bold]data/nvidia_usage.json[/bold][/dim]"
    )


# ---------------------------------------------------------------------------
# Phase 5: PDF export commands
# ---------------------------------------------------------------------------


@cli.command("export-pdf")
@click.option(
    "--resume-id",
    required=True,
    type=int,
    help="ID of the TailoredResume to export.",
)
@click.option(
    "--template",
    "template_name",
    type=click.Choice(["ats", "classic"], case_sensitive=False),
    default="ats",
    show_default=True,
    help="PDF template to use.",
)
@click.option(
    "--output",
    "filename",
    default=None,
    help="Custom output filename (e.g. my_resume.pdf).  "
         "Saved to data/output/ unless a full path is given.",
)
def export_pdf(resume_id: int, template_name: str, filename: str | None) -> None:
    """Export a tailored resume as a PDF file.

    Loads the tailored resume from the database, renders it with the chosen
    template, saves the file to ``data/output/``, and records the path in the
    database row.

    \b
    Examples:
        python main.py export-pdf --resume-id 1
        python main.py export-pdf --resume-id 1 --template classic
        python main.py export-pdf --resume-id 1 --output my_resume.pdf
    """
    from database.database import get_db
    from database.models import TailoredResume
    from pdf_generator.generator import PDFGenerator

    try:
        with get_db() as db:
            row = db.query(TailoredResume).filter(TailoredResume.id == resume_id).first()
            if not row:
                console.print(f"[red][!!][/red] No tailored resume found with id={resume_id}.")
                sys.exit(1)

            resume_data: dict = row.tailored_content or {}
            if not resume_data:
                console.print("[red][!!][/red] Tailored resume has no content.")
                sys.exit(1)

        console.print(
            f"[cyan]Exporting tailored resume #{resume_id} "
            f"using [bold]{template_name}[/bold] template...[/cyan]"
        )

        generator = PDFGenerator(output_dir="data/output")

        with get_db() as db:
            row = db.query(TailoredResume).filter(TailoredResume.id == resume_id).first()
            resume_data = row.tailored_content or {}

            pdf_path = generator.generate(resume_data, template_name, filename)

            row.pdf_path = pdf_path
            db.commit()

        file_size_kb = Path(pdf_path).stat().st_size / 1024
        console.print(f"\n[green][OK][/green] PDF saved: [bold]{pdf_path}[/bold]")
        console.print(f"     Template : {template_name}")
        console.print(f"     Size     : {file_size_kb:.1f} KB")
        console.print(f"     Resume ID: {resume_id}")

    except (ValueError, RuntimeError) as exc:
        console.print(f"[red][!!][/red] Export failed: {exc}")
        sys.exit(1)
    except SystemExit:
        raise
    except Exception as exc:
        console.print(f"[red][!!][/red] Unexpected error: {exc}")
        logging.getLogger(__name__).exception("export-pdf error for resume_id=%d", resume_id)
        sys.exit(1)


@cli.command("preview-resume")
@click.option(
    "--resume-id",
    required=True,
    type=int,
    help="ID of the TailoredResume to preview.",
)
@click.option(
    "--master",
    "use_master",
    is_flag=True,
    default=False,
    help="Preview the MasterResume instead of a TailoredResume.",
)
def preview_resume(resume_id: int, use_master: bool) -> None:
    """Display resume content in the terminal before PDF export.

    Shows a structured text preview of every section so you can check
    the content before committing to a PDF render.

    \b
    Examples:
        python main.py preview-resume --resume-id 1
        python main.py preview-resume --resume-id 1 --master
    """
    from database.database import get_db
    from database.models import MasterResume, TailoredResume
    from rich.panel import Panel
    from rich.rule import Rule

    try:
        with get_db() as db:
            if use_master:
                row = db.query(MasterResume).filter(MasterResume.id == resume_id).first()
                if not row:
                    console.print(f"[red][!!][/red] No master resume found with id={resume_id}.")
                    sys.exit(1)
                data = row.content or {}
                label = f"Master Resume #{resume_id} - {row.name}"
            else:
                row = db.query(TailoredResume).filter(TailoredResume.id == resume_id).first()
                if not row:
                    console.print(f"[red][!!][/red] No tailored resume found with id={resume_id}.")
                    sys.exit(1)
                data = row.tailored_content or {}
                label = f"Tailored Resume #{resume_id}"

        console.print()
        console.print(Panel(f"[bold]{label}[/bold]", expand=False))

        # Personal info
        pi = data.get("personal_info", {})
        if pi:
            console.print(Rule("[cyan]CONTACT[/cyan]"))
            console.print(f"  Name   : [bold]{pi.get('name', '-')}[/bold]")
            console.print(f"  Email  : {pi.get('email', '-')}")
            console.print(f"  Phone  : {pi.get('phone', '-')}")
            console.print(f"  Location: {pi.get('location', '-')}")

        # Summary
        summary = data.get("professional_summary", "")
        if summary:
            console.print(Rule("[cyan]PROFESSIONAL SUMMARY[/cyan]"))
            console.print(f"  {summary[:300]}{'...' if len(summary) > 300 else ''}")

        # Work experience
        exp = data.get("work_experience", [])
        if exp:
            console.print(Rule("[cyan]WORK EXPERIENCE[/cyan]"))
            for job in exp:
                console.print(
                    f"  [bold]{job.get('title', '')}[/bold] at {job.get('company', '')} "
                    f"[dim]({job.get('dates', '')})[/dim]"
                )
                for b in job.get("bullets", []):
                    console.print(f"    - {b}")

        # Education
        edu = data.get("education", [])
        if edu:
            console.print(Rule("[cyan]EDUCATION[/cyan]"))
            for e in edu:
                if isinstance(e, str):
                    console.print(f"  {e}")
                else:
                    deg    = e.get("degree", "")
                    school = e.get("school", e.get("institution", ""))
                    year   = e.get("year", "")
                    console.print(f"  [bold]{deg}[/bold] - {school} ({year})")

        # Skills
        skills = data.get("skills", [])
        if skills:
            console.print(Rule("[cyan]SKILLS[/cyan]"))
            if isinstance(skills, list):
                console.print("  " + ", ".join(str(s) for s in skills))
            else:
                for cat, items in skills.items():
                    console.print(f"  [bold]{cat}[/bold]: {', '.join(str(i) for i in items)}")

        # Projects
        projects = data.get("projects", [])
        if projects:
            console.print(Rule("[cyan]PROJECTS[/cyan]"))
            for p in projects:
                console.print(f"  [bold]{p.get('name', '')}[/bold]: {p.get('description', '')[:80]}")

        console.print()

    except SystemExit:
        raise
    except Exception as exc:
        console.print(f"[red][!!][/red] Preview failed: {exc}")
        logging.getLogger(__name__).exception("preview-resume error for resume_id=%d", resume_id)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Phase 6: Scheduler commands
# ---------------------------------------------------------------------------

_TASK_MAP = {
    "scrape":    ("scrape_jobs_task",        "scheduler.tasks"),
    "analyze":   ("analyze_new_jobs_task",   "scheduler.tasks"),
    "generate":  ("generate_resumes_task",   "scheduler.tasks"),
    "cleanup":   ("cleanup_old_jobs_task",   "scheduler.tasks"),
    "report":    ("daily_report_task",       "scheduler.tasks"),
}


@cli.command("start-scheduler")
@click.option(
    "--config",
    "config_path",
    default="config.yaml",
    show_default=True,
    help="Path to config.yaml.",
)
@click.option(
    "--daemon",
    is_flag=True,
    default=False,
    help="Keep running in the foreground until Ctrl+C (daemon mode).",
)
@click.option(
    "--test-mode",
    is_flag=True,
    default=False,
    help="Override config: use short intervals (2-5 min) for quick testing.",
)
def start_scheduler(config_path: str, daemon: bool, test_mode: bool) -> None:
    """Start the automated job pipeline scheduler.

    In normal mode the scheduler prints its job table and exits immediately
    (useful to verify configuration).  Pass --daemon to keep it running in
    the foreground so you can watch task executions in the logs.

    \b
    Examples:
        python main.py start-scheduler
        python main.py start-scheduler --daemon
        python main.py start-scheduler --test-mode --daemon
    """
    import signal
    import time

    from scheduler.scheduler import SchedulerManager

    manager = SchedulerManager(config_path=config_path)

    # Let --test-mode CLI flag override whatever is in config.yaml
    if test_mode:
        manager._config["test_mode"] = True

    try:
        manager.start()
    except Exception as exc:
        console.print(f"[red][!!][/red] Failed to start scheduler: {exc}")
        logging.getLogger(__name__).exception("start-scheduler error")
        sys.exit(1)

    # ---- Display job table ----
    console.print("\n[green][OK][/green] Scheduler started.\n")
    _print_job_table(manager.get_job_info())

    if not daemon:
        console.print(
            "\n[dim](Scheduler registered above. Pass --daemon to keep it running.)[/dim]"
        )
        manager.stop(wait=False)
        return

    # ---- Daemon mode: block until Ctrl+C ----
    console.print(
        "\n[cyan]Running in daemon mode. Press Ctrl+C to stop.[/cyan]\n"
    )

    def _shutdown(sig: int, frame: object) -> None:
        console.print("\n[yellow]Stopping scheduler...[/yellow]")
        manager.stop(wait=True)
        console.print("[green][OK][/green] Scheduler stopped.")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        while True:
            time.sleep(30)
            # Periodically log that we're still alive
            logging.getLogger(__name__).debug("[daemon] Heartbeat – scheduler is running.")
    except KeyboardInterrupt:
        _shutdown(0, None)


@cli.command("scheduler-status")
@click.option(
    "--config",
    "config_path",
    default="config.yaml",
    show_default=True,
    help="Path to config.yaml.",
)
def scheduler_status(config_path: str) -> None:
    """Show all scheduled jobs and their next run times.

    Creates a temporary scheduler instance to resolve trigger times, then
    stops it immediately without executing any tasks.

    \b
    Example:
        python main.py scheduler-status
    """
    from scheduler.scheduler import SchedulerManager

    try:
        manager = SchedulerManager(config_path=config_path)
        manager.start()
        jobs = manager.get_job_info()
        manager.stop(wait=False)
    except Exception as exc:
        console.print(f"[red][!!][/red] Scheduler error: {exc}")
        logging.getLogger(__name__).exception("scheduler-status error")
        sys.exit(1)

    if not jobs:
        console.print("[yellow]No scheduled jobs found.[/yellow]")
        return

    _print_job_table(jobs)


@cli.command("run-task")
@click.argument(
    "task",
    type=click.Choice(list(_TASK_MAP.keys()), case_sensitive=False),
)
@click.option(
    "--threshold",
    default=None,
    type=float,
    help="Match score threshold for 'generate' task (overrides config).",
)
@click.option(
    "--days",
    default=None,
    type=int,
    help="Days for 'cleanup' task (overrides config default of 30).",
)
@click.option(
    "--config",
    "config_path",
    default="config.yaml",
    show_default=True,
    help="Path to config.yaml (used by 'scrape' task for search configs).",
)
def run_task(
    task: str,
    threshold: float | None,
    days: int | None,
    config_path: str,
) -> None:
    """Run a single pipeline task immediately (without the scheduler).

    TASK choices:
      scrape    – Scrape LinkedIn for new jobs
                  (manual — also available via dashboard Pipeline Controls)
      analyze   – Analyse unanalysed jobs
                  (manual — also available via dashboard Pipeline Controls)
      generate  – Auto-generate resumes for high-match jobs
                  (manual — also available via dashboard Pipeline Controls)
      cleanup   – Archive old jobs
                  (automatic — runs on schedule, Sundays at midnight)
      report    – Send the daily summary report
                  (automatic — runs on schedule, daily at 20:00)

    \b
    Examples:
        python main.py run-task analyze
        python main.py run-task generate --threshold 50
        python main.py run-task cleanup --days 7
        python main.py run-task scrape
    """
    import importlib

    from rich.panel import Panel

    func_name, module_name = _TASK_MAP[task.lower()]
    mod = importlib.import_module(module_name)
    func = getattr(mod, func_name)

    console.print(f"\n[bold cyan]Running task:[/bold cyan] {func_name}\n")

    try:
        if task == "scrape":
            import yaml  # type: ignore[import-untyped]
            try:
                with open(config_path, "r", encoding="utf-8") as fh:
                    raw = yaml.safe_load(fh) or {}
                search_configs = raw.get("scheduler", {}).get("search_configs", [
                    {"keywords": "python developer", "location": "San Francisco", "max_results": 5}
                ])
            except Exception:
                search_configs = [
                    {"keywords": "python developer", "location": "San Francisco", "max_results": 5}
                ]
            result = func(search_configs)

        elif task == "generate":
            cfg_threshold = 60.0
            try:
                import yaml  # type: ignore[import-untyped]
                with open(config_path, "r", encoding="utf-8") as fh:
                    raw = yaml.safe_load(fh) or {}
                cfg_threshold = float(
                    raw.get("scheduler", {}).get("auto_generate_threshold", 60.0)
                )
            except Exception:
                pass
            effective_threshold = threshold if threshold is not None else cfg_threshold
            result = func(effective_threshold)

        elif task == "cleanup":
            effective_days = days if days is not None else 30
            result = func(effective_days)

        else:
            result = func()

    except Exception as exc:
        console.print(f"[red][!!][/red] Task raised an exception: {exc}")
        logging.getLogger(__name__).exception("run-task error for %r", task)
        sys.exit(1)

    # ---- Display results ----
    colour = "green" if result.success else "red"
    status_str = "[OK]" if result.success else "[FAIL]"

    console.print(
        Panel(
            f"[bold {colour}]{status_str}[/bold {colour}]  {result.task_name}",
            subtitle=f"Duration: {result.duration_seconds:.1f}s",
            border_style=colour,
            expand=False,
        )
    )

    if result.data:
        data_table = Table(show_header=False, box=None, padding=(0, 1))
        data_table.add_column("Key", style="cyan")
        data_table.add_column("Value", justify="right")
        for k, v in result.data.items():
            data_table.add_row(k.replace("_", " ").title(), str(v))
        console.print(data_table)

    if result.errors:
        console.print(f"\n[yellow]Warnings / errors ({len(result.errors)}):[/yellow]")
        for err in result.errors[:10]:
            console.print(f"  [dim]{err}[/dim]")


# ---------------------------------------------------------------------------
# Helper: render scheduler job table
# ---------------------------------------------------------------------------


def _print_job_table(jobs: list) -> None:
    """Print a Rich table of scheduled jobs."""
    tbl = Table(title="Scheduled Jobs", show_header=True, header_style="bold magenta")
    tbl.add_column("ID", style="cyan", width=20)
    tbl.add_column("Name", min_width=28)
    tbl.add_column("Next Run", width=26)
    tbl.add_column("Trigger", min_width=30)

    for j in jobs:
        next_run = j["next_run"]
        next_str = next_run.strftime("%Y-%m-%d %H:%M:%S %Z") if next_run else "N/A"
        tbl.add_row(j["id"], j["name"], next_str, j["trigger"])

    console.print(tbl)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cli()
