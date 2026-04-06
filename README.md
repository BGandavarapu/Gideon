# Gideon — Your Personal Employer

Gideon is a fully automated job application pipeline that scrapes job postings, analyzes them against your resume, rewrites your resume to match each job, and generates ATS-friendly PDFs — all while preserving your writing style.

## Features

- **Smart Job Scraping** — Scrapes LinkedIn job postings based on your resume's industry domain
- **NLP-Powered Analysis** — Extracts required/preferred skills using spaCy + custom taxonomy
- **AI Resume Tailoring** — Rewrites your resume bullets to match each job using Gemini AI
- **Style Preservation** — Extracts and maintains your writing voice, metrics usage, and formatting
- **Multi-Domain Support** — Supports 10 industry domains with automatic resume classification
- **NVIDIA NIM Integration** — Two-stage domain detection (heuristic + NIM) for accurate classification
- **ATS-Friendly PDFs** — Generates clean, parseable PDFs via ReportLab
- **Web Dashboard** — Linear-inspired UI at `http://localhost:5001`

## Tech Stack

- Python 3.x, Flask, SQLAlchemy 2.0 + SQLite
- spaCy (NLP), Google Gemini API (AI rewriting), NVIDIA NIM (classification)
- Selenium + Chrome (scraping), ReportLab (PDF generation)

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt
python -m spacy download en_core_web_sm

# Initialize the database
python main.py init-db

# Start the dashboard
.\scripts\start_app.ps1    # Windows
./scripts/start_app.sh     # Unix

# Open http://localhost:5001
```

## How It Works

1. **Upload or select a resume** — Gideon auto-detects your industry domain
2. **Scrape jobs** — Finds relevant postings matching your domain
3. **Analyze** — Scores each job against your skills and experience
4. **Generate** — AI rewrites your resume for each job, preserving your style
5. **Download** — Export tailored PDFs ready to submit

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `GEMINI_API_KEY` | Google Gemini API for resume rewriting |
| `NVIDIA_API_KEY` | NVIDIA NIM for domain classification |

## License

Private — All rights reserved.
