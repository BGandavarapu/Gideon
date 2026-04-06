"""Flask web application for the Gideon dashboard.

Provides a Linear-inspired UI for browsing jobs, viewing resumes, and
triggering resume generation / PDF export.

Run:
    python web/app.py            # dev server (default port 5001; set PORT=5000 if free)
    python -m flask --app web.app run --debug
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time as _time
from datetime import datetime, timezone
from pathlib import Path

# Record startup time and PID for the /api/health endpoint.
_START_TIME: float = _time.time()
_START_PID: int = os.getpid()

# ---------------------------------------------------------------------------
# Make sure the project root is on sys.path when running web/app.py directly
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Load .env before any module that reads env vars (NVIDIA_API_KEY, etc.)
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_ROOT / ".env")
except ImportError:
    pass

from flask import Flask, jsonify, render_template, request, send_file
from flask_cors import CORS

from sqlalchemy.orm import joinedload

from database.database import get_db
from database.models import Application, Job, MasterResume, TailoredResume
from web.settings_manager import DOMAINS, SettingsManager

# ---------------------------------------------------------------------------
# App configuration
# ---------------------------------------------------------------------------

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-change-in-prod")
CORS(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Singleton — all routes share the same instance.
settings_manager = SettingsManager()

# One-time migration: strip the 3 original SE configs that were seeded at
# project creation so industry configs (driven by active domain) take over.
settings_manager.clear_legacy_search_configs()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _safe_json(text: str | list | None):
    """Return parsed JSON or the value as-is; never raises."""
    if text is None:
        return []
    if isinstance(text, (list, dict)):
        return text
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@app.route("/api/health")
def api_health():
    """Return process identity and uptime for stale-process detection.

    curl http://localhost:5001/api/health
    """
    uptime = int(_time.time() - _START_TIME)
    return jsonify(
        {
            "status": "ok",
            "pid": _START_PID,
            "uptime_seconds": uptime,
            "started_at": datetime.utcfromtimestamp(_START_TIME).isoformat(),
            "port": int(os.environ.get("PORT", "5001")),
        }
    )


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@app.route("/")
def dashboard():
    with get_db() as db:
        total_jobs      = db.query(Job).count()
        new_jobs        = db.query(Job).filter(Job.status == "new").count()
        analyzed_jobs   = db.query(Job).filter(Job.status == "analyzed").count()
        applied_jobs    = db.query(Job).filter(Job.status == "applied").count()
        total_resumes   = db.query(TailoredResume).count()
        master_count    = db.query(MasterResume).count()
        total_apps      = db.query(Application).count()

        recent_jobs = (
            db.query(Job)
            .order_by(Job.date_scraped.desc())
            .limit(8)
            .all()
        )

        top_resumes = (
            db.query(TailoredResume)
            .options(joinedload(TailoredResume.job))
            .order_by(TailoredResume.match_score.desc())
            .limit(5)
            .all()
        )

        stats = {
            "total_jobs":     total_jobs,
            "new_jobs":       new_jobs,
            "analyzed_jobs":  analyzed_jobs,
            "applied_jobs":   applied_jobs,
            "total_resumes":  total_resumes,
            "master_count":   master_count,
            "total_apps":     total_apps,
        }

    return render_template(
        "dashboard.html",
        stats=stats,
        recent_jobs=recent_jobs,
        top_resumes=top_resumes,
    )


@app.route("/jobs")
def jobs_page():
    return render_template("jobs.html")


@app.route("/resumes")
def resumes_page():
    return render_template("resumes.html")


@app.route("/applications")
def applications_page():
    return render_template("applications.html")


# ---------------------------------------------------------------------------
# API – Jobs
# ---------------------------------------------------------------------------

@app.route("/api/jobs")
def api_jobs():
    status  = request.args.get("status", "").strip()
    search  = request.args.get("search", "").strip()
    limit   = min(int(request.args.get("limit", 200)), 500)
    offset  = int(request.args.get("offset", 0))

    with get_db() as db:
        q = db.query(Job)

        if status:
            q = q.filter(Job.status == status)
        if search:
            like = f"%{search}%"
            q = q.filter(
                Job.job_title.ilike(like) | Job.company_name.ilike(like)
            )

        total = q.count()
        jobs  = q.order_by(Job.date_scraped.desc()).offset(offset).limit(limit).all()

        # Fetch tailored resume scores keyed by job_id
        tailored = {
            r.job_id: r.match_score
            for r in db.query(TailoredResume).all()
        }

    data = []
    for j in jobs:
        data.append({
            "id":           j.id,
            "title":        j.job_title,
            "company":      j.company_name,
            "location":     j.location or "Remote",
            "status":       j.status,
            "source":       j.source,
            "salary":       j.salary_range,
            "date_scraped": j.date_scraped.strftime("%b %d, %Y") if j.date_scraped else "",
            "date_posted":  j.date_posted.isoformat() if j.date_posted else None,
            "match_score":  tailored.get(j.id),
            "has_resume":   j.id in tailored,
            "skills_count": len(j.required_skills or []),
        })

    return jsonify({"jobs": data, "total": total})


@app.route("/api/jobs/<int:job_id>")
def api_job_detail(job_id):
    with get_db() as db:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            return jsonify({"error": "Job not found"}), 404

        tailored = (
            db.query(TailoredResume)
            .filter(TailoredResume.job_id == job_id)
            .order_by(TailoredResume.match_score.desc())
            .first()
        )
        apps = db.query(Application).filter(Application.job_id == job_id).all()

    # Return the score_breakdown stored at generation time (consistent with match_score).
    score_breakdown = tailored.score_breakdown if tailored else None

    # Resolve the name of the resume used to analyze this job (display only)
    analyzed_resume_name = None
    if job.analyzed_with_resume_id:
        try:
            with get_db() as _dbr:
                _ar = _dbr.query(MasterResume).filter(
                    MasterResume.id == job.analyzed_with_resume_id
                ).first()
                analyzed_resume_name = _ar.name if _ar else None
        except Exception:
            pass

    return jsonify({
        "id":               job.id,
        "title":            job.job_title,
        "company":          job.company_name,
        "location":         job.location or "Remote",
        "description":      job.job_description,
        "required_skills":  _safe_json(job.required_skills),
        "preferred_skills": _safe_json(job.preferred_skills),
        "salary_range":     job.salary_range,
        "application_url":  job.application_url,
        "source":           job.source,
        "status":           job.status,
        "date_posted":      job.date_posted.isoformat() if job.date_posted else None,
        "date_scraped":     job.date_scraped.isoformat() if job.date_scraped else None,
        "tailored_resume": {
            "id":           tailored.id,
            "match_score":  tailored.match_score,
            "generated_at": tailored.generated_at.strftime("%b %d, %Y") if tailored.generated_at else "",
            "pdf_path":     tailored.pdf_path,
        } if tailored else None,
        "score_breakdown":           score_breakdown,
        "analyzed_with_resume_id":   job.analyzed_with_resume_id,
        "analyzed_with_resume_name": analyzed_resume_name,
        "applications": [
            {"id": a.id, "status": a.status, "date": a.application_date.isoformat() if a.application_date else None}
            for a in apps
        ],
    })


@app.route("/api/jobs/<int:job_id>/status", methods=["PATCH"])
def api_update_job_status(job_id):
    payload = request.get_json(silent=True) or {}
    new_status = payload.get("status", "").strip()
    allowed = {"applied", "archived"}
    if new_status not in allowed:
        return jsonify({"error": f"status must be one of {sorted(allowed)}"}), 400

    with get_db() as db:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            return jsonify({"error": "Job not found"}), 404
        job.status = new_status
        db.commit()

    return jsonify({"ok": True, "status": new_status})


@app.route("/api/jobs/<int:job_id>/reanalyze", methods=["POST"])
def api_reanalyze_job(job_id: int):
    """Re-extract skills for a job using the currently active resume.

    Updates ``required_skills``, ``preferred_skills``, ``analyzed_with_resume_id``,
    and optionally ``domain``.  Does NOT delete existing tailored resumes.
    """
    try:
        from analyzer.keyword_extractor import KeywordExtractor
        from analyzer.scoring import ScoringEngine

        with get_db() as db:
            job = db.query(Job).filter(Job.id == job_id).first()
            if not job:
                return jsonify({"error": "Job not found"}), 404
            if not job.job_description:
                return jsonify({"error": "Job has no description to analyze"}), 400

            master = (
                db.query(MasterResume)
                .filter(MasterResume.is_active == True)
                .first()
            )
            if not master:
                return jsonify({"error": "No active master resume found"}), 400

            extractor = KeywordExtractor()
            extracted = extractor.extract(job.job_description)

            job.required_skills = extracted["required_skills"] or None
            job.preferred_skills = extracted["preferred_skills"] or None
            job.status = "analyzed"
            job.analyzed_with_resume_id = master.id

            # Optional domain refinement (mirrors analyze_new_jobs_task)
            try:
                from analyzer.domain_detector import DomainDetector
                detected = DomainDetector().detect_from_job(job)
                job.domain = detected.get("domain", job.domain)
            except Exception:
                pass

            score_result = ScoringEngine().score(job, master)

        return jsonify({
            "ok":                        True,
            "required_skills_count":     len(job.required_skills or []),
            "preferred_skills_count":    len(job.preferred_skills or []),
            "match_score":               round(score_result.total_score, 1),
            "analyzed_with_resume_id":   master.id,
            "analyzed_with_resume_name": master.name,
        })

    except Exception as exc:
        logger.exception("Reanalysis failed for job %s", job_id)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/jobs/reanalyze-all", methods=["POST"])
def api_reanalyze_all_jobs():
    """Reset all analyzed jobs to 'new' so they get re-analyzed with the improved extractor.

    Clears ``required_skills``, ``preferred_skills``, and ``analyzed_with_resume_id`` for
    every job, then sets ``status = 'new'`` so the next Analyze Jobs run processes them.

    Does NOT delete tailored resumes — existing generated resumes are kept.
    """
    try:
        with get_db() as db:
            jobs = db.query(Job).filter(Job.status == "analyzed").all()
            count = len(jobs)
            for job in jobs:
                job.status = "new"
                job.required_skills = None
                job.preferred_skills = None
                job.analyzed_with_resume_id = None
            db.commit()

        return jsonify({"ok": True, "jobs_reset": count,
                        "message": f"Reset {count} jobs to 'new'. Run Analyze Jobs to re-extract skills."})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# API – Resumes
# ---------------------------------------------------------------------------

@app.route("/api/resumes/master")
def api_master_resumes():
    """List master resumes for dashboard cards (light payload, explicit flags)."""
    with get_db() as db:
        masters = db.query(MasterResume).order_by(MasterResume.created_at.desc()).all()

    out: list[dict] = []
    for m in masters:
        d = m.to_dict()
        content = m.content if isinstance(m.content, dict) else {}
        out.append(
            {
                "id": m.id,
                "name": m.name,
                "is_active": bool(d.get("is_active")),
                "is_sample": bool(d.get("is_sample")),
                "domain": d.get("domain"),
                "domains": m.domains or ([m.domain] if m.domain else []),
                "created_at": m.created_at.strftime("%b %d, %Y") if m.created_at else "",
                "sections": list(content.keys()) if content else [],
            }
        )
    return jsonify(out)


@app.route("/api/resumes/tailored")
def api_tailored_resumes():
    limit  = min(int(request.args.get("limit", 100)), 300)
    offset = int(request.args.get("offset", 0))

    with get_db() as db:
        total = db.query(TailoredResume).count()
        rows  = (
            db.query(TailoredResume)
            .order_by(TailoredResume.match_score.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        # Pre-fetch job titles
        job_map = {
            j.id: j for j in
            db.query(Job).filter(Job.id.in_([r.job_id for r in rows])).all()
        }

    data = []
    for r in rows:
        j = job_map.get(r.job_id)
        data.append({
            "id":           r.id,
            "job_id":       r.job_id,
            "job_title":    j.job_title if j else "—",
            "company":      j.company_name if j else "—",
            "match_score":  r.match_score,
            "generated_at": r.generated_at.strftime("%b %d, %Y") if r.generated_at else "",
            "has_pdf":      bool(r.pdf_path and Path(r.pdf_path).exists()),
            "pdf_path":     r.pdf_path,
        })

    return jsonify({"resumes": data, "total": total})


@app.route("/api/resumes/tailored/<int:resume_id>")
def api_tailored_resume_detail(resume_id):
    with get_db() as db:
        r = db.query(TailoredResume).filter(TailoredResume.id == resume_id).first()
        if not r:
            return jsonify({"error": "Resume not found"}), 404
        j = db.query(Job).filter(Job.id == r.job_id).first()

    content = r.tailored_content or {}
    return jsonify({
        "id":           r.id,
        "job_id":       r.job_id,
        "job_title":    j.job_title if j else "—",
        "company":      j.company_name if j else "—",
        "match_score":  r.match_score,
        "generated_at": r.generated_at.isoformat() if r.generated_at else None,
        "has_pdf":      bool(r.pdf_path and Path(r.pdf_path).exists()),
        "content":      content,
    })


# ---------------------------------------------------------------------------
# API – Actions
# ---------------------------------------------------------------------------

@app.route("/api/generate-resume", methods=["POST"])
def api_generate_resume():
    """Trigger Gemini resume generation for a given job."""
    payload  = request.get_json(silent=True) or {}
    job_id   = payload.get("job_id")
    if not job_id:
        return jsonify({"error": "job_id required"}), 400

    try:
        from analyzer.scoring import ScoringEngine
        from resume_engine.modifier import ResumeModifier

        with get_db() as db:
            job = db.query(Job).filter(Job.id == job_id).first()
            if not job:
                return jsonify({"error": "Job not found"}), 404

            if job.status == "new":
                return jsonify({
                    "error": "not_analyzed",
                    "message": "This job has not been analyzed yet. Run Analyze Jobs first.",
                }), 422

            master = (
                db.query(MasterResume)
                .filter(MasterResume.is_active == True)
                .first()
            )
            if not master:
                return jsonify({"error": "No active master resume found"}), 400

            # Block generation when the job was analyzed with a different resume.
            # NULL analyzed_with_resume_id means old job (pre-feature) — allow it.
            if (job.analyzed_with_resume_id is not None
                    and job.analyzed_with_resume_id != master.id):
                analyzed_with = db.query(MasterResume).filter(
                    MasterResume.id == job.analyzed_with_resume_id
                ).first()
                return jsonify({
                    "error":                     "resume_mismatch",
                    "analyzed_with_resume_name": analyzed_with.name if analyzed_with else "Unknown",
                    "analyzed_with_domain":      analyzed_with.domain if analyzed_with else None,
                    "active_resume_name":        master.name,
                    "active_domain":             master.domain,
                }), 409

            engine = ScoringEngine()
            score_result = engine.score(job, master)

            modifier = ResumeModifier()
            tailored = modifier.modify_resume(
                master,
                job,
                {
                    "overall_score": score_result.total_score,
                    "matched_skills": [],
                    "missing_skills": [],
                },
                style_fingerprint=master.style_fingerprint,
            )

            # ModificationResult is a dataclass; plain dicts are also accepted
            # (e.g. from test mocks that return dict results).
            if isinstance(tailored, dict):
                tailored_content = tailored["content"]
            else:
                tailored_content = tailored.content

            # Upsert
            existing = (
                db.query(TailoredResume)
                .filter(
                    TailoredResume.job_id == job_id,
                    TailoredResume.master_resume_id == master.id,
                )
                .first()
            )
            if existing:
                existing.tailored_content = tailored_content
                existing.match_score      = score_result.total_score
                existing.score_breakdown  = score_result.score_breakdown
                existing.generated_at     = datetime.now(timezone.utc)
                resume_id = existing.id
            else:
                new_r = TailoredResume(
                    job_id=job_id,
                    master_resume_id=master.id,
                    tailored_content=tailored_content,
                    match_score=score_result.total_score,
                    score_breakdown=score_result.score_breakdown,
                    generated_at=datetime.now(timezone.utc),
                )
                db.add(new_r)
                db.flush()
                resume_id = new_r.id
            db.commit()

        api_calls = tailored.api_calls_used if not isinstance(tailored, dict) else 0
        return jsonify({
            "ok": True,
            "resume_id": resume_id,
            "match_score": score_result.total_score,
            "api_calls_used": api_calls,
            "tailoring_applied": api_calls > 0,
        })

    except Exception as exc:
        logger.exception("Resume generation failed for job %s", job_id)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/export-pdf", methods=["POST"])
def api_export_pdf():
    """Generate or re-generate a PDF for a tailored resume."""
    payload   = request.get_json(silent=True) or {}
    resume_id = payload.get("resume_id")
    template  = payload.get("template", "ats")
    if not resume_id:
        return jsonify({"error": "resume_id required"}), 400

    try:
        from pdf_generator.generator import PDFGenerator

        with get_db() as db:
            r = db.query(TailoredResume).filter(TailoredResume.id == resume_id).first()
            if not r:
                return jsonify({"error": "Resume not found"}), 404
            content = r.tailored_content

            generator = PDFGenerator()
            pdf_path  = generator.generate(content, template)
            r.pdf_path = pdf_path
            db.commit()

        return jsonify({"ok": True, "pdf_path": pdf_path})

    except Exception as exc:
        logger.exception("PDF export failed for resume %s", resume_id)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/download-pdf/<int:resume_id>")
def api_download_pdf(resume_id):
    with get_db() as db:
        r = db.query(TailoredResume).filter(TailoredResume.id == resume_id).first()
        if not r:
            return jsonify({"error": "Resume not found"}), 404
        if not r.pdf_path:
            return jsonify({
                "error": "No PDF generated yet. Use Export PDF to generate one."
            }), 404

        # Normalise separators and resolve relative paths
        pdf_path = str(r.pdf_path).replace("/", os.sep).replace("\\", os.sep)
        if not os.path.isabs(pdf_path):
            pdf_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                pdf_path,
            )
        pdf_path = os.path.normpath(pdf_path)

        if not os.path.exists(pdf_path):
            return jsonify({
                "error": f"PDF file not found on disk. Try Re-export PDF to regenerate it."
            }), 404

    try:
        return send_file(
            pdf_path,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"resume_{resume_id}.pdf",
        )
    except Exception as exc:
        logger.error("PDF download failed for resume %s: %s", resume_id, exc)
        return jsonify({"error": f"Download failed: {exc}"}), 500


# ---------------------------------------------------------------------------
# API – Stats (dashboard)
# ---------------------------------------------------------------------------

@app.route("/api/stats")
def api_stats():
    with get_db() as db:
        total_jobs     = db.query(Job).count()
        new_jobs       = db.query(Job).filter(Job.status == "new").count()
        analyzed_jobs  = db.query(Job).filter(Job.status == "analyzed").count()
        applied_jobs   = db.query(Job).filter(Job.status == "applied").count()
        total_resumes  = db.query(TailoredResume).count()
        total_apps     = db.query(Application).count()

        # Score distribution
        scores = [
            r.match_score for r in db.query(TailoredResume).all()
            if r.match_score is not None
        ]
        avg_score = round(sum(scores) / len(scores), 1) if scores else 0

        # Jobs by source
        from sqlalchemy import func
        by_source = dict(
            db.query(Job.source, func.count(Job.id))
            .group_by(Job.source)
            .all()
        )

    # NVIDIA quota stats (read from persisted usage file, no API call needed)
    nvidia_stats = {"model": "nvidia/llama-3.3-nemotron-super-49b-v1", "used": 0, "limit": 5000}
    try:
        import json as _json
        from pathlib import Path as _Path

        _usage_file = _Path("data") / "gemini_usage.json"
        if _usage_file.exists():
            _raw = _json.loads(_usage_file.read_text(encoding="utf-8"))
            if isinstance(_raw.get("nvidia"), dict):
                nvidia_stats["used"] = _raw["nvidia"].get("calls", 0)
    except Exception:
        pass  # Non-critical — dashboard still renders without quota stats

    return jsonify({
        "total_jobs":    total_jobs,
        "new_jobs":      new_jobs,
        "analyzed_jobs": analyzed_jobs,
        "applied_jobs":  applied_jobs,
        "total_resumes": total_resumes,
        "total_apps":    total_apps,
        "avg_score":     avg_score,
        "by_source":     by_source,
        "nvidia":        nvidia_stats,
    })


# ---------------------------------------------------------------------------
# Manual Pipeline Controls — state tracking
# ---------------------------------------------------------------------------

# Track whether each manual task is currently running (process-lifetime only).
_task_running: dict = {"scrape": False, "analyze": False, "generate": False}

# ISO timestamps of when each task last completed (process-lifetime only).
_task_last_run: dict = {"scrape": None, "analyze": None, "generate": None}

# Serialize "start task" checks so two POSTs cannot both pass before the worker thread sets flags.
_task_start_locks: dict = {k: threading.Lock() for k in _task_running}


# ---------------------------------------------------------------------------
# API – Manual task triggers
# ---------------------------------------------------------------------------

@app.route("/api/run/scrape", methods=["POST"])
def run_scrape():
    """Trigger scrape_jobs_task in a background thread and return immediately."""
    from scheduler.tasks import scrape_jobs_task

    with _task_start_locks["scrape"]:
        if _task_running["scrape"]:
            return jsonify(
                {"error": "Scrape already in progress.", "status": "busy"}
            ), 409
        _task_running["scrape"] = True

    def _run() -> None:
        try:
            # Pass no search_configs so scrape_jobs_task() resolves them itself
            # using the active resume's domain (industry configs) as the primary
            # source.  Manual configs in settings.json are only used as a fallback
            # when no active domain is set.
            logger.info("Manual scrape_jobs_task starting (domain-aware mode).")
            scrape_jobs_task()
            logger.info("Manual scrape_jobs_task completed.")
        except Exception as exc:
            app.logger.error("Manual scrape failed: %s", exc)
        finally:
            _task_running["scrape"] = False
            _task_last_run["scrape"] = datetime.now(timezone.utc).isoformat()

    try:
        t = threading.Thread(target=_run, daemon=True)
        t.start()
    except Exception:
        _task_running["scrape"] = False
        raise
    return jsonify({"status": "started", "task": "scrape_jobs_task"})


@app.route("/api/run/analyze", methods=["POST"])
def run_analyze():
    """Trigger analyze_new_jobs_task in a background thread and return immediately."""
    from scheduler.tasks import analyze_new_jobs_task

    def _run() -> None:
        _task_running["analyze"] = True
        try:
            logger.info("Manual analyze_new_jobs_task started.")
            analyze_new_jobs_task()
            logger.info("Manual analyze_new_jobs_task completed.")
        except Exception as exc:
            app.logger.error("Manual analyze failed: %s", exc)
        finally:
            _task_running["analyze"] = False
            _task_last_run["analyze"] = datetime.now(timezone.utc).isoformat()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"status": "started", "task": "analyze_new_jobs_task"})


@app.route("/api/run/generate", methods=["POST"])
def run_generate():
    """Trigger generate_resumes_task in a background thread and return immediately."""
    from scheduler.tasks import generate_resumes_task
    import yaml  # type: ignore[import-untyped]

    def _run() -> None:
        _task_running["generate"] = True
        try:
            threshold = 35.0
            try:
                with open("config.yaml", "r", encoding="utf-8") as fh:
                    raw = yaml.safe_load(fh) or {}
                threshold = float(
                    raw.get("scheduler", {}).get("auto_generate_threshold", 35.0)
                )
            except Exception:
                pass
            logger.info("Manual generate_resumes_task started (threshold=%.1f).", threshold)
            generate_resumes_task(threshold)
            logger.info("Manual generate_resumes_task completed.")
        except Exception as exc:
            app.logger.error("Manual generate failed: %s", exc)
        finally:
            _task_running["generate"] = False
            _task_last_run["generate"] = datetime.now(timezone.utc).isoformat()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"status": "started", "task": "generate_resumes_task"})


@app.route("/api/run/status", methods=["GET"])
def run_status():
    """Return whether each manual task is currently running."""
    return jsonify({
        "scrape":   _task_running.get("scrape", False),
        "analyze":  _task_running.get("analyze", False),
        "generate": _task_running.get("generate", False),
    })


@app.route("/api/run/last-run", methods=["GET"])
def run_last_run():
    """Return ISO timestamps of when each manual task last completed."""
    return jsonify(_task_last_run)


# ---------------------------------------------------------------------------
# Settings page + API
# ---------------------------------------------------------------------------

@app.route("/settings")
def settings_page():
    """Render the automation settings page."""
    return render_template("settings.html")


@app.route("/api/settings", methods=["GET"])
def get_settings():
    """Return the full contents of data/settings.json."""
    return jsonify(settings_manager.load())


_TOGGLEABLE_TASKS = {"scrape", "generate"}


@app.route("/api/settings/automation/<task>", methods=["PATCH"])
def patch_automation_setting(task: str):
    """Update mode and/or schedule for a toggleable task.

    Body (JSON, all fields optional):
        {"mode": "automatic", "schedule": "08:30"}

    Returns 400 for unknown tasks, invalid modes, or bad schedule format.
    """
    if task not in _TOGGLEABLE_TASKS:
        return jsonify({
            "error": f"Task {task!r} is not toggleable. Only: {sorted(_TOGGLEABLE_TASKS)}"
        }), 400

    body = request.get_json(silent=True) or {}
    mode = body.get("mode")
    schedule = body.get("schedule")

    if mode is None and schedule is None:
        return jsonify({"error": "Provide at least one of: mode, schedule"}), 400

    # Validate and apply mode
    if mode is not None:
        try:
            settings_manager.set_mode(task, mode)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    # Validate and apply schedule
    if schedule is not None:
        try:
            settings_manager.set_schedule(task, schedule)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    # Live-update the scheduler if one is running
    try:
        # Import lazily — scheduler may not be started at all
        from scheduler.scheduler import SchedulerManager as _SM
        # We don't hold a reference to a running scheduler instance here,
        # so reschedule is best-effort; if no scheduler is running, log only.
        logger.info(
            "[settings] Mode for %r updated to %r. "
            "Restart the scheduler or use reschedule_task() to apply live.",
            task, mode or "(unchanged)",
        )
    except Exception:
        pass

    updated = settings_manager.load().get("automation", {}).get(task, {})
    return jsonify({"status": "saved", "task": task, "settings": updated})


# ---------------------------------------------------------------------------
# API – Resume mode selector (sample vs own)
# ---------------------------------------------------------------------------

def _resume_summary(master) -> dict:
    """Return a compact summary dict for a MasterResume ORM object."""
    content = master.content or {}
    skills = content.get("skills", [])
    # skills may be a list of strings or list of dicts
    skills_count = len(skills) if isinstance(skills, list) else 0
    return {
        "id":           master.id,
        "name":         master.name,
        "is_active":    master.is_active,
        "is_sample":    master.is_sample,
        "skills_count": skills_count,
        "created_at":   master.created_at.isoformat() if master.created_at else None,
        "domain":       master.domain,
        "domain_display": DOMAINS.get(master.domain, "") if master.domain else "",
        "domains":      master.domains or ([master.domain] if master.domain else []),
    }


@app.route("/api/resume/mode", methods=["GET"])
def api_get_resume_mode():
    """Return current resume mode and summary of both sample/user resumes."""
    with get_db() as db:
        # Return the active sample resume (or fall back to first sample available)
        sample_resume = (
            db.query(MasterResume)
            .filter(MasterResume.is_sample == True, MasterResume.is_active == True)
            .first()
        ) or (
            db.query(MasterResume)
            .filter(MasterResume.is_sample == True)
            .first()
        )
        user_resume = (
            db.query(MasterResume)
            .filter(MasterResume.is_sample == False)
            .order_by(MasterResume.created_at.desc())
            .first()
        )

    return jsonify({
        "mode":          settings_manager.get_resume_mode(),
        "sample_resume": _resume_summary(sample_resume) if sample_resume else None,
        "user_resume":   _resume_summary(user_resume) if user_resume else None,
    })


@app.route("/api/resume/mode", methods=["PATCH"])
def api_patch_resume_mode():
    """Switch the active master resume between sample and own modes.

    Body (JSON):
        mode       (required) – ``"sample"`` or ``"own"``
        domain     (optional) – when ``mode="sample"``, activates the sample
                                resume for that specific industry domain.
                                Omitting domain falls back to any sample resume.
        resume_id  (optional) – when ``mode="own"``, activates that user
                                (non-sample) resume. If omitted, uses the
                                most recently created user resume.
    """
    body = request.get_json(silent=True) or {}
    mode = body.get("mode")
    domain = body.get("domain")  # optional; used when mode=="sample"
    resume_id = body.get("resume_id")  # optional; used when mode=="own"

    if mode not in ("sample", "own"):
        return jsonify({"error": "mode must be 'sample' or 'own'"}), 400

    with get_db() as db:
        if mode == "sample":
            q = db.query(MasterResume).filter(MasterResume.is_sample.is_(True))
            if domain:
                if domain not in DOMAINS:
                    return jsonify({"error": f"Invalid domain {domain!r}"}), 400
                q = q.filter(MasterResume.domain == domain)
            target = q.first()
            if not target:
                return jsonify({
                    "error":  "Sample resume not found",
                    "domain": domain,
                }), 404
        else:  # own
            if resume_id is not None:
                try:
                    rid = int(resume_id)
                except (TypeError, ValueError):
                    return jsonify({"error": "invalid resume_id"}), 400
                target = (
                    db.query(MasterResume)
                    .filter(
                        MasterResume.id == rid,
                        MasterResume.is_sample.is_(False),
                    )
                    .first()
                )
                if not target:
                    return jsonify({
                        "error":   "no_user_resume",
                        "message": "User resume not found",
                    }), 404
            else:
                target = (
                    db.query(MasterResume)
                    .filter(MasterResume.is_sample.is_(False))
                    .order_by(MasterResume.created_at.desc())
                    .first()
                )
                if not target:
                    return jsonify({
                        "error":   "no_user_resume",
                        "message": "No resume uploaded yet",
                    }), 404

        # Deactivate all, then activate target (flush so ORM state is consistent)
        db.query(MasterResume).update({"is_active": False})
        db.flush()
        target.is_active = True
        db.commit()
        active_name = target.name
        active_domain = target.domain
        active_id = target.id

    # Persist mode in settings only after DB commit succeeds
    settings_manager.set_resume_mode(mode)
    domain_display = DOMAINS.get(active_domain, "") if active_domain else ""
    if mode == "sample":
        logger.info(
            "Active domain changed to: %r (sample resume %r)",
            active_domain,
            active_name,
        )
    else:
        logger.info(
            "Active domain changed to: %r (own resume %r, id=%s)",
            active_domain,
            active_name,
            active_id,
        )
    return jsonify({
        "status":         "switched",
        "mode":           mode,
        "active_resume":  active_name,
        "resume_id":      active_id,
        "domain":         active_domain,
        "domain_display": domain_display,
    })


# ---------------------------------------------------------------------------
# API – Upload PDF resume
# ---------------------------------------------------------------------------

@app.route("/api/resume/upload", methods=["POST"])
def api_resume_upload():
    """Parse an uploaded PDF and store it as a new MasterResume.

    Form fields:
        file  (required) – PDF file
        name  (optional) – human-readable name for this resume

    On success activates the new resume and sets resume_mode = "own".
    """
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    _ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt"}
    import os as _os
    file_ext = _os.path.splitext(f.filename.lower())[1]
    if file_ext not in _ALLOWED_EXTENSIONS:
        return jsonify({
            "error": (
                f"Unsupported file type '{file_ext}'. "
                "Upload a PDF (.pdf), Word document (.docx), or plain text (.txt) file."
            )
        }), 400

    file_bytes = f.read()
    if not file_bytes:
        return jsonify({"error": "Uploaded file is empty"}), 400

    try:
        from pdf_generator.pdf_parser import NotAResumeError, ResumePDFParser
        parser = ResumePDFParser()
        content = parser.parse(file_bytes, file_extension=file_ext.lstrip("."))
    except NotAResumeError as exc:
        return jsonify({
            "error":         "not_a_resume",
            "message":       (
                f"This doesn't look like a resume. "
                f"Detected document type: {exc.document_type}. "
                f"{exc.reason}"
            ),
            "document_type": exc.document_type,
            "confidence":    exc.confidence,
        }), 422
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 422
    except Exception as exc:
        logger.exception("PDF parsing failed")
        return jsonify({"error": f"Parsing error: {exc}"}), 500

    name = (request.form.get("name") or "").strip()
    if not name:
        # Derive name from personal_info or filename
        name = (
            content.get("personal_info", {}).get("name")
            or f.filename.rsplit(".", 1)[0].replace("_", " ").title()
            or "Uploaded Resume"
        )

    # Extract style fingerprint before storing
    try:
        from resume_engine.style_extractor import StyleExtractor
        style = StyleExtractor().extract(content)
        logger.info(
            "Style fingerprint extracted: voice=%s, structure=%s, metrics=%s",
            style.get("voice"), style.get("sentence_structure", {}).get("style"),
            style.get("metric_usage", {}).get("density"),
        )
    except Exception as _se:
        logger.warning("Style extraction failed (%s) — storing None.", _se)
        style = None

    # Detect domain from resume content
    try:
        from analyzer.domain_detector import DomainDetector
        detected = DomainDetector().detect_from_resume(content)
        detected_domain = detected.get("domain", "other")
        detected_domains = detected.get("domains")  # list from NIM, or None
        domain_confidence = detected.get("confidence", 0.0)
        logger.info(
            "Domain detected: %s domains=%s (confidence=%.2f)",
            detected_domain, detected_domains, domain_confidence,
        )
    except Exception as _de:
        logger.warning("Domain detection failed (%s) — storing None.", _de)
        detected_domain = None
        domain_confidence = 0.0

    with get_db() as db:
        # Deactivate all current master resumes
        db.query(MasterResume).update({"is_active": False})
        new_resume = MasterResume(
            name=name,
            content=content,
            is_active=True,
            is_sample=False,
            style_fingerprint=style,
            domain=detected_domain,
            domains=detected_domains or ([detected_domain] if detected_domain else None),
        )
        db.add(new_resume)
        db.commit()
        resume_id = new_resume.id

    # Update settings to "own" mode
    settings_manager.set_resume_mode("own")
    logger.info("PDF resume uploaded: id=%d name=%r domain=%r", resume_id, name, detected_domain)

    skills = content.get("skills", [])
    experience = content.get("work_experience", [])
    upload_warnings: List[str] = []

    if detected_domain == "other":
        upload_warnings.append(
            "Could not auto-detect resume domain. Please set it manually from the Resumes page."
        )
    if not skills and not experience:
        upload_warnings.append(
            "Resume parsed with no skills or experience detected. "
            "You may want to re-upload or edit manually."
        )

    style_summary = {}
    if style:
        style_summary = {
            "voice":       style.get("voice", "no_pronouns"),
            "structure":   style.get("sentence_structure", {}).get("style", "moderate"),
            "metrics":     style.get("metric_usage", {}).get("density", "light"),
            "bullet_char": style.get("format", {}).get("bullet_char", "none"),
        }
    response: Dict[str, Any] = {
        "ok":              True,
        "id":              resume_id,
        "name":            name,
        "skills_count":    len(skills) if isinstance(skills, list) else 0,
        "style":           style_summary,
        "domain":          detected_domain,
        "domains":         detected_domains or ([detected_domain] if detected_domain else []),
        "domain_display":  DOMAINS.get(detected_domain, "") if detected_domain else "",
        "domain_confidence": round(domain_confidence, 4),
    }
    if upload_warnings:
        response["warnings"] = upload_warnings
    return jsonify(response)


# ---------------------------------------------------------------------------
# Active context + sample resume preview routes
# ---------------------------------------------------------------------------


@app.route("/api/active-context", methods=["GET"])
def api_active_context():
    """Return a summary of the current active resume and associated search configs.

    Response shape::

        {
          "active_resume": {"id", "name", "domain", "domain_display",
                            "is_sample", "skills_count"},
          "industry_search_configs": [...],
          "user_search_configs": [...],
          "total_configs": int,
          "mode": "sample" | "own"
        }
    """
    with get_db() as db:
        db.expire_all()
        mr = db.query(MasterResume).filter(MasterResume.is_active.is_(True)).first()

    active_resume_data = None
    industry_cfgs: list = []
    user_cfgs: list = []

    if mr:
        skills = mr.content.get("skills", []) if isinstance(mr.content, dict) else []
        active_resume_data = {
            "id":           mr.id,
            "name":         mr.name,
            "domain":       mr.domain,
            "domain_display": DOMAINS.get(mr.domain, "") if mr.domain else "",
            "is_sample":    mr.is_sample,
            "skills_count": len(skills),
        }
        if mr.domain:
            active_domains = (
                [d for d in (mr.domains or []) if d in DOMAINS and d != "other"]
                or ([mr.domain] if mr.domain and mr.domain != "other" else [])
            )
            industry_cfgs = settings_manager.get_industry_search_configs_for_domains(active_domains)
            user_cfgs = [
                c for c in settings_manager.get_search_configs(enabled_only=True)
                if c.get("domain") in active_domains
            ]

    mode = settings_manager.get_resume_mode()
    total = len(industry_cfgs) + len(user_cfgs)

    return jsonify({
        "active_resume":          active_resume_data,
        "industry_search_configs": industry_cfgs,
        "user_search_configs":     user_cfgs,
        "total_configs":           total,
        "mode":                    mode,
    })


@app.route("/api/sample-resume/<domain>", methods=["GET"])
def api_sample_resume_preview(domain: str):
    """Return a preview of the sample resume for *domain*.

    Response shape::

        {
          "id", "name", "domain", "persona_name",
          "skills_count", "experience_count", "skills",
          "industry_search_configs"
        }
    """
    if domain not in DOMAINS:
        return jsonify({"error": f"Unknown domain {domain!r}"}), 404

    with get_db() as db:
        mr = (
            db.query(MasterResume)
            .filter(MasterResume.is_sample == True, MasterResume.domain == domain)
            .first()
        )

    if not mr:
        return jsonify({"error": f"Sample resume for domain '{domain}' not found"}), 404

    content = mr.content if isinstance(mr.content, dict) else {}
    skills = content.get("skills", [])
    work_exp = content.get("work_experience", [])
    persona_name = content.get("personal_info", {}).get("name", "")
    industry_cfgs = settings_manager.get_industry_search_configs(domain)

    return jsonify({
        "id":                      mr.id,
        "name":                    mr.name,
        "domain":                  domain,
        "domain_display":          DOMAINS.get(domain, ""),
        "persona_name":            persona_name,
        "skills_count":            len(skills),
        "experience_count":        len(work_exp),
        "skills":                  skills[:15],
        "industry_search_configs": industry_cfgs,
    })


# ---------------------------------------------------------------------------
# Delete master resume route
# ---------------------------------------------------------------------------


@app.route("/api/resumes/master/<int:resume_id>", methods=["DELETE"])
def api_delete_master_resume(resume_id: int):
    """Delete an uploaded master resume and all its tailored resumes.

    Sample resumes cannot be deleted.  The currently active resume cannot be
    deleted — switch to another resume first.
    """
    with get_db() as db:
        resume = db.query(MasterResume).filter(MasterResume.id == resume_id).first()
        if not resume:
            return jsonify({"error": "Resume not found"}), 404
        if resume.is_sample:
            return jsonify({"error": "Sample resumes cannot be deleted"}), 403
        if resume.is_active:
            return jsonify({
                "error": "Cannot delete the active resume. Switch to another resume first."
            }), 409

        # Collect PDF paths before cascade-deleting so we can clean up disk files
        pdf_paths = [
            tr.pdf_path
            for tr in resume.tailored_resumes
            if tr.pdf_path
        ]

        db.delete(resume)
        db.commit()

    # Best-effort disk cleanup — don't fail if files are already gone
    for path in pdf_paths:
        try:
            import os as _os
            if _os.path.isfile(path):
                _os.remove(path)
        except Exception as _e:
            logger.warning("Could not delete PDF %s: %s", path, _e)

    logger.info("Master resume #%d deleted (%d PDF(s) removed)", resume_id, len(pdf_paths))
    return jsonify({"ok": True, "deleted_id": resume_id})


# ---------------------------------------------------------------------------
# Domain override route
# ---------------------------------------------------------------------------


@app.route("/api/resume/<int:resume_id>/domain", methods=["PATCH"])
def api_resume_set_domain(resume_id: int):
    """Override the domain(s) on a master resume.

    Accepts either a single domain or a list:
        {"domain": "software_engineering"}
        {"domains": ["software_engineering", "ai_ml"]}
    When ``domains`` is provided it takes precedence; ``domain`` is set to
    the first entry for backwards compatibility.
    """
    payload = request.get_json(silent=True) or {}

    domains_list = payload.get("domains")
    if domains_list is not None:
        # Multi-domain path
        if not isinstance(domains_list, list) or not domains_list:
            return jsonify({"error": "domains must be a non-empty list"}), 400
        invalid = [d for d in domains_list if d not in DOMAINS or d == "other"]
        if invalid:
            return jsonify({"error": f"invalid domains: {invalid}", "valid": list(DOMAINS.keys())}), 400
        primary = domains_list[0]
    else:
        # Single-domain path (backwards compat)
        domain = payload.get("domain", "").strip()
        if not domain:
            return jsonify({"error": "domain or domains required"}), 400
        if domain not in DOMAINS:
            return jsonify({"error": f"invalid domain {domain!r}", "valid": list(DOMAINS.keys())}), 400
        primary = domain
        domains_list = [domain]

    with get_db() as db:
        resume = db.query(MasterResume).filter(MasterResume.id == resume_id).first()
        if not resume:
            return jsonify({"error": "Resume not found"}), 404
        resume.domain = primary
        resume.domains = domains_list
        db.commit()

    logger.info("Resume #%d domain overridden to %r (domains=%r)", resume_id, primary, domains_list)
    return jsonify({"status": "updated", "domain": primary, "domains": domains_list, "display_name": DOMAINS[primary]})


# ---------------------------------------------------------------------------
# Preferred location setting
# ---------------------------------------------------------------------------


@app.route("/api/settings/location", methods=["GET"])
def api_get_preferred_location():
    """Return the user's preferred scraping location."""
    return jsonify({"preferred_location": settings_manager.get_preferred_location()})


@app.route("/api/settings/location", methods=["PATCH"])
def api_set_preferred_location():
    """Update the user's preferred scraping location."""
    payload = request.get_json(silent=True) or {}
    location = str(payload.get("preferred_location", "")).strip()
    settings_manager.set_preferred_location(location)
    return jsonify({"status": "updated", "preferred_location": location})


# ---------------------------------------------------------------------------
# Search config routes
# ---------------------------------------------------------------------------


@app.route("/api/search-configs", methods=["GET"])
def api_get_search_configs():
    """Return all search configs (including disabled ones)."""
    configs = settings_manager.get_search_configs(enabled_only=False)
    return jsonify({"configs": configs, "total": len(configs)})


@app.route("/api/search-configs", methods=["POST"])
def api_add_search_config():
    """Add a new search config."""
    payload = request.get_json(silent=True) or {}
    try:
        new_id = settings_manager.add_search_config(payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    # Return the newly created config
    all_configs = settings_manager.get_search_configs(enabled_only=False)
    new_cfg = next((c for c in all_configs if c.get("id") == new_id), None)
    return jsonify({"status": "created", "config": new_cfg, "id": new_id}), 201


@app.route("/api/search-configs/<config_id>", methods=["PATCH"])
def api_update_search_config(config_id: str):
    """Update fields on an existing search config."""
    payload = request.get_json(silent=True) or {}
    try:
        found = settings_manager.update_search_config(config_id, payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if not found:
        return jsonify({"error": "Config not found"}), 404

    all_configs = settings_manager.get_search_configs(enabled_only=False)
    updated_cfg = next((c for c in all_configs if c.get("id") == config_id), None)
    return jsonify({"status": "updated", "config": updated_cfg})


@app.route("/api/search-configs/<config_id>", methods=["DELETE"])
def api_delete_search_config(config_id: str):
    """Delete a search config by id."""
    deleted = settings_manager.delete_search_config(config_id)
    if not deleted:
        return jsonify({"error": "Config not found"}), 404
    return jsonify({"status": "deleted", "id": config_id})


# ---------------------------------------------------------------------------
# Domain resume assignment routes
# ---------------------------------------------------------------------------


@app.route("/api/domain-resumes", methods=["GET"])
def api_get_domain_resumes():
    """Return all domain → resume mappings with resume names."""
    mappings = {}
    with get_db() as db:
        for domain, display_name in DOMAINS.items():
            resume_id = settings_manager.get_domain_resume(domain)
            resume_name = None
            if resume_id is not None:
                mr = db.query(MasterResume).filter(MasterResume.id == resume_id).first()
                resume_name = mr.name if mr else None
                if mr is None:
                    resume_id = None  # stale reference
            mappings[domain] = {
                "resume_id":   resume_id,
                "resume_name": resume_name,
                "display_name": display_name,
            }
    return jsonify({"mappings": mappings})


@app.route("/api/domain-resumes/<domain>", methods=["PATCH"])
def api_set_domain_resume(domain: str):
    """Assign (or clear) a resume for a domain."""
    if domain not in DOMAINS:
        return jsonify({"error": f"Invalid domain {domain!r}", "valid": list(DOMAINS.keys())}), 400

    payload = request.get_json(silent=True) or {}
    resume_id = payload.get("resume_id")

    # Validate resume exists if id provided
    if resume_id is not None:
        try:
            resume_id = int(resume_id)
        except (TypeError, ValueError):
            return jsonify({"error": "resume_id must be an integer or null"}), 400
        with get_db() as db:
            mr = db.query(MasterResume).filter(MasterResume.id == resume_id).first()
            if not mr:
                return jsonify({"error": f"Resume {resume_id} not found"}), 404

    settings_manager.set_domain_resume(domain, resume_id)
    return jsonify({"status": "updated", "domain": domain, "resume_id": resume_id})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import socket

    # Default 5001: Windows often reserves 5000 (AirPlay / other services), and a
    # second stuck Flask instance on 5000 causes endless "loading". Override: PORT=5000
    _port = int(os.environ.get("PORT", "5001"))

    # Warn immediately if another process already owns the port.
    # This surfaces the stale-process problem rather than silently binding alongside it.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
        if _s.connect_ex(("127.0.0.1", _port)) == 0:
            print(
                f"\nWARNING: port {_port} is already in use by another process!\n"
                f"  A stale Flask server is likely running. Kill it first:\n\n"
                f"  Windows:   .\\scripts\\start_app.ps1\n"
                f"  Mac/Linux: ./scripts/start_app.sh\n\n"
                f"  Or manually:\n"
                f"  Windows:   netstat -ano | findstr :{_port}"
                f"  then  Stop-Process -Id <PID> -Force\n"
                f"  Mac/Linux: lsof -ti :{_port} | xargs kill -9\n",
                flush=True,
            )

    print(
        f"\n  Gideon: http://127.0.0.1:{_port}/\n"
        f"  PID: {os.getpid()}\n",
        flush=True,
    )
    # threaded=True: one slow request (e.g. DB lock) must not block the whole UI.
    # use_reloader=False: prevents Flask from forking a child reloader process,
    #   which doubles the number of Python processes and complicates port cleanup.
    app.run(
        host="0.0.0.0",
        port=_port,
        debug=False,
        use_reloader=False,
        threaded=True,
    )
