"""Live end-to-end pipeline verification script.

Run against a live app (http://localhost:5001) to verify:
1. Resume detection
2. Tailoring with real NVIDIA NIM calls
3. Match score + score_breakdown accuracy

Usage:
    python scripts/verify_pipeline.py
"""

import json
import sys

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not installed. Run: pip install requests")
    sys.exit(1)

BASE = "http://localhost:5001"
PASS = "[PASS]"
FAIL = "[FAIL]"


def check(label, condition, detail=""):
    if condition:
        print(f"  {PASS} {label}")
    else:
        print(f"  {FAIL} {label}" + (f": {detail}" if detail else ""))
    return condition


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# 1. Health
# ---------------------------------------------------------------------------

section("1. App Health")
r = requests.get(f"{BASE}/api/health", timeout=5)
ok = check("App is running", r.status_code == 200)
if not ok:
    print("\nApp is not running. Start it with: .\\scripts\\start_app.ps1")
    sys.exit(1)
print(f"     pid={r.json()['pid']}, uptime={r.json()['uptime_seconds']:.0f}s")


# ---------------------------------------------------------------------------
# 2. Resume detection via upload
# ---------------------------------------------------------------------------

section("2. Resume Detection")

# 2a. Upload a clear text resume
txt_resume = b"""Alex Developer
alex@test.com | (555) 000-0001 | New York, NY

PROFESSIONAL SUMMARY
Backend engineer with 5 years Python and cloud experience.

TECHNICAL SKILLS
Python, Django, FastAPI, PostgreSQL, Docker, AWS, Redis, Git

WORK EXPERIENCE
Senior Backend Engineer - TechCo (2021-Present)
- Built REST APIs serving 1M daily requests
- Reduced latency by 35% with Redis caching
- Led AWS EKS migration

EDUCATION
B.S. Computer Science - MIT 2019
"""

r = requests.post(f"{BASE}/api/resume/upload",
                  files={"file": ("test_resume.txt", txt_resume, "text/plain")},
                  timeout=30)
ok = check("TXT resume accepted (200)", r.status_code == 200,
           f"status={r.status_code} body={r.text[:200]}")
if ok:
    data = r.json()
    check("skills_count > 0", data.get("skills_count", 0) > 0,
          f"skills_count={data.get('skills_count')}")
    check("domain detected", data.get("domain") is not None,
          f"domain={data.get('domain')}")
    check("name extracted", bool(data.get("name")),
          f"name={data.get('name')!r}")
    print(f"     name={data.get('name')!r}, domain={data.get('domain')}, "
          f"skills={data.get('skills_count')}")
    txt_resume_id = data.get("id")
else:
    txt_resume_id = None

# 2b. Upload an invoice — should get 422
invoice = b"""INVOICE #12345
Bill To: Client Corp
Payment Due: April 30, 2026
Total Amount Due: $540.00
Subtotal: $500.00
Tax Invoice: $40.00
"""
r = requests.post(f"{BASE}/api/resume/upload",
                  files={"file": ("invoice.txt", invoice, "text/plain")},
                  timeout=15)
check("Invoice rejected (422)", r.status_code == 422,
      f"status={r.status_code}")
if r.status_code == 422:
    data = r.json()
    check("error='not_a_resume'", data.get("error") == "not_a_resume",
          f"error={data.get('error')!r}")

# 2c. Unsupported file type → 400
r = requests.post(f"{BASE}/api/resume/upload",
                  files={"file": ("resume.exe", b"binary data", "application/octet-stream")},
                  timeout=5)
check("Unsupported extension rejected (400)", r.status_code == 400)


# ---------------------------------------------------------------------------
# 3. Switch to SE sample resume and find SE job
# ---------------------------------------------------------------------------

section("3. Sample Resume + Job Selection")

r = requests.patch(f"{BASE}/api/resume/mode",
                   json={"mode": "sample", "domain": "software_engineering"}, timeout=5)
check("Switch to SE sample (200)", r.status_code == 200)
if r.status_code == 200:
    active = r.json().get("active_resume")
    print(f"     Active resume: {active!r}")

r = requests.get(f"{BASE}/api/jobs?status=analyzed&limit=10", timeout=5)
check("Fetched analyzed jobs", r.status_code == 200)
jobs = r.json().get("jobs", [])
check("At least 1 analyzed job exists", len(jobs) > 0,
      "Run Analyze Jobs first to populate analyzed jobs")

if not jobs:
    print("\nNo analyzed jobs found. Run the Analyze Jobs task from the dashboard.")
    sys.exit(1)

# Pick a job without an existing tailored resume if possible
job = next((j for j in jobs if not j.get("has_resume")), jobs[0])
job_id = job["id"]
print(f"     Using job: id={job_id}, title={str(job.get('title', ''))[:50]!r}")


# ---------------------------------------------------------------------------
# 4. Generate tailored resume
# ---------------------------------------------------------------------------

section("4. Resume Generation + Tailoring")

r = requests.post(f"{BASE}/api/generate-resume",
                  json={"job_id": job_id}, timeout=120)

# Handle 409 resume mismatch — re-analyze with current resume then retry
if r.status_code == 409:
    print("  [INFO] Resume mismatch — re-analyzing job with current resume...")
    ra = requests.post(f"{BASE}/api/jobs/{job_id}/reanalyze", timeout=30)
    check("Re-analyze succeeded", ra.status_code == 200,
          f"reanalyze status={ra.status_code}")
    r = requests.post(f"{BASE}/api/generate-resume",
                      json={"job_id": job_id}, timeout=120)

ok = check("Generate succeeded (200)", r.status_code == 200,
           f"status={r.status_code} body={r.text[:300]}")
if ok:
    data = r.json()
    match_score = data.get("match_score", 0)
    api_calls = data.get("api_calls_used", 0)
    tailored = data.get("tailoring_applied", False)
    check("match_score is numeric", isinstance(match_score, (int, float)))
    check("match_score in [0, 100]", 0 <= match_score <= 100,
          f"match_score={match_score}")
    check("api_calls_used > 0 (NIM was called)", api_calls > 0,
          f"api_calls_used={api_calls} — resume may have no content to rewrite")
    check("tailoring_applied is True", tailored is True,
          f"tailoring_applied={tailored}")
    print(f"     score={match_score:.1f}, api_calls={api_calls}, tailored={tailored}")


# ---------------------------------------------------------------------------
# 5. Job detail: score_breakdown
# ---------------------------------------------------------------------------

section("5. Match Score Breakdown")

r = requests.get(f"{BASE}/api/jobs/{job_id}", timeout=10)
ok = check("Job detail fetched (200)", r.status_code == 200)
if ok:
    detail = r.json()
    bd = detail.get("score_breakdown")
    ok2 = check("score_breakdown is not null", bd is not None,
                "Breakdown may be null for old rows — re-generate the resume")
    if ok2:
        check("has required_skills", "required_skills" in bd)
        check("has preferred_skills", "preferred_skills" in bd)
        check("has experience", "experience" in bd)
        check("has education", "education" in bd)
        check("has bonus", "bonus" in bd)
        rs = bd.get("required_skills", {})
        check("required_skills.matched is int", isinstance(rs.get("matched"), int))
        check("required_skills.total is int", isinstance(rs.get("total"), int))
        check("required_skills.score in [0, 100]",
              0 <= rs.get("score", -1) <= 100,
              f"score={rs.get('score')}")
        print(f"     required: {rs.get('matched')}/{rs.get('total')} "
              f"({rs.get('score', 0):.1f}%)")
        pref = bd.get("preferred_skills", {})
        print(f"     preferred: {pref.get('matched')}/{pref.get('total')} "
              f"({pref.get('score', 0):.1f}%)")
        print(f"     experience: {bd.get('experience', {}).get('score', 0):.1f}")
        print(f"     education: {bd.get('education', {}).get('score', 0):.1f}")
        print(f"     bonus: {bd.get('bonus', 0):.1f}")
        if bd.get("weight_note"):
            print(f"     note: {bd['weight_note']}")


# ---------------------------------------------------------------------------
# 6. Tailored content has bullets
# ---------------------------------------------------------------------------

section("6. Tailored Resume Content")

if ok and detail.get("tailored_resume"):
    tr_id = detail["tailored_resume"]["id"]
    r = requests.get(f"{BASE}/api/resumes/tailored/{tr_id}", timeout=10)
    ok = check("Tailored resume fetched (200)", r.status_code == 200)
    if ok:
        content = r.json().get("content", {})
        skills = content.get("skills", [])
        work_exp = content.get("work_experience", [])
        bullets = [b for exp in work_exp for b in exp.get("bullets", [])]
        summary = content.get("professional_summary", "")
        check("skills list non-empty", len(skills) > 0,
              f"skills={skills}")
        check("work_experience entries exist", len(work_exp) > 0,
              "No work experience — resume may have empty content")
        check("bullets present", len(bullets) > 0,
              f"No bullets in {len(work_exp)} experience entries")
        check("professional_summary non-empty", bool(summary.strip()),
              "Empty summary")
        print(f"     skills={len(skills)}, experience={len(work_exp)}, "
              f"bullets={len(bullets)}")
        print(f"     summary: {summary[:80]!r}...")
else:
    print("  [SKIP] No tailored_resume in job detail")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

section("Verification Complete")
print("  All checks above should show [PASS].")
print("  [FAIL] items indicate bugs to investigate.")
