"""Test 6: Verify modification details - compare master vs tailored resume content."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from database.database import get_db
from database.models import TailoredResume, MasterResume

with get_db() as db:
    t = db.query(TailoredResume).order_by(TailoredResume.id.desc()).first()
    m = db.query(MasterResume).filter(MasterResume.id == t.master_resume_id).first()
    tc = t.tailored_content or {}
    mc = m.content or {}

    print("=== TAILORED RESUME CONTENT ===")
    print()
    print("PROFESSIONAL SUMMARY:")
    print("  BEFORE:", mc.get("professional_summary", "N/A"))
    print("  AFTER: ", tc.get("professional_summary", "N/A"))
    print()
    print("WORK EXPERIENCE BULLETS:")
    orig_bullets = mc.get("work_experience", [{}])[0].get("bullets", [])
    tail_bullets = tc.get("work_experience", [{}])[0].get("bullets", [])
    for i, (ob, tb) in enumerate(zip(orig_bullets, tail_bullets)):
        changed = "[CHANGED]" if ob != tb else "[same]"
        print(f"  Bullet {i+1} {changed}")
        if ob != tb:
            print(f"    BEFORE: {ob}")
            print(f"    AFTER:  {tb}")
        else:
            print(f"    {ob}")
    print()
    print("SKILLS:")
    print("  BEFORE:", ", ".join(mc.get("skills", [])[:10]))
    print("  AFTER: ", ", ".join(tc.get("skills", [])[:10]))
    print()
    print("PROJECTS:")
    for p in tc.get("projects", []):
        print(f"  - {p.get('name')}: {str(p.get('description', ''))[:70]}")
