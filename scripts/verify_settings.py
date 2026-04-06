"""
Comprehensive verification script for Settings automation toggle feature.
Covers Tests 4-13 using Flask test client (no live port needed).
"""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web.app import app as flask_app
import web.app as app_module
from web.settings_manager import SettingsManager

flask_app.config["TESTING"] = True

RESULTS = {}

def make_sm(tmp_dir):
    sm = SettingsManager()
    sm.SETTINGS_PATH = str(Path(tmp_dir) / "settings.json")
    return sm

def get_client_and_sm():
    """Fresh client with isolated settings for each test group."""
    tmp = tempfile.mkdtemp()
    sm = make_sm(tmp)
    orig_sm = app_module.settings_manager
    app_module.settings_manager = sm
    client = flask_app.test_client()
    return client, sm, orig_sm, tmp

def restore(orig_sm):
    app_module.settings_manager = orig_sm


# ── TEST 4: Settings API defaults + validation ─────────────────────────────

def test4():
    client, sm, orig, tmp = get_client_and_sm()
    fails = []
    try:
        # 4a: GET defaults
        r = client.get("/api/settings")
        d = json.loads(r.data)
        checks = [
            (r.status_code == 200,                          "4a HTTP 200"),
            (d["automation"]["scrape"]["mode"] == "manual", "4a scrape.mode=manual"),
            (d["automation"]["generate"]["mode"] == "manual","4a generate.mode=manual"),
            (d["automation"]["scrape"]["schedule"] == "09:00","4a scrape.schedule=09:00"),
            (d["automation"]["generate"]["schedule"] == "10:00","4a generate.schedule=10:00"),
            ("last_updated" in d,                           "4a last_updated present"),
        ]
        for ok, name in checks:
            if not ok: fails.append(name)

        # 4b: reject analyze
        r = client.patch("/api/settings/automation/analyze",
            data=json.dumps({"mode": "manual"}), content_type="application/json")
        if r.status_code != 400: fails.append("4b analyze not 400")
        if "analyze" not in json.loads(r.data).get("error","").lower() and \
           "toggleable" not in json.loads(r.data).get("error","").lower():
            fails.append("4b error msg missing 'analyze'/'toggleable'")

        # 4c: reject invalid mode
        r = client.patch("/api/settings/automation/scrape",
            data=json.dumps({"mode": "semi-auto"}), content_type="application/json")
        if r.status_code != 400: fails.append("4c semi-auto not 400")

        # 4d: reject invalid schedule
        for sched in ["9am", "25:00"]:
            r = client.patch("/api/settings/automation/scrape",
                data=json.dumps({"schedule": sched}), content_type="application/json")
            if r.status_code != 400: fails.append(f"4d {sched!r} not 400")

        # 4e: reject unknown task
        r = client.patch("/api/settings/automation/cleanup",
            data=json.dumps({"mode": "manual"}), content_type="application/json")
        if r.status_code != 400: fails.append("4e cleanup not 400")

    finally:
        restore(orig)
    return fails

# ── TEST 5: Switching to automatic ─────────────────────────────────────────

def test5():
    client, sm, orig, tmp = get_client_and_sm()
    fails = []
    try:
        # 5a: set scrape automatic
        r = client.patch("/api/settings/automation/scrape",
            data=json.dumps({"mode": "automatic"}), content_type="application/json")
        d = json.loads(r.data)
        if r.status_code != 200: fails.append("5a HTTP not 200")
        if d.get("status") != "saved": fails.append("5a status != saved")
        if d.get("settings", {}).get("mode") != "automatic": fails.append("5a mode != automatic")

        # 5b: persisted to disk
        disk = json.loads(Path(sm.SETTINGS_PATH).read_text())
        if disk["automation"]["scrape"]["mode"] != "automatic":
            fails.append("5b mode not persisted to disk")
        if not disk.get("last_updated"):
            fails.append("5b last_updated not set")

        # 5c: set schedule
        r = client.patch("/api/settings/automation/scrape",
            data=json.dumps({"schedule": "08:30"}), content_type="application/json")
        d = json.loads(r.data)
        if r.status_code != 200: fails.append("5c HTTP not 200")
        if d.get("settings", {}).get("schedule") != "08:30": fails.append("5c schedule not 08:30")

        # 5d: schedule persisted
        disk = json.loads(Path(sm.SETTINGS_PATH).read_text())
        if disk["automation"]["scrape"]["schedule"] != "08:30":
            fails.append("5d schedule not on disk")

        # 5e: generate mode+schedule together
        r = client.patch("/api/settings/automation/generate",
            data=json.dumps({"mode": "automatic", "schedule": "11:00"}),
            content_type="application/json")
        d = json.loads(r.data)
        if r.status_code != 200: fails.append("5e HTTP not 200")
        if d.get("settings", {}).get("mode") != "automatic": fails.append("5e mode != automatic")
        if d.get("settings", {}).get("schedule") != "11:00": fails.append("5e schedule != 11:00")

        # 5f: GET reflects all changes
        r = client.get("/api/settings")
        d = json.loads(r.data)
        if d["automation"]["scrape"]["mode"] != "automatic": fails.append("5f scrape.mode not automatic")
        if d["automation"]["scrape"]["schedule"] != "08:30": fails.append("5f scrape.schedule not 08:30")
        if d["automation"]["generate"]["mode"] != "automatic": fails.append("5f generate.mode not automatic")
        if d["automation"]["generate"]["schedule"] != "11:00": fails.append("5f generate.schedule not 11:00")

    finally:
        restore(orig)
    return fails

# ── TEST 6: Persistence across restart ─────────────────────────────────────

def test6():
    """Simulate restart by creating a brand-new SettingsManager pointing to
    the same file that was written in a previous instance."""
    tmp = tempfile.mkdtemp()
    sm1 = make_sm(tmp)

    orig = app_module.settings_manager
    app_module.settings_manager = sm1
    client = flask_app.test_client()

    fails = []
    try:
        # Write settings via first "instance"
        client.patch("/api/settings/automation/scrape",
            data=json.dumps({"mode": "automatic", "schedule": "08:30"}),
            content_type="application/json")
        client.patch("/api/settings/automation/generate",
            data=json.dumps({"mode": "automatic", "schedule": "11:00"}),
            content_type="application/json")
        lu_before = json.loads(Path(sm1.SETTINGS_PATH).read_text()).get("last_updated")

        # "Restart": new SettingsManager instance, same file
        sm2 = make_sm(tmp)
        app_module.settings_manager = sm2

        r = client.get("/api/settings")
        d = json.loads(r.data)
        if d["automation"]["scrape"]["mode"] != "automatic":  fails.append("6 scrape.mode lost")
        if d["automation"]["scrape"]["schedule"] != "08:30":  fails.append("6 scrape.schedule lost")
        if d["automation"]["generate"]["mode"] != "automatic":fails.append("6 generate.mode lost")
        if d["automation"]["generate"]["schedule"] != "11:00":fails.append("6 generate.schedule lost")
        # last_updated must not be reset on a read-only load
        lu_after = d.get("last_updated")
        if lu_after != lu_before: fails.append(f"6 last_updated changed on load: {lu_before!r} -> {lu_after!r}")

    finally:
        restore(orig)
    return fails

# ── TEST 7: Scheduler respects settings ─────────────────────────────────────

def test7():
    from scheduler.scheduler import SchedulerManager
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR

    fails = []

    def make_sched(scrape_mode, gen_mode, scrape_sched="08:30", gen_sched="11:00"):
        sm_mock = MagicMock()
        sm_mock.get_mode.side_effect = lambda t: scrape_mode if t == "scrape" else gen_mode
        sm_mock.get_schedule.side_effect = lambda t: scrape_sched if t == "scrape" else gen_sched
        mgr = SchedulerManager.__new__(SchedulerManager)
        mgr._scheduler = BackgroundScheduler()
        mgr._config = {"test_mode": False, "cleanup_days": 30,
                       "search_configs": [], "auto_generate_threshold": 35.0}
        mgr._is_running = False
        mgr._scheduler.add_listener(lambda e: None, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
        with patch("web.settings_manager.SettingsManager", return_value=sm_mock):
            mgr._register_jobs()
        return mgr

    # 7a: both automatic
    mgr = make_sched("automatic", "automatic")
    ids = [j.id for j in mgr._scheduler.get_jobs()]
    if "scrape_jobs" not in ids:    fails.append("7a scrape_jobs not registered when automatic")
    if "generate_resumes" not in ids: fails.append("7a generate_resumes not registered when automatic")
    if "cleanup_old_jobs" not in ids: fails.append("7a cleanup_old_jobs missing")
    if "daily_report" not in ids:    fails.append("7a daily_report missing")
    if len(ids) != 4:                fails.append(f"7a expected 4 jobs, got {len(ids)}: {ids}")

    # 7b: both manual
    mgr2 = make_sched("manual", "manual")
    ids2 = [j.id for j in mgr2._scheduler.get_jobs()]
    if "scrape_jobs" in ids2:    fails.append("7b scrape_jobs still registered in manual mode")
    if "generate_resumes" in ids2: fails.append("7b generate_resumes still registered in manual mode")
    if "cleanup_old_jobs" not in ids2: fails.append("7b cleanup_old_jobs missing")
    if "daily_report" not in ids2:    fails.append("7b daily_report missing")
    if len(ids2) != 2:               fails.append(f"7b expected 2 jobs, got {len(ids2)}: {ids2}")

    return fails

# ── TEST 8: Live reschedule ─────────────────────────────────────────────────

def test8():
    client, sm, orig, tmp = get_client_and_sm()
    fails = []
    try:
        # PATCH sets mode and logs — reschedule_task is best-effort when scheduler not running
        r = client.patch("/api/settings/automation/scrape",
            data=json.dumps({"mode": "automatic"}), content_type="application/json")
        if r.status_code != 200: fails.append("8a HTTP not 200 on set automatic")

        r = client.patch("/api/settings/automation/scrape",
            data=json.dumps({"mode": "manual"}), content_type="application/json")
        if r.status_code != 200: fails.append("8b HTTP not 200 on set manual")

        # reschedule_task with no scheduler running must not crash
        from scheduler.scheduler import SchedulerManager
        from apscheduler.schedulers.background import BackgroundScheduler
        mgr = SchedulerManager.__new__(SchedulerManager)
        mgr._scheduler = BackgroundScheduler()
        mgr._config = {}
        mgr._is_running = False
        try:
            mgr.reschedule_task("scrape")
        except Exception as e:
            fails.append(f"8 reschedule_task crashed when scheduler not running: {e}")

    finally:
        restore(orig)
    return fails

# ── TEST 9: Manual task execution in both modes ─────────────────────────────

def test9():
    client, sm, orig, tmp = get_client_and_sm()
    fails = []
    try:
        # 9a: manual trigger in manual mode
        r = client.post("/api/run/scrape")
        d = json.loads(r.data)
        if r.status_code != 200: fails.append("9a HTTP not 200")
        if d.get("status") != "started": fails.append("9a status != started")

        # 9b: manual override in automatic mode (scrape)
        client.patch("/api/settings/automation/scrape",
            data=json.dumps({"mode": "automatic"}), content_type="application/json")
        r = client.post("/api/run/scrape")
        d = json.loads(r.data)
        if r.status_code != 200: fails.append("9b HTTP not 200 (auto override)")
        if d.get("status") != "started": fails.append("9b status != started (auto override)")

        # 9c: manual override in automatic mode (generate)
        client.patch("/api/settings/automation/generate",
            data=json.dumps({"mode": "automatic"}), content_type="application/json")
        r = client.post("/api/run/generate")
        d = json.loads(r.data)
        if r.status_code != 200: fails.append("9c HTTP not 200 (gen auto override)")
        if d.get("status") != "started": fails.append("9c status != started (gen auto override)")

        # reset
        client.patch("/api/settings/automation/scrape",
            data=json.dumps({"mode": "manual"}), content_type="application/json")
        client.patch("/api/settings/automation/generate",
            data=json.dumps({"mode": "manual"}), content_type="application/json")

    finally:
        restore(orig)
    return fails

# ── TEST 10: Settings page UI ─────────────────────────────────────────────

def test10():
    client, sm, orig, tmp = get_client_and_sm()
    fails = []
    try:
        # 10a: settings page content
        r = client.get("/settings")
        html = r.data.decode("utf-8", errors="replace")
        checks = [
            ("Job Scraping" in html or "Scrape" in html, "10a Scrape present"),
            ("Resume Generation" in html or "Generate" in html, "10a Generate present"),
            ("Job Analysis" in html or "analyze" in html.lower(), "10a Analyze present"),
            ("Always Auto" in html, "10a Always Auto present"),
            ("manual" in html.lower(), "10a manual option present"),
            ("automatic" in html.lower(), "10a automatic option present"),
            ("fetch('/api/settings')" in html or 'fetch("/api/settings")' in html, "10a fetch /api/settings"),
            ("PATCH" in html, "10a PATCH present"),
            ("schedule" in html.lower(), "10a schedule input present"),
            ("Saved" in html or "saved" in html, "10a Saved flash present"),
            ("last_updated" in html or "Last updated" in html, "10a Last updated present"),
        ]
        for ok, name in checks:
            if not ok: fails.append(name)

        # 10b: settings nav link on dashboard
        mock_db = MagicMock()
        mock_db.__enter__ = lambda s: mock_db
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_q = MagicMock()
        mock_q.count.return_value = 0
        mock_q.filter.return_value = mock_q
        mock_q.order_by.return_value = mock_q
        mock_q.limit.return_value = mock_q
        mock_q.options.return_value = mock_q
        mock_q.all.return_value = []
        mock_db.query.return_value = mock_q
        with patch.object(app_module, "get_db", return_value=mock_db):
            r2 = client.get("/")
        dash_html = r2.data.decode("utf-8", errors="replace")
        if "/settings" not in dash_html: fails.append("10b /settings link missing from dashboard")

        # 10c: schedule show/hide logic
        if "display:none" not in html and "display: none" not in html:
            fails.append("10c schedule input show/hide logic missing (display:none)")

        # 10d: set automatic, confirm GET returns it
        client.patch("/api/settings/automation/scrape",
            data=json.dumps({"mode": "automatic"}), content_type="application/json")
        r3 = client.get("/api/settings")
        d = json.loads(r3.data)
        if d["automation"]["scrape"]["mode"] != "automatic":
            fails.append("10d API not reflecting automatic after PATCH")
        # reset
        client.patch("/api/settings/automation/scrape",
            data=json.dumps({"mode": "manual"}), content_type="application/json")

    finally:
        restore(orig)
    return fails

# ── TEST 11: Dashboard badge ──────────────────────────────────────────────

def test11():
    client, sm, orig, tmp = get_client_and_sm()
    fails = []

    def get_dash():
        mock_db = MagicMock()
        mock_db.__enter__ = lambda s: mock_db
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_q = MagicMock()
        mock_q.count.return_value = 0
        mock_q.filter.return_value = mock_q
        mock_q.order_by.return_value = mock_q
        mock_q.limit.return_value = mock_q
        mock_q.options.return_value = mock_q
        mock_q.all.return_value = []
        mock_db.query.return_value = mock_q
        with patch.object(app_module, "get_db", return_value=mock_db):
            r = client.get("/")
        return r.data.decode("utf-8", errors="replace")

    try:
        html = get_dash()
        if "badge-scrape" not in html: fails.append("11 badge-scrape element missing")
        if "badge-generate" not in html: fails.append("11 badge-generate element missing")
        if "refreshAutoBadges" not in html: fails.append("11 refreshAutoBadges function missing")
        if "30000" not in html: fails.append("11d 30s interval (30000) missing")
        if "/api/settings" not in html: fails.append("11 /api/settings fetch missing from dashboard")

    finally:
        restore(orig)
    return fails

# ── TEST 12: Corrupt file recovery ─────────────────────────────────────────

def test12():
    tmp = tempfile.mkdtemp()
    sm = make_sm(tmp)
    fails = []

    # Write corrupt JSON
    Path(sm.SETTINGS_PATH).write_text("{{invalid json{{", encoding="utf-8")

    result = sm.load()
    if result["automation"]["scrape"]["mode"] != "manual":
        fails.append("12a default not returned after corruption")
    if not Path(sm.SETTINGS_PATH).exists():
        fails.append("12a settings.json not recreated")

    # Verify file is now valid JSON
    try:
        json.loads(Path(sm.SETTINGS_PATH).read_text())
    except json.JSONDecodeError:
        fails.append("12a recreated file is still invalid JSON")

    # API should return 200 after corruption recovery
    orig = app_module.settings_manager
    app_module.settings_manager = sm
    client = flask_app.test_client()
    try:
        r = client.get("/api/settings")
        if r.status_code != 200: fails.append("12c Flask not 200 after corrupt file")
        d = json.loads(r.data)
        if d["automation"]["scrape"]["mode"] != "manual":
            fails.append("12c API not returning defaults after corrupt file")
    finally:
        restore(orig)

    return fails

# ── TEST 13: Partial PATCH ─────────────────────────────────────────────────

def test13():
    client, sm, orig, tmp = get_client_and_sm()
    fails = []
    try:
        # 13a: set only mode, schedule stays at default 09:00
        r = client.patch("/api/settings/automation/scrape",
            data=json.dumps({"mode": "automatic"}), content_type="application/json")
        d = json.loads(r.data)
        if d["settings"]["schedule"] != "09:00":
            fails.append(f"13a schedule changed when only mode was set: got {d['settings']['schedule']!r}")

        # 13b: set only schedule, mode stays at automatic
        r = client.patch("/api/settings/automation/scrape",
            data=json.dumps({"schedule": "07:45"}), content_type="application/json")
        d = json.loads(r.data)
        if d["settings"]["mode"] != "automatic":
            fails.append(f"13b mode changed when only schedule was set: got {d['settings']['mode']!r}")
        if d["settings"]["schedule"] != "07:45":
            fails.append(f"13b schedule not updated: got {d['settings']['schedule']!r}")

        # 13c: verify on disk
        disk = json.loads(Path(sm.SETTINGS_PATH).read_text())
        if disk["automation"]["scrape"]["mode"] != "automatic":
            fails.append("13c mode not automatic on disk")
        if disk["automation"]["scrape"]["schedule"] != "07:45":
            fails.append("13c schedule not 07:45 on disk")

    finally:
        restore(orig)
    return fails

# ── Test 1 checks needing file/code inspection ─────────────────────────────

def test1_settings_json_recreated():
    """Remove settings.json, call load(), confirm it is recreated with defaults."""
    tmp = tempfile.mkdtemp()
    sm = make_sm(tmp)
    # Ensure file doesn't exist
    p = Path(sm.SETTINGS_PATH)
    if p.exists(): p.unlink()
    data = sm.load()
    fails = []
    if not p.exists(): fails.append("1 settings.json not created on first load()")
    if data["automation"]["scrape"]["mode"] != "manual": fails.append("1 scrape default not manual")
    if data["automation"]["generate"]["mode"] != "manual": fails.append("1 generate default not manual")
    if data["automation"]["scrape"]["schedule"] != "09:00": fails.append("1 scrape schedule not 09:00")
    if data["automation"]["generate"]["schedule"] != "10:00": fails.append("1 generate schedule not 10:00")
    return fails

# ── Run everything ─────────────────────────────────────────────────────────

def run_all():
    tests = [
        ("TEST 1 (defaults/recreate)", test1_settings_json_recreated),
        ("TEST 4 (API defaults+validation)", test4),
        ("TEST 5 (switch to automatic)", test5),
        ("TEST 6 (persist across restart)", test6),
        ("TEST 7 (scheduler respects settings)", test7),
        ("TEST 8 (live reschedule)", test8),
        ("TEST 9 (manual trigger in both modes)", test9),
        ("TEST 10 (settings page UI)", test10),
        ("TEST 11 (dashboard badge)", test11),
        ("TEST 12 (corrupt recovery)", test12),
        ("TEST 13 (partial PATCH)", test13),
    ]

    all_pass = True
    for name, fn in tests:
        try:
            fails = fn()
            if fails:
                all_pass = False
                print(f"  FAIL {name}")
                for f in fails:
                    print(f"       ✗ {f}")
            else:
                print(f"  PASS {name}")
        except Exception as e:
            all_pass = False
            import traceback
            print(f"  ERROR {name}: {e}")
            traceback.print_exc()

    print()
    print("=" * 56)
    print("OVERALL:", "ALL PASS" if all_pass else "SOME FAILED")
    print("=" * 56)
    return all_pass

if __name__ == "__main__":
    ok = run_all()
    sys.exit(0 if ok else 1)
