# Resume Auto-Tailor App - Complete Development Plan for Cursor AI

## Project Overview
Build a Python application that automatically scrapes job postings from the internet, analyzes job requirements, and generates customized resumes tailored to each job description.

---

## Phase 1: Core Job Scraping Module

### Objective
Create a web scraper that extracts job postings from job boards.

### Technical Requirements
- Use Python with BeautifulSoup4 and Selenium
- Target websites: LinkedIn, Indeed, and/or Glassdoor
- Handle dynamic content loading (JavaScript-rendered pages)
- Implement rate limiting and respectful scraping practices
- Add error handling for connection issues and page structure changes

### Data to Extract Per Job
- Job title
- Company name
- Location
- Job description (full text)
- Required skills/qualifications
- Preferred qualifications
- Salary range (if available)
- Application URL
- Date posted

### Deliverables
```
scraper/
├── __init__.py
├── base_scraper.py          # Abstract base class for scrapers
├── linkedin_scraper.py       # LinkedIn-specific implementation
├── indeed_scraper.py         # Indeed-specific implementation
├── utils.py                  # Helper functions (retry logic, delays)
└── config.py                 # Scraping configs (delays, user agents)
```

### Implementation Notes
- Use Selenium for JavaScript-heavy sites (LinkedIn)
- Use BeautifulSoup for static sites (Indeed)
- Implement random delays between requests (2-5 seconds)
- Rotate user agents to avoid detection
- Store raw HTML temporarily for debugging

---

## Phase 2: Job Data Storage & Management

### Objective
Store scraped jobs in a structured database for analysis and tracking.

### Technical Requirements
- Use SQLite for local development (easy to upgrade to PostgreSQL later)
- Use SQLAlchemy ORM for database operations
- Implement data validation and deduplication

### Database Schema
```sql
-- Jobs Table
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_title TEXT NOT NULL,
    company_name TEXT NOT NULL,
    location TEXT,
    job_description TEXT NOT NULL,
    required_skills TEXT,  -- JSON array
    preferred_skills TEXT, -- JSON array
    salary_range TEXT,
    application_url TEXT UNIQUE,
    date_posted DATE,
    date_scraped TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'new'  -- new, analyzed, applied
);

-- Master Resume Table
CREATE TABLE master_resumes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    content TEXT NOT NULL,  -- JSON structure of resume sections
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_active BOOLEAN DEFAULT TRUE
);

-- Tailored Resumes Table
CREATE TABLE tailored_resumes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER REFERENCES jobs(id),
    master_resume_id INTEGER REFERENCES master_resumes(id),
    tailored_content TEXT NOT NULL,  -- JSON structure
    match_score FLOAT,
    generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    pdf_path TEXT
);

-- Applications Tracking Table
CREATE TABLE applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER REFERENCES jobs(id),
    tailored_resume_id INTEGER REFERENCES tailored_resumes(id),
    application_date DATE,
    status TEXT,  -- applied, interviewing, rejected, accepted
    notes TEXT
);
```

### Deliverables
```
database/
├── __init__.py
├── models.py                 # SQLAlchemy model definitions
├── database.py               # Database connection and session management
└── migrations/               # Database migration scripts
```

---

## Phase 3: Job Analysis & Keyword Extraction

### Objective
Analyze job descriptions to extract key requirements, skills, and keywords for resume matching.

### Technical Requirements
- Use spaCy or NLTK for natural language processing
- Implement keyword extraction algorithms
- Identify technical skills, soft skills, and requirements
- Calculate keyword frequency and importance

### Analysis Components
1. **Skill Extraction**: Identify technical skills (Python, AWS, SQL, etc.)
2. **Qualification Parsing**: Extract years of experience, education requirements, certifications
3. **Action Verb Identification**: Find important verbs (managed, developed, led)
4. **Industry Terms**: Recognize domain-specific terminology

### Deliverables
```
analyzer/
├── __init__.py
├── keyword_extractor.py      # Extract keywords from job descriptions
├── skill_matcher.py          # Match skills between job and resume
├── requirement_parser.py     # Parse structured requirements
└── scoring.py                # Calculate match scores
```

### Matching Algorithm
```python
def calculate_match_score(job_keywords, resume_keywords):
    # Weight different keyword types
    # - Required skills: 40%
    # - Preferred skills: 30%
    # - Action verbs: 20%
    # - Industry terms: 10%
    # Return match score (0-100)
```

---

## Phase 4: Resume Modification Engine (UPDATED FOR GEMINI)

### Objective
Automatically modify resume content to align with job requirements while maintaining truthfulness using Google's Gemini API.

### Technical Requirements
- Use Google Gemini API (free tier with gemini-1.5-flash model)
- Implement template system for different resume formats
- Maintain factual accuracy (only rephrase, don't fabricate)
- Preserve original achievements while emphasizing relevant ones
- Handle API rate limits and errors gracefully

### API Configuration
- **Model**: gemini-1.5-flash (fast and free)
- **API Key**: Store in .env as GEMINI_API_KEY
- **Rate Limits**: 60 requests per minute (free tier)
- **Package**: google-generativeai

### Modification Strategies
1. **Bullet Point Rewriting**: Rephrase achievements to include job keywords
2. **Skills Section Reordering**: Prioritize relevant skills
3. **Summary Tailoring**: Customize professional summary for each job
4. **Project Highlighting**: Emphasize relevant projects
5. **Keyword Injection**: Naturally incorporate missing keywords where appropriate

### Gemini Integration Pattern
```python
import google.generativeai as genai
import os
from typing import List, Dict

class GeminiRewriter:
    """
    AI-powered resume content rewriter using Google Gemini API.
    
    Handles intelligent rephrasing of resume content to match job requirements
    while maintaining truthfulness and professional quality.
    """
    
    def __init__(self):
        """Initialize Gemini API with configuration from environment."""
        api_key = os.getenv('GEMINI_API_KEY')
        if not api_key:
            raise ValueError("GEMINI_API_KEY not found in environment variables")
        
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel('gemini-1.5-flash')
        
    def rewrite_bullet_point(
        self, 
        original_bullet: str, 
        job_keywords: List[str], 
        job_context: str
    ) -> str:
        """
        Rewrite a single resume bullet point to align with job requirements.
        
        Args:
            original_bullet: Original achievement text
            job_keywords: List of relevant keywords from job description
            job_context: Brief context (job title, company, key requirements)
            
        Returns:
            Rewritten bullet point optimized for the job
            
        Example:
            original: "Built web applications using Python"
            keywords: ["Django", "REST API", "PostgreSQL"]
            context: "Senior Python Developer at Tech Corp"
            output: "Developed Django web applications with REST APIs and PostgreSQL"
        """
        prompt = f"""You are an expert resume writer. Rewrite the following achievement to better match a job opportunity.

Original achievement: {original_bullet}

Job context: {job_context}
Relevant keywords to incorporate: {', '.join(job_keywords)}

Rules:
1. Keep it truthful - only rephrase, never fabricate experience
2. Naturally incorporate 2-3 relevant keywords from the list
3. Maintain any quantifiable results (numbers, percentages, metrics)
4. Use strong action verbs (developed, implemented, led, optimized)
5. Keep it concise: maximum 20 words
6. Make it ATS-friendly (clear, keyword-rich, no fancy formatting)

Return ONLY the rewritten bullet point, nothing else. No explanations or preamble."""

        try:
            response = self.model.generate_content(prompt)
            rewritten = response.text.strip()
            
            # Remove any markdown formatting or extra quotes
            rewritten = rewritten.replace('*', '').replace('**', '').strip('"').strip("'")
            
            return rewritten
            
        except Exception as e:
            # Log error and return original if API fails
            print(f"Error rewriting bullet point: {e}")
            return original_bullet
    
    def generate_professional_summary(
        self,
        original_summary: str,
        job_title: str,
        job_keywords: List[str],
        years_experience: int
    ) -> str:
        """
        Generate a tailored professional summary for the resume.
        
        Args:
            original_summary: Original professional summary
            job_title: Target job title
            job_keywords: Key skills and requirements from job
            years_experience: Years of relevant experience
            
        Returns:
            Tailored professional summary (3-4 sentences)
        """
        prompt = f"""You are an expert resume writer. Create a compelling professional summary.

Original summary: {original_summary}

Target job: {job_title}
Years of experience: {years_experience}
Key skills to highlight: {', '.join(job_keywords[:10])}

Rules:
1. Keep it truthful - base it on the original summary
2. Incorporate 5-7 relevant keywords naturally
3. Emphasize experience level and expertise
4. Make it compelling and professional
5. Length: 3-4 sentences (60-80 words)
6. Use third-person or first-person perspective consistently with original

Return ONLY the professional summary, nothing else."""

        try:
            response = self.model.generate_content(prompt)
            summary = response.text.strip()
            return summary.replace('*', '').replace('**', '')
            
        except Exception as e:
            print(f"Error generating summary: {e}")
            return original_summary
    
    def suggest_skills_reorder(
        self,
        current_skills: List[str],
        job_keywords: List[str]
    ) -> List[str]:
        """
        Reorder skills list to prioritize job-relevant skills.
        
        Args:
            current_skills: Current skills list from resume
            job_keywords: Keywords from job description
            
        Returns:
            Reordered skills list with job-relevant skills first
        """
        # Normalize for comparison
        job_keywords_lower = [kw.lower() for kw in job_keywords]
        
        # Separate matching and non-matching skills
        matching_skills = []
        other_skills = []
        
        for skill in current_skills:
            if skill.lower() in job_keywords_lower:
                matching_skills.append(skill)
            else:
                other_skills.append(skill)
        
        # Return matching skills first, then others
        return matching_skills + other_skills
```

### AI Prompting Strategy for Different Resume Sections
```python
def tailor_work_experience(
    work_experience: Dict,
    job_description: str,
    job_keywords: List[str]
) -> Dict:
    """
    Tailor an entire work experience entry.
    
    Args:
        work_experience: Dict with 'title', 'company', 'dates', 'bullets'
        job_description: Full job description text
        job_keywords: Extracted keywords
        
    Returns:
        Modified work experience with tailored bullets
    """
    rewriter = GeminiRewriter()
    job_context = f"Job: {job_description[:200]}..."
    
    tailored_bullets = []
    for bullet in work_experience['bullets']:
        # Select most relevant keywords for this bullet
        relevant_keywords = select_relevant_keywords(bullet, job_keywords)
        
        # Rewrite bullet
        new_bullet = rewriter.rewrite_bullet_point(
            original_bullet=bullet,
            job_keywords=relevant_keywords,
            job_context=job_context
        )
        tailored_bullets.append(new_bullet)
    
    return {
        **work_experience,
        'bullets': tailored_bullets
    }
```

### Error Handling & Rate Limiting
```python
import time
from functools import wraps

def rate_limit(calls_per_minute=60):
    """Decorator to enforce rate limiting for Gemini API calls."""
    min_interval = 60.0 / calls_per_minute
    last_called = [0.0]
    
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            elapsed = time.time() - last_called[0]
            left_to_wait = min_interval - elapsed
            
            if left_to_wait > 0:
                time.sleep(left_to_wait)
            
            ret = func(*args, **kwargs)
            last_called[0] = time.time()
            return ret
        
        return wrapper
    return decorator

class GeminiRewriter:
    @rate_limit(calls_per_minute=55)  # Slightly under limit for safety
    def rewrite_bullet_point(self, original_bullet, job_keywords, job_context):
        # ... implementation from above
```

### Deliverables
```
resume_engine/
├── __init__.py
├── modifier.py               # Core modification logic
├── gemini_rewriter.py        # Gemini API integration (UPDATED)
├── templates.py              # Resume structure templates
├── validator.py              # Ensure modifications are truthful
└── rate_limiter.py           # API rate limiting utilities
```

### Configuration for Gemini (.env)
```
# Google Gemini API Configuration
GEMINI_API_KEY=your_gemini_api_key_here
```

### Configuration (config.yaml) - AI Section
```yaml
ai:
  provider: gemini
  model: gemini-1.5-flash
  api_key: ${GEMINI_API_KEY}
  max_tokens: 1000
  temperature: 0.7
  rate_limit_rpm: 55  # Requests per minute (under 60 limit)
  retry_attempts: 3
  retry_delay: 2  # seconds
```

### Testing Gemini Integration
```python
# tests/test_gemini_rewriter.py
import os
import pytest
from resume_engine.gemini_rewriter import GeminiRewriter

def test_gemini_api_connection():
    """Test that Gemini API is properly configured."""
    assert os.getenv('GEMINI_API_KEY'), "GEMINI_API_KEY not set"
    
    rewriter = GeminiRewriter()
    assert rewriter.model is not None

def test_bullet_point_rewriting():
    """Test that bullet points are rewritten appropriately."""
    rewriter = GeminiRewriter()
    
    original = "Built web applications using Python"
    keywords = ["Django", "REST API", "PostgreSQL"]
    context = "Senior Python Developer position"
    
    result = rewriter.rewrite_bullet_point(original, keywords, context)
    
    # Check that result is different but related
    assert result != original
    assert len(result.split()) <= 25  # Reasonable length
    assert any(kw.lower() in result.lower() for kw in keywords)  # Has keyword

def test_rate_limiting():
    """Test that rate limiting prevents excessive API calls."""
    rewriter = GeminiRewriter()
    
    start_time = time.time()
    
    # Make 3 calls
    for i in range(3):
        rewriter.rewrite_bullet_point(
            "Test bullet",
            ["Python"],
            "Test job"
        )
    
    elapsed = time.time() - start_time
    
    # Should take at least 2 seconds for 3 calls (rate limited)
    assert elapsed >= 2.0
```

### Cost & Rate Limits (Free Tier)
- **Requests per minute**: 60 (we use 55 to be safe)
- **Requests per day**: 1,500
- **Monthly cost**: $0 (completely free)
- **Context window**: 1M tokens input, 8K tokens output

### Best Practices for Gemini Usage
1. **Batch Processing**: Group multiple bullet points when possible
2. **Caching**: Cache common rewrites to reduce API calls
3. **Fallback**: Always have original content as fallback
4. **Validation**: Check output quality and revert if nonsensical
5. **Logging**: Log all API calls for debugging

---

## Phase 5: PDF Generation

### Objective
Generate professional PDF resumes from modified content.

### Technical Requirements
- Use ReportLab or python-docx + docx2pdf
- Support multiple resume templates/styles
- Ensure ATS (Applicant Tracking System) compatibility
- Generate clean, professional formatting

### Templates to Support
1. **Classic**: Traditional format with clear sections
2. **Modern**: Two-column layout with icons
3. **ATS-Optimized**: Simple, parser-friendly format

### Deliverables
```
pdf_generator/
├── __init__.py
├── generator.py              # Main PDF generation logic
├── templates/
│   ├── classic.py
│   ├── modern.py
│   └── ats_optimized.py
└── styles.py                 # Font, color, spacing definitions
```

---

## Phase 6: Automation & Scheduling

### Objective
Automatically run scraping and resume generation on a schedule.

### Technical Requirements
- Use APScheduler or schedule library for task scheduling
- Implement job queue for processing
- Add logging and monitoring
- Handle failures gracefully

### Scheduled Tasks
1. **Daily Job Scraping**: Run scraper every morning at 9 AM
2. **Automatic Analysis**: Analyze new jobs immediately after scraping
3. **Resume Generation**: Generate tailored resumes for high-match jobs
4. **Cleanup**: Archive old jobs after 30 days

### Deliverables
```
scheduler/
├── __init__.py
├── tasks.py                  # Scheduled task definitions
├── scheduler.py              # Scheduler configuration
└── notifications.py          # Email/SMS notifications for new matches
```

---

## Phase 7: Command-Line Interface (CLI)

### Objective
Provide user-friendly command-line interface for all operations.

### Technical Requirements
- Use Click or argparse for CLI framework
- Provide interactive prompts where needed
- Display progress bars and status updates
- Support configuration via config file

### CLI Commands
```bash
# Scrape jobs
python main.py scrape --source linkedin --keywords "python developer" --location "San Francisco"

# Upload master resume
python main.py upload-resume --file resume.pdf

# Analyze jobs
python main.py analyze --job-id 123

# Generate tailored resume
python main.py generate --job-id 123 --template classic

# List jobs
python main.py list-jobs --status new --min-match-score 80

# Export resume
python main.py export --resume-id 456 --output tailored_resume.pdf

# Start scheduler
python main.py schedule --interval daily
```

### Deliverables
```
cli/
├── __init__.py
├── commands.py               # CLI command definitions
└── interface.py              # User interaction helpers
```

---

## Phase 8: Optional Web Interface

### Objective
Create a simple web dashboard for visual job and resume management.

### Technical Requirements
- Use Flask or FastAPI for backend
- Simple HTML/CSS/JavaScript frontend (or React for advanced UI)
- Display jobs in searchable table
- Show match scores and tailored resumes
- Allow manual editing before PDF generation

### Key Pages
1. **Dashboard**: Overview of jobs, applications, match scores
2. **Jobs List**: Searchable/filterable table of scraped jobs
3. **Resume Builder**: Upload and edit master resume
4. **Tailored Resumes**: View generated resumes, download PDFs
5. **Applications Tracker**: Track application status

### Deliverables
```
web/
├── app.py                    # Flask/FastAPI application
├── routes/
│   ├── jobs.py
│   ├── resumes.py
│   └── dashboard.py
├── templates/                # HTML templates
└── static/                   # CSS, JavaScript, images
```

---

## Project Structure (Final)
```
resume-auto-tailor/
├── README.md
├── requirements.txt
├── .env.example
├── config.yaml
├── main.py                   # Entry point
├── scraper/                  # Phase 1
├── database/                 # Phase 2
├── analyzer/                 # Phase 3
├── resume_engine/            # Phase 4 (GEMINI)
├── pdf_generator/            # Phase 5
├── scheduler/                # Phase 6
├── cli/                      # Phase 7
├── web/                      # Phase 8 (optional)
├── tests/                    # Unit tests
├── data/
│   ├── jobs/                 # Scraped job data
│   ├── resumes/              # Master resumes
│   └── output/               # Generated PDFs
└── logs/                     # Application logs
```

---

## Dependencies (requirements.txt) - UPDATED FOR GEMINI
```txt
# Web Scraping
beautifulsoup4==4.12.2
selenium==4.15.0
requests==2.31.0
webdriver-manager==4.0.1

# Database
sqlalchemy==2.0.23
alembic==1.12.1

# NLP & Analysis
spacy==3.7.2
nltk==3.8.1
scikit-learn==1.3.2

# AI API - GEMINI (FREE)
google-generativeai==0.3.2

# PDF Generation
reportlab==4.0.7
python-docx==1.1.0
docx2pdf==0.1.8

# Scheduling
apscheduler==3.10.4
schedule==1.2.0

# CLI
click==8.1.7
rich==13.7.0  # For pretty terminal output

# Web Framework (Optional)
flask==3.0.0
fastapi==0.104.1
uvicorn==0.24.0

# Utilities
python-dotenv==1.0.0
pyyaml==6.0.1
pydantic==2.5.0
```

---

## Configuration (config.yaml) - UPDATED FOR GEMINI
```yaml
scraping:
  delay_min: 2  # seconds
  delay_max: 5
  max_retries: 3
  timeout: 30
  user_agents:
    - "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    - "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    - "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0"

database:
  type: sqlite
  path: data/jobs.db

ai:
  provider: gemini
  model: gemini-1.5-flash
  api_key: ${GEMINI_API_KEY}
  max_tokens: 1000
  temperature: 0.7
  rate_limit_rpm: 55
  retry_attempts: 3
  retry_delay: 2

resume:
  default_template: classic
  max_bullet_points: 8
  include_summary: true

scheduler:
  scrape_time: "09:00"
  scrape_frequency: daily
  auto_generate_threshold: 75  # Only generate for jobs with 75+ match score

notifications:
  email_enabled: false
  smtp_server: smtp.gmail.com
  smtp_port: 587
```

---

## Environment Variables (.env.example) - UPDATED FOR GEMINI
```
# Google Gemini API (get from https://makersuite.google.com/app/apikey)
GEMINI_API_KEY=your_gemini_api_key_here

# Database
DATABASE_PATH=data/jobs.db

# Notification Settings (optional)
EMAIL_ADDRESS=
EMAIL_PASSWORD=
NOTIFY_EMAIL=
```

---

## Development Phases (Recommended Build Order)

### Week 1: Foundation
- Set up project structure
- Implement database models (Phase 2)
- Build basic CLI (Phase 7)
- Create one simple scraper (Phase 1)

### Week 2: Core Functionality
- Complete job scraping for multiple sites (Phase 1)
- Implement keyword extraction (Phase 3)
- Build skill matching algorithm (Phase 3)

### Week 3: Resume Engine with Gemini
- Set up Gemini API integration (Phase 4)
- Build resume modification logic (Phase 4)
- Test with sample resumes
- Implement rate limiting and error handling

### Week 4: Output & Automation
- Implement PDF generation (Phase 5)
- Set up scheduling system (Phase 6)
- Add error handling and logging

### Week 5: Polish & Optional Features
- Build web interface (Phase 8 - optional)
- Add testing
- Improve error handling
- Documentation

---

## Critical Implementation Notes

### 1. Legal & Ethical Considerations
- **Respect robots.txt**: Check each site's scraping policy
- **Rate Limiting**: Don't overwhelm job sites with requests
- **Terms of Service**: Review LinkedIn/Indeed TOS before scraping
- **Resume Accuracy**: Never fabricate experience or skills - only rephrase

### 2. API Keys & Security (GEMINI SPECIFIC)
- Get free Gemini API key from: https://makersuite.google.com/app/apikey
- Store API key in `.env` file (never commit to git)
- Use environment variables for sensitive data
- Add `.env` to `.gitignore`
- No cost - completely free tier

### 3. Error Handling
- Gracefully handle scraping failures (sites change HTML structure)
- Handle Gemini API errors (rate limits, timeouts, invalid responses)
- Log all errors with timestamps
- Implement retry logic with exponential backoff
- Notify user of critical failures
- Always have fallback to original content if AI fails

### 4. Testing Strategy
- Unit tests for keyword extraction
- Integration tests for database operations
- Mock Gemini API calls for testing (don't waste quota)
- Test PDF generation with various resume formats
- Validate AI output quality

### 5. Performance Optimization
- Cache scraped pages temporarily
- Batch database operations
- Use connection pooling
- Implement lazy loading for web interface
- Cache common Gemini API responses
- Rate limit Gemini calls to avoid hitting quotas

---

## Gemini API Setup Guide

### Getting Your API Key
1. Go to https://makersuite.google.com/app/apikey
2. Sign in with your Google account
3. Click "Create API Key"
4. Copy the key
5. Add to your `.env` file:
```
   GEMINI_API_KEY=your_actual_key_here
```

### Testing Your Gemini Setup
```python
# test_gemini.py
import google.generativeai as genai
import os
from dotenv import load_dotenv

load_dotenv()

genai.configure(api_key=os.getenv('GEMINI_API_KEY'))
model = genai.GenerativeModel('gemini-1.5-flash')

response = model.generate_content("Say hello!")
print(response.text)
# Should print: "Hello! 👋  How can I help you today?"
```

### Free Tier Limits (as of 2024)
- ✅ 60 requests per minute
- ✅ 1,500 requests per day
- ✅ No credit card required
- ✅ No cost ever (free tier)
- ✅ 1M token context window

---

## Cursor-Specific Prompting Tips

When working with Cursor AI to build this:

1. **Start Small**: Ask Cursor to build one phase at a time
2. **Be Specific**: Reference this plan and specific file paths
3. **Iterative Development**: Test each component before moving on
4. **Use Comments**: Ask Cursor to add detailed comments explaining logic
5. **For Gemini Integration**: Specifically mention "Use Google Gemini API with gemini-1.5-flash model"
6. **Example Prompt**: 
```
   Using the plan in PROJECT_PLAN.md, implement Phase 4 (Resume Modification Engine).
   
   IMPORTANT: Use Google Gemini API (google-generativeai package) with the gemini-1.5-flash model.
   
   Create resume_engine/gemini_rewriter.py with:
   - GeminiRewriter class
   - rewrite_bullet_point() method
   - generate_professional_summary() method
   - Rate limiting decorator (55 requests/minute)
   - Error handling with fallback to original content
   
   Follow the code patterns from PROJECT_PLAN.md Phase 4.
   Include comprehensive error handling, logging, and type hints.
```

---

## Success Metrics

Your app is working well when:
- ✅ Successfully scrapes 20+ jobs per search query
- ✅ Extracts keywords with 80%+ accuracy
- ✅ Gemini API successfully rewrites bullet points
- ✅ Generates resumes that increase match scores by 15+ points
- ✅ Creates ATS-compatible PDFs
- ✅ Runs automatically on schedule without crashes
- ✅ Provides clear feedback via CLI or web interface
- ✅ Stays within Gemini free tier limits

---

## Future Enhancements (v2.0)

- Cover letter generation using Gemini
- Interview preparation based on job description (Gemini-powered)
- Multi-language support
- Integration with LinkedIn Easy Apply
- Chrome extension for one-click tailoring
- Analytics: track which modifications lead to interviews
- A/B testing different resume versions
- Gemini fine-tuning for better resume writing (when available)
