from database.database import get_db
from database.models import Job, MasterResume
from analyzer.scoring import ScoringEngine
engine = ScoringEngine()
with get_db() as db:
    jobs = db.query(Job).filter(Job.status == 'analyzed').limit(10).all()
    resume = db.query(MasterResume).filter(MasterResume.is_active == True).first()
    print('Active resume:', resume.name)
    print('Skills:', len(resume.content['skills']))
    scores = []
    for job in jobs:
        result = engine.calculate_score(job, resume)
        scores.append(result.score)
        print(job.job_title[:40], '| Score:', round(result.score,1), '| Pref:', result.preferred_skills_total)
    print('Average:', round(sum(scores)/len(scores),1), '% (was 37.8%)')
    print('Range:', round(min(scores),1), '% -', round(max(scores),1), '%')
