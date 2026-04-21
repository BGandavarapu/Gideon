"""Analyze scheduler log file for task execution summary."""
import re
from collections import Counter
from pathlib import Path

log_path = Path("logs/app.log")
if not log_path.exists():
    print("logs/app.log not found")
    raise SystemExit(1)

logs = log_path.read_text(encoding="utf-8", errors="replace")

# Count task OK/error events from scheduler listener
task_events = re.findall(r"\[scheduler\] Job '(\w+)' (OK)", logs)
task_ok_counts = Counter(task for task, _ in task_events)

# Count task starts
task_starts = re.findall(r"\[(scrape_jobs|analyze_new_jobs|generate_resumes|cleanup_old_jobs|daily_report)\] (Starting|Generating)", logs)
task_start_counts = Counter(task for task, _ in task_starts)

# Count errors
errors = re.findall(r"ERROR.*", logs)
warnings = re.findall(r"WARNING.*", logs)

# Count 429 quota errors specifically
quota_errors = [e for e in errors if "429" in e or "RESOURCE_EXHAUSTED" in e or "quota" in e.lower()]

# Scrape stats
new_jobs = re.findall(r"(\d+) saved,", logs)
updated_jobs = re.findall(r"(\d+) updated,", logs)

print("=" * 50)
print("TASK EXECUTION SUMMARY")
print("=" * 50)
print(f"\nTask starts logged:")
for task, count in sorted(task_start_counts.items()):
    ok = task_ok_counts.get(task, 0)
    print(f"  {task:<25} started={count}  completed_ok={ok}")

print(f"\nScraping:")
total_saved = sum(int(x) for x in new_jobs)
total_updated = sum(int(x) for x in updated_jobs)
print(f"  Total new jobs saved:    {total_saved}")
print(f"  Total jobs updated:      {total_updated}")

print(f"\nLog counts:")
print(f"  Total ERROR lines:       {len(errors)}")
print(f"  Total WARNING lines:     {len(warnings)}")
print(f"  NIM 429 quota errors:    {len(quota_errors)}")

if quota_errors:
    print(f"\n  First quota error (truncated):")
    print(f"    {quota_errors[0][:120]}")
