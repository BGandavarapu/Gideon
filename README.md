# Gideon — Your Personal Employer

Gideon is a job search pipeline I built because applying to jobs is miserable. It scrapes LinkedIn, rewrites your resume for each posting, generates PDFs that match your exact formatting, and now coaches you through interview prep — all driven from a single chat interface.

## What it does

**The pipeline:**
- Scrapes LinkedIn based on whatever industry your resume targets
- Runs NLP on each job description to pull out required vs. preferred skills
- Rewrites your resume bullets using NVIDIA NIM to match the job's language
- Generates PDFs that look exactly like your uploaded resume (same font, same bullet style, same section order)

**The coaching layer:**
- Skill gap analysis — see exactly what's missing before applying
- Skill assessments — 10-question quizzes to test your own knowledge
- Interview prep — browse 15 role-specific questions or run a full mock interview with scored feedback

**The interface:**
- Chat-first — type "scrape jobs", "generate resumes", "mock interview for Stripe" and Gideon handles it
- Dashboard for browsing jobs, scores, and downloading PDFs

## Tech

- Python, Flask, SQLAlchemy 2.0 + SQLite
- spaCy + custom skills taxonomy for NLP
- NVIDIA NIM — two models:
  - `nvidia/nemotron-3-super-120b-a12b` (orchestrator) — chat agent reasoning, interview intros, performance summaries
  - `nvidia/llama-3.3-nemotron-super-49b-v1.5` (worker) — resume rewriting, PDF parsing, question generation, grading, classification
- Selenium + Chrome for scraping, ReportLab for PDF generation
- pdfminer.six for extracting font/bullet/layout from uploaded resumes

## Setup

```bash
pip install -r requirements.txt
python -m spacy download en_core_web_sm

# Set your API key
export NVIDIA_API_KEY=your_key_here   # Unix
$env:NVIDIA_API_KEY="your_key_here"  # Windows

# Initialize DB + seed sample resumes
python main.py init-db

# Start the app (always use the script — it kills stale processes first)
.\scripts\start_app.ps1    # Windows
./scripts/start_app.sh     # Unix
```

Then open `http://localhost:5001`.

## How a session looks

1. Upload your resume PDF or pick one of the 9 sample personas
2. Gideon detects your industry and configures the search queries
3. Hit "Scrape" to pull fresh jobs from LinkedIn
4. Hit "Analyze" to score each job against your skills
5. Hit "Generate" to rewrite and export tailored resumes
6. In chat: "show me interview questions for the Stripe job" or "start a mock interview"

## Resume style preservation

When you upload a PDF, Gideon fingerprints it — font family, bullet character, section header case, name alignment, writing voice, metric density. Every tailored resume and exported PDF is driven by that fingerprint. If your original used Times New Roman with bullet character `●` and centered name, so does every generated version.

## Interview prep

Two modes:

**Browse** — generates 15 questions split into behavioral (1–8) and technical (9–15), each with model answer tips. Good for self-study before an interview.

**Mock** — live simulation where Gideon plays the interviewer, asks one question at a time, gives immediate feedback on each answer (what was strong, what was missing, a specific tip), then wraps up with a score out of 100 and a hiring recommendation.

Sessions persist to the database so you can resume a mock interview if you close the tab.

## Domains

10 supported: Software Engineering, AI/ML, Product Management, Marketing, Data & Analytics, Design, Finance, Sales, Operations. Each has built-in LinkedIn search configs.

"Other" has no search configs — pick a real domain.

## Environment variables

| Variable | What it's for |
|----------|---------------|
| `NVIDIA_API_KEY` | Required — powers all AI (resume rewriting, parsing, interview coaching) |

## License

Private — all rights reserved.
