# Gideon — Your Personal Employer
# This file is read by Claude Code at the start of every session.
# Last updated: April 14, 2026

---

## What This App Does

Gideon is a fully automated job application pipeline
built in Python. It:
1. Scrapes LinkedIn job postings based on the active resume's industry
2. Uses NLP (spaCy + custom taxonomy) to extract required/preferred skills
3. Uses NVIDIA NIM AI to rewrite the user's resume to match each job
4. Preserves the user's writing style during rewriting (voice, metrics,
   sentence length, bullet format, section order, font family, header style)
5. Generates ATS-friendly PDFs via ReportLab, matching the uploaded resume's
   exact visual style (font, bullets, section headers, categorised skills)
6. Tracks everything in SQLite via SQLAlchemy 2.0
7. Provides a Linear-inspired web dashboard at http://localhost:5001
8. Offers a conversational Chat interface (GideonAgent) at http://localhost:5001/
   where users can drive the full pipeline via natural language

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
- NVIDIA NIM API (nvidia/llama-3.3-nemotron-super-49b-v1.5) — ALL AI tasks
- ReportLab (PDF generation — fingerprint-driven, no hard-coded Helvetica)
- APScheduler (automated cleanup + daily report only)
- pdfminer.six (PDF text extraction, font detection, bullet char detection,
                 name alignment detection — all via LTChar/layout tree)
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
│   ├── nvidia_usage.json    # API quota tracker
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
│   │                        # Preserves font_family, section_header_style,
│   │                        # name_alignment into tailored content
│   │                        # Dict-aware skills promotion + reordering
│   ├── rewriter.py          # PRIMARY: Rewriter class — NVIDIA NIM API calls
│   │                        # for resume rewriting (bullet + summary)
│   │                        # _build_style_constraints(), batch_rewrite_bullets()
│   │                        # (gemini_rewriter.py deleted — was a backwards-compat shim)
│   ├── style_extractor.py   # Extracts style fingerprint from resume
│   │                        # Captures font_family, section_header_style,
│   │                        # name_alignment, detected_bullet_char
│   │                        # Normalises NIM display-name section_order
│   │                        # to snake_case keys
│   ├── pdf_parser.py        # PDF upload → text → NIM parse
│   │                        # _detect_font_family: recursive LTChar walk
│   │                        # _detect_bullet_char: raw PDF line scanning
│   │                        # _detect_name_alignment: layout x-position
│   │                        # Also contains ResumeClassifier +
│   │                        # NotAResumeError
│   └── validator.py         # Checks for hallucinated skills
├── pdf_generator/
│   ├── generator.py         # PDFGenerator — accepts style_fingerprint kwarg
│   ├── base_template.py     # BasePDFTemplate — _resolve_style(fingerprint)
│   │                        # maps fingerprint → body_font/bold_font/bullet_char
│   ├── styles.py            # SECTION_TITLES_UPPER / TITLE_COLON / TITLE
│   │                        # (SECTION_TITLES = alias for UPPER, compat)
│   └── templates/
│       ├── classic.py       # Fingerprint-driven: font, bullet, header case
│       └── ats_optimized.py # Same — no more hard-coded Helvetica or "-"
├── scheduler/
│   ├── tasks.py             # scrape/analyze/generate/cleanup/report
│   └── scheduler.py         # APScheduler setup
├── web/
│   ├── app.py               # Flask app + all API routes
│   │                        # / (root) → chat.html (Chat page is the home)
│   │                        # /dashboard → dashboard page
│   │                        # /api/export-pdf loads master style_fingerprint
│   │                        # /api/chat → GideonAgent.chat()
│   ├── agent.py             # GideonAgent — conversational AI agent
│   │                        # Uses NIM function-calling to drive pipeline
│   │                        # Tools: scrape_jobs, analyze_jobs, generate_resumes,
│   │                        #        switch_resume, get_active_context, get_jobs
│   │                        # Calls Flask routes internally via test_client()
│   │                        # Session TTL: 30 min; max iterations: 5
│   ├── settings_manager.py  # SettingsManager — reads/writes settings.json
│   ├── templates/           # Jinja2 HTML templates
│   │   └── chat.html        # Chat UI (bubbles, action chips, loading dots)
│   └── static/              # CSS + JS
│       ├── js/main.js       # All frontend JS
│       └── css/style.css    # Includes .chat-container, .chat-bubble, etc.
├── tests/                   # 788 passing, 0 failed (baseline; chat agent not yet covered)
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
  NIM rewrites bullets using the resume's style fingerprint
        ↓
  PDF exported in user's original style (font, bullets, section headers,
  section order, categorised skills — all preserved from upload)

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
                       dates, bullets:[]}],
  "skills": ["string"] OR {category: [items]} for categorised skills,
  "education": [{degree, school/institution, year, gpa,
                 location, coursework}],
  "certifications": ["string" or {name, issuer, year}],
  "projects": [{name, bullets:[], date, tech:[],
                description (fallback if no bullets)}],
  "font_family": "Times"|"Helvetica"|"Courier",
  "detected_bullet_char": "●"|"•"|"-"|"none" (raw PDF detection),
  "name_alignment": "center"|"left",
  "section_header_style": {"case": "title_colon"|"upper"|"title",
                            "rule": true|false},
  "section_order": ["education", "work_experience", ...]
                   (NIM returns display names; StyleExtractor normalises)
}

### TailoredResume
id, job_id (FK), master_resume_id (FK),
tailored_content (JSON — same shape as content, includes font_family,
                  section_header_style, name_alignment copied from master),
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

## NVIDIA NIM Integration

MODEL:   nvidia/llama-3.3-nemotron-super-49b-v1.5
  Used for: ALL AI tasks — resume rewriting, PDF parsing,
            document classification, domain detection
  Endpoint: https://integrate.api.nvidia.com/v1
  SDK: openai (OpenAI-compatible API)
  Quota: 60 RPM, 5000 RPD

Quota tracked in: data/nvidia_usage.json
  {"nvidia": {"date": "YYYY-MM-DD", "calls": N, "tokens_estimated": N}}

RateLimiter handles sliding window RPM + daily RPD.
QuotaExceededError → raise to caller (original text returned).

NOTE: All AI tasks use NVIDIA NIM exclusively. Do not add Google Gemini calls.
gemini_rewriter.py has been deleted — import Rewriter from resume_engine.rewriter directly.

---

## Style Fingerprint System

Extracted ONCE on resume upload by StyleExtractor.
Stored in MasterResume.style_fingerprint.
Passed to Rewriter (NIM) as HARD CONSTRAINTS and to PDFGenerator for rendering.

Eight dimensions (full fingerprint schema):
1. voice: "first_person" | "third_person" | "no_pronouns"
2. sentence_structure: {style: "punchy"(≤12w)|"moderate"|"detailed",
                        avg_word_count, min_word_count, max_word_count}
3. metric_usage: {density: "heavy"(>40%)|"moderate"|"light",
                  ratio, bullets_with_metrics, total_bullets}
4. structure: ordered list of non-empty section keys (from section_order
              parser key or canonical fallback). NIM returns display names
              ("Education", "Work Experience"); StyleExtractor normalises
              to snake_case keys via _DISPLAY_TO_KEY map.
5. format: {bullet_char, capitalization: "upper"|"lower", trailing_period}
   bullet_char: detected from raw PDF text via _detect_bullet_char(),
   supports ● • ○ ◦ ■ □ ▪ ▸ ► ➤ - * — –. Falls back to detected_bullet_char
   on content dict when NIM-parsed bullets have no leading char.
6. font_family: "Times" | "Helvetica" | "Courier"
   (detected from PDF via pdfminer LTChar recursive walk,
   stored on content + fingerprint)
7. section_header_style: {"case": "title_colon"|"upper"|"title",
                          "rule": true|false}
   (parsed from NIM output or defaulted to title_colon/true)
8. name_alignment: "center" | "left"
   (detected from PDF via pdfminer layout x-position of first text box,
   compared to page center with 15% tolerance)

_build_style_constraints() converts fingerprint to plain
English instructions injected into every NIM prompt.

BasePDFTemplate._resolve_style(fingerprint) maps fingerprint →
  {body_font, bold_font, italic_font, bold_italic_font,
   bullet_char, section_header_case, section_header_rule,
   name_alignment}
using ReportLab built-in family scheme:
  Times     → Times-Roman / Times-Bold / Times-Italic / Times-BoldItalic
  Helvetica → Helvetica / Helvetica-Bold / Helvetica-Oblique / ...
  Courier   → Courier / Courier-Bold / ...

---

## PDF Style Preservation Pipeline

When a user uploads their own resume, the full visual style is captured
and preserved through tailoring:

  PDF upload
    → pdf_parser._detect_font_family(pdf_bytes)  [recursive LTChar walk]
    → pdf_parser._detect_bullet_char(pdf_bytes)   [raw text line scanning]
    → pdf_parser._detect_name_alignment(pdf_bytes) [layout x-position]
    → NIM prompt extracts: section_order, section_header_style,
        categorised skills dict, project bullets, full date ranges,
        coursework, linkedin/github URLs
    → result stores font_family, detected_bullet_char, name_alignment
    → StyleExtractor.extract() captures all into style_fingerprint
      (normalises NIM display-name section_order to snake_case keys)
    ↓
  Tailoring (modifier.py)
    → _promote_keywords_to_skills() is dict-aware (preserves categories)
    → _reorder_categorised_skills() sorts within each category
    → font_family, section_header_style, name_alignment copied into
      raw_tailored_content
    → _enforce_structure_order() reorders dict keys to match fingerprint
    ↓
  PDF export
    → /api/export-pdf loads master.style_fingerprint
    → PDFGenerator.generate(..., style_fingerprint=fingerprint)
    → Template.__init__(style_fingerprint=...) calls _resolve_style()
    → All fonts, bullets, section titles, name centering, skills rendering
      driven by self._style dict — zero hard-coded values in templates

Skills dict rendering:
  If skills is a dict (categorised), each category rendered as:
    "Programming Languages: " (bold) + "Python, Go, ..." (regular)
  on its own line. If skills is a list, rendered as comma-separated.

Section titles pick from:
  SECTION_TITLES_UPPER      → "WORK EXPERIENCE"
  SECTION_TITLES_TITLE_COLON → "Work Experience:"  (default for uploaded PDFs)
  SECTION_TITLES_TITLE       → "Work Experience"
  based on section_header_case in fingerprint.

---

## Chat Agent (GideonAgent)

`web/agent.py` — `GideonAgent` wraps NVIDIA NIM with native function calling
to let users drive the entire pipeline through natural language.

### Architecture
- Uses OpenAI SDK pointed at `https://integrate.api.nvidia.com/v1`
- Model: `nvidia/llama-3.3-nemotron-super-49b-v1.5` (same as all other AI tasks)
- Calls existing Flask API endpoints via `app.test_client()` — no logic duplication
- `_MAX_AGENT_ITERATIONS = 6` prevents infinite tool-call loops
- Session TTL: `_SESSION_TTL = 1800s` (30 min); sessions keyed by UUID
- `GideonAgent._sessions` is a **class attribute** (shared across instances)
- Module-level singleton `gideon` created via `init_agent(app)`; init failures
  are caught so a broken agent never prevents Flask from booting

### Available Tools
| Tool | What it does |
|---|---|
| `scrape_jobs` | POST /api/run/scrape, then polls /api/run/status until done |
| `analyze_jobs` | POST /api/run/analyze, same polling pattern |
| `generate_resumes` | POST /api/run/generate, same polling pattern |
| `switch_resume` | PATCH /api/resume/mode (mode + optional domain) |
| `get_active_context` | GET /api/active-context |
| `get_jobs` | GET /api/jobs?status=X&limit=N |

### Polling Pattern for Pipeline Tasks
`_run_and_wait()` starts the task then polls `/api/run/status` every 5s.
When the flag clears it fetches stats + last-run and returns a summary dict.
Per-task timeouts: scrape 600s, analyze 300s, generate 600s.

### API Routes
`POST /api/chat`
  Body:    `{"message": "...", "session_id": "<uuid or null>"}`
  Returns: `{"response": "...", "actions_taken": [...], "tool_calls": [...], "session_id": "..."}`
  503 if NVIDIA_API_KEY not set.

`POST /api/chat/reset`
  Body:    `{"session_id": "<uuid>"}`
  Returns: `{"status": "reset", "session_id": "..."}`
  Drops the server-side session so the next turn starts fresh.

### Chat UI
`web/templates/chat.html` — **standalone page** (does NOT extend base.html);
has its own `<html>` document and top-nav markup
  - user/agent chat bubbles; action chips (green checkmarks) for completed actions
  - Loading indicator: 3 animated dots
  - Session ID persisted in JS `sessionId` variable
  - XSS-safe: `escapeHtml()` used for user input before inserting into DOM

### Navigation
`/` (root) → `chat.html` (Chat is the landing page)
`/dashboard` → dashboard page (was previously `/`)
`Chat` link is the first item in the top nav.

---

## Non-Resume Detection (Two-Stage)

Stage 1 — Heuristics (no API call):
  Checks RESUME_HEADERS, NON_RESUME_SIGNALS, email, phone
  → confidence formula → verdict: resume/not_resume/inconclusive

Stage 2 — NIM (only for inconclusive):
  Sends first 2000 chars to NIM model
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
Sample resumes use flat skills lists (not dicts) — both code paths must work.

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

GET  /                                → Chat page (landing page)
GET  /dashboard                       → Dashboard page
POST /api/chat                        → GideonAgent.chat()
                                        body: {message, session_id}
                                        503 if NVIDIA_API_KEY missing
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
                                        loads master style_fingerprint
GET  /api/download-pdf/<id>           → download PDF file
GET  /api/resumes/master              → list master resumes
GET  /api/resume/active               → active resume summary

---

## Frontend Architecture

Single-page feel using vanilla JS (no framework).
All JS lives in web/static/js/main.js EXCEPT:
  - Settings page JS → embedded inline in web/templates/settings.html
  - Chat page JS → embedded inline in web/templates/chat.html

Navigation tabs (in base.html top nav):
  Chat → / (first tab, landing page)
  Dashboard → /dashboard
  Jobs → /jobs
  Resumes → /resumes
  Settings → /settings

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

3. CHAT AGENT — NO TEST COVERAGE
   GideonAgent (web/agent.py) has no unit tests yet.
   /api/chat endpoint is untested.
   Tests should mock NIM client + flask test_client tool calls.

---

## Test Suite

Run: python -m pytest tests/ -q
Baseline: 788 passed, 0 failed

Test files and coverage:

  tests/test_analyzer.py          (123 tests) — KeywordExtractor, RequirementParser,
    SkillMatcher, ScoringEngine, edge cases, split buckets, weight redistribution

  tests/test_api_testclient.py    (11 tests)  — Manual task API endpoints,
    non-blocking response, running-flag lifecycle, dashboard controls

  tests/test_database.py          (37 tests)  — DatabaseManager, ORM models (Job,
    MasterResume, TailoredResume, Application), cascade deletes, session context

  tests/test_db_handler.py        (39 tests)  — save_job_to_db upsert, batch saves,
    skill merging, HTML cleaning, relative date extraction

  tests/test_domain_detector.py   (83 tests)  — Domain detection from text/resume/job,
    search configs CRUD, domain-resume mapping API, multi-domain API,
    delete resume API, expanded keyword coverage, scraper/generator task domain logic

  tests/test_e2e_pipeline.py      (15 tests)  — Resume detection two-stage pipeline,
    tailoring pipeline (API calls, skills preservation, bullets), score breakdown storage

  tests/test_pdf_generator.py     (50 tests)  — PDFGenerator template selection,
    ATS template (full/minimal/multipage/skills dict/certs/projects),
    Classic template, BasePDFTemplate utilities (wrap_text, check_page_break)

  tests/test_pdf_parser.py        (50 tests)  — ResumeClassifier heuristic + NIM,
    two-stage pipeline, multi-format parsing (PDF/DOCX/TXT), NIM structured parsing,
    upload API classification (422 for non-resume, 200 for valid)

  tests/test_resume_driven.py     (24 tests)  — Sample resume seeding, industry search
    configs, sample resume API, patch mode with domain, active context API,
    scraping resume-driven flow

  tests/test_resume_engine.py     (69 tests)  — RateLimiter, Rewriter (NIM/Nemotron),
    ContentValidator, ResumeModifier (tailor, skills, projects, metrics)

  tests/test_resume_mismatch.py   (10 tests)  — analyzed_with_resume_id column,
    409 mismatch guard, reanalyze endpoint

  tests/test_resume_mode.py       (34 tests)  — Settings resume mode, is_sample column,
    resume mode API (upload, switch, activate, deactivate)

  tests/test_scheduler.py         (49 tests)  — TaskResult, NotificationService,
    SchedulerManager, scrape/analyze/generate/cleanup/report tasks,
    manual vs auto task separation, API routes

  tests/test_scoring_integration.py (24 tests) — Synonym matching accuracy, weight
    redistribution, skill overlap, bonus cap, cross-domain scoring

  tests/test_scraper.py           (54 tests)  — JobPosting, ScrapingConfig,
    text utilities, Indeed scraper JSON-LD, base scraper orchestration

  tests/test_settings.py          (41 tests)  — SettingsManager defaults/persistence/
    validation, settings API, scheduler registration, dashboard badge

  tests/test_style_extractor.py   (46 tests)  — StyleExtractor voice/structure/metrics/
    format detection, style constraints injection, structure order enforcement,
    upload stores style fingerprint in DB

---

## Rules — Never Break These

1. Always use .\scripts\start_app.ps1 to start Flask
2. Run python -m pytest tests/ -q after every change
3. All settings through SettingsManager + settings.json only
4. DB schema changes need _migrate_add_column_if_missing
5. NIM bulk tasks → use NIM lite model, complex tasks → full model
6. window.activateResume must be on window scope for onclick
7. main.js cache bust version string must be bumped on JS changes
8. Never edit config.yaml search_configs (deprecated)
9. scrape/analyze/generate are MANUAL — never auto-schedule them
10. get_active_domain() must call db.expire_all() for fresh reads
11. Job status "new" and "analyzed" are pipeline-only — API only
    accepts "applied" or "archived" from users
12. analyzed_with_resume_id = NULL means old job → always allow generate
13. SCRAPE_LIMIT = 50 new jobs per run (in scheduler/tasks.py)
14. Skills can be a list OR a dict (categorised) — both must work everywhere:
    modifier._promote_keywords_to_skills, reorder_skills, template renderers
15. Templates are fingerprint-driven — never hard-code "Helvetica" or "-" bullet
    in classic.py or ats_optimized.py; use self._style["body_font"] etc.
16. font_family + section_header_style + name_alignment must flow:
    parser → content dict → style_fingerprint → tailored_content →
    PDFGenerator.generate(style_fingerprint)
17. The codebase uses NVIDIA NIM for all AI. Do not add Google Gemini calls.
    gemini_rewriter.py is deleted. Use resume_engine.rewriter.Rewriter directly.
18. _detect_font_family uses recursive _count_char() to walk the pdfminer
    layout tree — LTTextBox children can be LTTextLine or LTChar directly.
    Never iterate `for char in text_line` without checking hasattr(__iter__).
19. NIM section_order returns display names ("Education", "Work Experience"),
    not snake_case keys. StyleExtractor._detect_structure normalises via
    _DISPLAY_TO_KEY map. Always normalise before comparing to content keys.
20. detected_bullet_char (from raw PDF) overrides _detect_format result
    when NIM strips the leading bullet char from parsed bullet text.
21. GideonAgent calls Flask routes internally via app.test_client() — never
    make HTTP requests to localhost or spawn a second process. The test_client
    is the contract; keep tool implementations in _execute_tool() thin.
22. Chat JS is inline in chat.html (not in main.js) — do not move it to main.js.
    Keep settings.html JS inline too. Only pipeline/dashboard JS belongs in main.js.
23. The Rewriter class lives in resume_engine/rewriter.py.
    gemini_rewriter.py has been deleted — all imports must use rewriter.py directly.

---

## Development Workflow

Claude Code handles all implementation directly in this project.
Implementation, debugging, and verification all happen here.
Always verify with the test suite before moving on.
