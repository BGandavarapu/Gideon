"""Quick DB integrity check for end-to-end test."""
from database.database import get_db
from database.models import Job, TailoredResume, MasterResume

with get_db() as db:
    jobs = db.query(Job).all()
    masters = db.query(MasterResume).all()
    tailored_list = db.query(TailoredResume).all()

    print(f"Total Jobs:            {len(jobs)}")
    print(f"Total Master Resumes:  {len(masters)}")
    print(f"Total Tailored:        {len(tailored_list)}")
    print()

    all_ok = True
    for t in tailored_list:
        job = t.job
        master = t.master_resume
        job_label = job.job_title if job else "MISSING"
        master_label = master.name if master else "MISSING"
        pdf = t.pdf_path or "Not set"
        ok = "OK" if (job and master) else "!!"
        if not (job and master):
            all_ok = False
        print(f"[{ok}] TailoredResume #{t.id}")
        print(f"     Job:    {job_label}")
        print(f"     Master: {master_label}")
        print(f"     Score:  {t.match_score:.1f}%")
        print(f"     PDF:    {pdf}")
        print()

    print("Relationships: " + ("ALL OK" if all_ok else "SOME MISSING - check above"))
