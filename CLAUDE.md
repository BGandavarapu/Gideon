# Gideon — Your Personal Employer
# This file is read by Claude Code at the start of every session.
# Last updated: April 2026

---

## What This App Does

Gideon is a fully automated job application pipeline
built in Python. It:
1. Scrapes LinkedIn job postings based on the active resume's industry
2. Uses NLP (spaCy + custom taxonomy) to extract required/preferred skills
3. Uses Google Gemini AI to rewrite the user's resume to match each job
4. Preserves the user's writing style during rewriting (voice, metrics,
   sentence length, bullet format, section order)
5. Generates ATS-friendly PDFs via ReportLab
6. Tracks everything in SQLite via SQLAlchemy 2.0
7. Provides a Linear-inspired web dashboard at http://localhost:5001

---

## Run Location
c:\Users\bharg\.cursor\Projects\JobScraper

## How to Start the App
ALWAYS use:
  .\scripts\start_app.ps1

NEVER use:
  python web/app.py

Reason: stale Flask processes accumulate on port 5001 if not
using the startup script. Multiple processes serve stale code
causing hours of confusing debugging. The script kills all
existing processes on port 5001 before starting a fresh one.

Verify the app is running:
  curl http://localhost:5001/api/health
  → shows PID and uptime. One PID only = clean state.

---

## Tech Stack
- Python 3.x
- Flask (web dashboard + REST API)
- SQLAlchemy 2.0 + SQLite (data/jobs.db)
- spaCy en_core_web_sm + custom skills_taxonomy.yaml
- Google Gemini API (gemini-2.5-flash + gemini-2.5-flash-lite)
- ReportLab (PDF generation)
- APScheduler (automated cleanup + daily report only)
- pdfminer.six (PDF text extraction)
- Selenium + Chrome (LinkedIn scraping)

---

## Project Structure

JobScraper/
├── main.py                  # CLI entry point (Click + Rich)
├── config.yaml              # DEPRECATED for search configs
│                            # Only used for scheduler timing now
├── data/
│   ├── jobs.db              # SQLite database
│   ├── settings.json        # ALL user preferences (source of truth)
│   ├── gemini_usage.json    # API quota tracker (dual model)
│   ├── sample_resumes/      # 9 JSON files, one per industry domain
│   └── output/              # Generated PDFs
├── scraper/
│   ├── linkedin_scraper.py  # Selenium scraper (working)
│   └── indeed_scraper.py    # BeautifulSoup (blocked, do not use)
├── database/
│   ├── models.py            # SQLAlchemy ORM models
│   └── database.py          # get_db(), create_tables(), migrations
├── analyzer/
│   ├── keyword_extractor.py # spaCy NLP + taxonomy skill extraction
│   ├── requirement_parser.py # Splits job desc into required/preferred
│   ├── skill_matcher.py     # Synonym matching + normalization
│   ├── scoring.py           # Match score calculation
│   ├── domain_detector.py   # Classifies job/resume into 10 domains
│   └── skills_taxonomy.yaml # Multi-domain skill definitions
├── resume_engine/
│   ├── modifier.py          # Orchestrates full tailoring pipeline
│   ├── gemini_rewriter.py   # Gemini API calls (dual model routing)
│   ├── style_extractor.py   # Extracts style fingerprint from resume
│   ├── pdf_parser.py        # PDF upload → text → Gemini parse
│   │                        # Also contains ResumeClassifier +
│   │                        # NotAResumeError
│   └── validator.py         # Checks for hallucinated skills
├── pdf_generator/
│   └── generator.py         # ReportLab PDF templates (ats, classic)
├── scheduler/
│   ├── tasks.py             # scrape/analyze/generate/cleanup/report
│   └── scheduler.py         # APScheduler setup
├── web/
│   ├── app.py               # Flask app + all API routes
│   ├── settings_manager.py  # SettingsManager — reads/writes settings.json
│   ├── templates/           # Jinja2 HTML templates
│   └── static/              # CSS + JS
│       ├── js/main.js       # All frontend JS
│       └── css/style.css
├── tests/                   # 737 passing, 4 skipped (Selenium)
├── scripts/
│   ├── start_app.ps1        # Windows: kill stale + start Flask
│   └── start_app.sh         # Unix: kill stale + start Flask
└── logs/
    ├── app.log              # Rotating, 10MB/5 backups
    └── scheduler.log

---

## THE MOST IMPORTANT CONCEPT: Resume Drives Everything

The active resume is the single source of truth for the entire pipeline.

  User picks resume (sample or own)
        ↓
  App reads resume.domain + resume.domains (multi-domain support)
        ↓
  SettingsManager.get_industry_search_configs_for_domains([domains])
  returns built-in search queries, location overridden by preferred_location
        ↓
  scrape_jobs_task() uses those queries (capped at 50 new jobs/run)
  Jobs are tagged with domain
        ↓
  analyze_new_jobs_task() extracts domain-relevant skills
  Sets analyzed_with_resume_id = active resume id
        ↓
  generate_resumes_task() picks the resume assigned to that domain
  Gemini rewrites bullets using the resume's style fingerprint
        ↓
  PDF exported in user's original style

Everything flows from ONE choice: which resume is active.
Never break this chain.

---

## Domain System (10 Domains)

DOMAINS = {
  "software_engineering": "Software Engineering",
  "ai_ml":                "AI / Machine Learning",
  "product_management":   "Product Management",
  "marketing":            "Marketing",
  "data_analytics":       "Data & Analytics",
  "design":               "Design (UX/UI)",
  "finance":              "Finance & Accounting",
  "sales":                "Sales",
  "operations":           "Operations",
  "other":                "Other"  ← WARNING: no search configs
}

"other" domain = no industry_search_configs = 0 jobs scraped.
If a user's resume is classified as "other", show a warning
and let them override the domain manually.

Multi-domain support: MasterResume has both:
  domain  (String) — primary domain, for backwards compat
  domains (JSON list) — all selected domains

PATCH /api/resume/<id>/domain accepts {"domains": ["x","y"]} for multi.
get_active_domains() returns the list; falls back to [domain] if unset.

Each of the 9 named domains has 3 built-in industry search
configs (read-only) in SettingsManager.INDUSTRY_SEARCH_CONFIGS.

---

## Settings Architecture

data/settings.json is the ONLY source of truth for user prefs.
config.yaml is DEPRECATED for search configs (has a comment saying so).

settings.json schema:
{
  "automation": {
    "scrape": {"mode": "manual", "schedule": "09:00"},
    "generate": {"mode": "manual", "schedule": "10:00"}
  },
  "resume_mode": "sample",        // "sample" or "own"
  "preferred_location": "",       // overrides hardcoded locations in
                                  // INDUSTRY_SEARCH_CONFIGS when non-empty
  "search_configs": [],           // user-added queries
  "domain_resumes": {             // domain → resume_id mapping
    "software_engineering": null,
    "marketing": null, ...
  }
}

SettingsManager methods to know:
  get_active_domain()            → primary domain of active MasterResume
  get_active_domains()           → list of all domains (multi-domain)
  get_industry_search_configs_for_domains(domains)
                                 → built-in configs with preferred_location
                                   applied if set
  get_search_configs()           → user-added configs
  get_resume_mode()              → "sample" or "own"
  set_resume_mode(mode)          → saves to settings.json
  get_domain_resume(domain)      → resume_id for domain
  set_domain_resume(domain, id)  → assigns resume to domain
  get_preferred_location()       → user's location preference (str)
  set_preferred_location(loc)    → saves preferred location
  clear_legacy_search_configs()  → removes old SE default configs

---

## Database Models

### Job
id, job_title, company_name, job_description (Text),
required_skills (JSON list), preferred_skills (JSON list),
salary_range, application_url (UNIQUE — dedup key),
source, location, date_posted, date_scraped, domain,
analyzed_with_resume_id (FK → master_resumes.id, nullable, SET NULL),
status: new → analyzed → applied/archived

IMPORTANT: "new" and "analyzed" are pipeline-owned states.
The user may only set status to "applied" or "archived" via the API.
PATCH /api/jobs/<id>/status only accepts {"status": "applied"} or "archived".

analyzed_with_resume_id: set by analyze_new_jobs_task() to track which
resume was active during analysis. NULL = old job (pre-feature), always
allowed to generate. Non-null mismatch with active resume → 409 on generate.

### MasterResume
id, name, content (JSON — full resume structure),
is_active (only one True at a time), is_sample (Bool),
domain (String — primary), domains (JSON list — all selected),
style_fingerprint (JSON), created_at

content schema:
{
  "personal_info": {name, email, phone, location,
                    linkedin, github, website},
  "professional_summary": "string",
  "work_experience": [{title, company, location,
                       start_date, end_date, bullets:[]}],
  "skills": ["string"],
  "education": [{degree, institution, graduation_year, gpa}],
  "certifications": ["string"],
  "projects": [{name, description, technologies:[]}]
}

### TailoredResume
id, job_id (FK), master_resume_id (FK),
tailored_content (JSON — same shape as content),
match_score (Float 0-100), generated_at, pdf_path

### Application
id, job_id, tailored_resume_id, application_date,
status (applied/interviewing/offered/rejected/withdrawn),
notes, created_at, updated_at
(Currently empty — placeholder, not yet implemented)

---

## Scraping Rules

- SCRAPE_LIMIT = 50 new jobs per run (defined in scheduler/tasks.py)
- Loop over search configs; break when new_jobs >= SCRAPE_LIMIT
- result.data includes "capped": True/False
- Each config has its own max_results (default 20 per config)
- Location per config comes from preferred_location setting if set,
  otherwise from the hardcoded INDUSTRY_SEARCH_CONFIGS default

---

## Resume Mismatch Guard

When a job is analyzed, analyzed_with_resume_id is stamped with the
active resume's id. When "Generate Tailored Resume" is clicked:

  analyzed_with_resume_id is NULL → allow (old job, backwards compat)
  analyzed_with_resume_id == active resume id → allow
  analyzed_with_resume_id != active resume id → 409 resume_mismatch

409 response body:
  { "error": "resume_mismatch",
    "analyzed_with_resume_name": "...",
    "analyzed_with_domain": "...",
    "active_resume_name": "...",
    "active_domain": "..." }

Frontend shows mismatch card with "Re-analyze with current resume" button.
POST /api/jobs/<id>/reanalyze re-runs analysis with current active resume,
updates analyzed_with_resume_id, does NOT delete existing TailoredResume rows.

Jobs with status == "new" (unanalyzed) return 422 not_analyzed on generate.
Frontend shows "Not yet analyzed" card instead of Generate button for new jobs.

---

## Scoring Engine

total = (required_skills_match  × 0.40)
      + (preferred_skills_match × 0.30)  ← redistributed to
      + (experience_match       × 0.20)    required (0.70) if
      + (education_match        × 0.10)    no preferred skills
      + bonus (max +5)

ScoreResult has .total_score (float) and .score_breakdown (dict).
Use ScoringEngine().score(job, master) — NOT .calculate_score().
Scores are NOT recalculated for old jobs — only new analyses.

---

## Gemini Integration

PRIMARY MODEL:   gemini-2.5-flash
  Used for: professional summary rewriting, PDF parsing,
            document classification, one-time complex tasks
  Free tier: 10 RPM, 250 RPD

BULK MODEL:      gemini-2.5-flash-lite
  Used for: bullet point rewrites (many per resume),
            simple classification tasks
  Free tier: 15 RPM, 1000 RPD

Quota tracked in: data/gemini_usage.json
  {"primary": {"daily_count": X, "daily_limit": 250, ...},
   "bulk":    {"daily_count": Y, "daily_limit": 1000, ...}}

RateLimiter handles sliding window RPM + daily RPD.
QuotaExceededError on bulk → fallback to primary.
QuotaExceededError on primary → raise to caller.

---

## Style Fingerprint System

Extracted ONCE on resume upload by StyleExtractor.
Stored in MasterResume.style_fingerprint.
Passed to GeminiRewriter as HARD CONSTRAINTS.

Five dimensions:
1. voice: "first_person" | "third_person" | "no_pronouns"
2. sentence_structure: "punchy"(≤12w) | "moderate" | "detailed"
3. metric_usage: "heavy"(>40%) | "moderate" | "light"
4. structure: ordered list of section keys
5. format: {bullet_char, capitalization, trailing_period}

_build_style_constraints() converts fingerprint to plain
English instructions injected into every Gemini prompt.

---

## Non-Resume Detection (Two-Stage)

Stage 1 — Heuristics (no API call):
  Checks RESUME_HEADERS, NON_RESUME_SIGNALS, email, phone
  → confidence formula → verdict: resume/not_resume/inconclusive

Stage 2 — Gemini (only for inconclusive):
  Sends first 2000 chars to gemini-2.5-flash-lite
  Returns {is_resume, confidence, document_type, reason}

If both inconclusive → default to not_resume.
Raises NotAResumeError → HTTP 422 with document_type + reason.

---

## Sample Resumes (9 personas, auto-seeded on init-db)

software_engineering  → Alex Rivera (Python/Django/AWS)
ai_ml                → Priya Sharma (PyTorch/LLMs/MLflow)
product_management   → Jordan Kim (Jira/OKRs/Figma)
marketing            → Sofia Martinez (SEO/HubSpot/Google Ads)
data_analytics       → Marcus Chen (SQL/Tableau/dbt)
design               → Aisha Okafor (Figma/UX Research)
finance              → Ryan Patel (DCF/Excel/Bloomberg)
sales                → Emma Thompson (Salesforce/B2B SaaS)
operations           → David Nakamura (Six Sigma/Asana)

Seeding is idempotent — safe to run init-db multiple times.
Each sample has is_sample=True, domain set, style_fingerprint.

---

## Scheduler

Only TWO tasks run automatically:
  cleanup_old_jobs_task → weekly Sunday midnight
  daily_report_task     → daily 20:00

THREE tasks are MANUAL (dashboard buttons only):
  scrape_jobs_task      → POST /api/run/scrape
  analyze_new_jobs_task → POST /api/run/analyze
  generate_resumes_task → POST /api/run/generate

Start scheduler: python main.py start-scheduler --daemon
Scheduler runs independently of Flask.

---

## Key API Routes

GET  /api/health                      → PID + uptime
GET  /api/active-context              → active resume + configs
GET  /api/stats                       → dashboard numbers
GET  /api/resume/mode                 → current mode + resumes
PATCH /api/resume/mode                → switch sample/own + domain
POST /api/resume/upload               → PDF upload pipeline
PATCH /api/resume/<id>/domain         → override domain(s); accepts
                                        {"domain":"x"} or {"domains":["x","y"]}
GET  /api/sample-resume/<domain>      → preview sample resume
GET  /api/search-configs              → list user search configs
POST /api/search-configs              → add search config
PATCH /api/search-configs/<id>        → update config
DELETE /api/search-configs/<id>       → remove config
GET  /api/domain-resumes              → all domain→resume mappings
PATCH /api/domain-resumes/<domain>    → assign resume to domain
GET  /api/settings                    → current settings.json
PATCH /api/settings/automation/<task> → update mode/schedule
GET  /api/settings/location           → get preferred_location
PATCH /api/settings/location          → set preferred_location
POST /api/run/scrape                  → trigger scrape (background)
POST /api/run/analyze                 → trigger analyze (background)
POST /api/run/generate                → trigger generate (background)
GET  /api/run/status                  → is any task running
GET  /api/run/last-run                → last completion timestamps
GET  /api/jobs                        → paginated job list
GET  /api/jobs/<id>                   → job detail + score breakdown
                                        includes analyzed_with_resume_id/name
PATCH /api/jobs/<id>/status           → user status update
                                        ONLY accepts "applied" or "archived"
POST /api/jobs/<id>/reanalyze         → re-analyze with current resume
POST /api/generate-resume             → generate tailored resume
                                        409 = resume mismatch
                                        422 = job not analyzed yet
POST /api/export-pdf                  → generate PDF for resume
GET  /api/download-pdf/<id>           → download PDF file
GET  /api/resumes/master              → list master resumes
GET  /api/resume/active               → active resume summary

---

## Frontend Architecture

Single-page feel using vanilla JS (no framework).
All JS lives in web/static/js/main.js EXCEPT settings page JS
which is embedded inline in web/templates/settings.html.

Key functions in main.js:
  loadMasterResumes()     → fetches + renders resume cards
  activateResume(card)    → switches active resume via API
  window.activateResume   → must be on window scope (onclick)
  window.saveDomains()    → saves multi-domain selection via checkbox UI
  downloadPDF(resumeId)   → fetch→blob→download (no navigation)
  checkActiveBanner()     → updates dashboard context banner
  loadModeState()         → refreshes sample/own card state
  openJobPanel(jobId)     → loads job detail into side panel
  generateResume(jobId)   → POST generate; handles 409/422 responses
  reanalyzeJob(jobId)     → POST reanalyze; clears mismatch card
  updateJobStatus(jobId, status, btn) → PATCH status (applied/archived only)

Job panel status display: read-only badge + "Mark as Applied" / "Archive"
buttons. No status dropdown. "new" jobs show "Not yet analyzed" card
instead of Generate button.

Cache busting: main.js loaded with ?v=20260404c in base.html.
If JS changes aren't reflecting → bump the version string.

---

## DB Migration Pattern

ALL column additions use this idempotent pattern in database.py:

  _migrate_add_column_if_missing(
      'table_name', 'column_name', 'TYPE'
  )

This checks PRAGMA table_info before ALTER TABLE.
Safe to run multiple times. Called in create_tables().
Never use raw ALTER TABLE without this wrapper.

Current migrations beyond initial schema:
  master_resumes.domains          JSON
  jobs.analyzed_with_resume_id    INTEGER (FK → master_resumes.id)

---

## Current Known Issues / Active Work

1. %PDF-1.4 BAD UPLOAD
   A corrupted resume upload stored raw PDF bytes as the name
   Still in DB, is_sample=False, domain="other"
   Should be deleted: find by name LIKE '%PDF%' and remove

2. APPLICATION TRACKING
   Application model exists but is empty placeholder
   The Applications page shows 0 and has no functionality

---

## Test Suite

Run: python -m pytest tests/ -q
Baseline: 737 passed, 4 skipped, 0 failed

4 skipped = Selenium tests requiring live Chrome browser
These are EXPECTED to skip — do not try to fix them.

Test files:
  tests/test_scraper.py           # LinkedIn scraper
  tests/test_database.py          # ORM models
  tests/test_analyzer.py          # NLP + scoring
  tests/test_resume_engine.py     # Gemini (mocked)
  tests/test_pdf_generator.py     # PDF output
  tests/test_scheduler.py         # Task scheduling
  tests/test_settings.py          # SettingsManager
  tests/test_resume_mode.py       # Sample/own mode
  tests/test_pdf_parser.py        # Upload + classification
  tests/test_style_extractor.py   # Style fingerprint
  tests/test_domain_detector.py   # Domain classification + multi-domain API
  tests/test_resume_driven.py     # Resume-driven scraping
  tests/test_api_testclient.py    # API endpoint integration
  tests/test_resume_mismatch.py   # Resume mismatch guard (10 tests)

---

## Rules — Never Break These

1. Always use .\scripts\start_app.ps1 to start Flask
2. Run python -m pytest tests/ -q after every change
3. All settings through SettingsManager + settings.json only
4. DB schema changes need _migrate_add_column_if_missing
5. Gemini bulk tasks → flash-lite, complex tasks → flash
6. window.activateResume must be on window scope for onclick
7. main.js cache bust version string must be bumped on JS changes
8. Never edit config.yaml search_configs (deprecated)
9. scrape/analyze/generate are MANUAL — never auto-schedule them
10. get_active_domain() must call db.expire_all() for fresh reads
11. Job status "new" and "analyzed" are pipeline-only — API only
    accepts "applied" or "archived" from users
12. analyzed_with_resume_id = NULL means old job → always allow generate
13. SCRAPE_LIMIT = 50 new jobs per run (in scheduler/tasks.py)

---

## Development Workflow

Claude Code handles all implementation directly in this project.
Implementation, debugging, and verification all happen here.
Always verify with the test suite before moving on.
