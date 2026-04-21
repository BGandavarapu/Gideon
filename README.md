# Gideon

A job search pipeline that scrapes LinkedIn, rewrites your resume for each posting, generates PDFs that match your exact formatting, and coaches you through interview prep. Everything runs from a chat interface.

## What it does

Scrapes LinkedIn based on your resume's industry, extracts required and preferred skills from each job description, then rewrites your resume bullets to match using NVIDIA NIM. The exported PDF looks identical to what you uploaded, same font, same bullets, same section order.

Beyond the pipeline there's a coaching layer: skill gap analysis, 10-question skill assessments, and interview prep with either a browsable question list or a full mock interview with per-answer feedback and a final score.

## Tech

- Python, Flask, SQLAlchemy 2.0, SQLite
- spaCy with a custom skills taxonomy for NLP
- NVIDIA NIM (nemotron-super-120b for the chat agent, llama-3.3-nemotron-49b for structured tasks)
- Selenium and Chrome for scraping
- ReportLab for PDFs, pdfminer.six for reading uploaded resume layout

## Setup

```bash
pip install -r requirements.txt
python -m spacy download en_core_web_sm

export NVIDIA_API_KEY=your_key_here   # Unix
$env:NVIDIA_API_KEY="your_key_here"  # Windows

python main.py init-db

.\scripts\start_app.ps1   # Windows
./scripts/start_app.sh    # Unix
```

Open `http://localhost:5001`.

## Usage

Upload your resume or pick one of the 9 built-in personas (one per industry). Gideon detects your domain and sets up the search queries. From there hit Scrape, Analyze, and Generate in order, or just tell Gideon what you want in the chat.

For interview prep, say something like "show me interview questions for the Stripe job" or "start a mock interview" and it handles the rest.

## Supported domains

Software Engineering, AI/ML, Product Management, Marketing, Data Analytics, Design, Finance, Sales, Operations

## Environment variables

`NVIDIA_API_KEY` is the only required variable. It powers all AI tasks including resume rewriting, PDF parsing, and interview coaching.

## License

MIT
