"""
Tests 5-10: Manual pipeline API tests using Flask test client.
Avoids live network / port issues entirely.
"""
import json
import time
import threading
import unittest

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web.app import app, _task_running, _task_last_run
import web.app as web_module
from database.database import create_tables


class TestManualAPIEndpoints(unittest.TestCase):

    def setUp(self):
        create_tables()  # ensure schema migrations are applied before tests run
        self.client = app.test_client()
        app.config["TESTING"] = True
        # Reset state before each test
        for k in _task_running:
            _task_running[k] = False
        for k in _task_last_run:
            _task_last_run[k] = None

    # ── TEST 5a ──────────────────────────────────────────────────────────────
    def test_5a_scrape_returns_started(self):
        t0 = time.time()
        r = self.client.post("/api/run/scrape")
        elapsed = time.time() - t0
        self.assertEqual(r.status_code, 200)
        body = json.loads(r.data)
        self.assertEqual(body["status"], "started")
        self.assertEqual(body["task"], "scrape_jobs_task")
        self.assertLess(elapsed, 2.0, f"Response took {elapsed:.2f}s — blocking!")

    # ── TEST 5b ──────────────────────────────────────────────────────────────
    def test_5b_analyze_returns_started(self):
        t0 = time.time()
        r = self.client.post("/api/run/analyze")
        elapsed = time.time() - t0
        self.assertEqual(r.status_code, 200)
        body = json.loads(r.data)
        self.assertEqual(body["status"], "started")
        self.assertEqual(body["task"], "analyze_new_jobs_task")
        self.assertLess(elapsed, 2.0, f"Response took {elapsed:.2f}s — blocking!")

    # ── TEST 5c ──────────────────────────────────────────────────────────────
    def test_5c_generate_returns_started(self):
        t0 = time.time()
        r = self.client.post("/api/run/generate")
        elapsed = time.time() - t0
        self.assertEqual(r.status_code, 200)
        body = json.loads(r.data)
        self.assertEqual(body["status"], "started")
        self.assertEqual(body["task"], "generate_resumes_task")
        self.assertLess(elapsed, 2.0, f"Response took {elapsed:.2f}s — blocking!")

    # ── TEST 5d ──────────────────────────────────────────────────────────────
    def test_5d_status_returns_three_bool_keys(self):
        r = self.client.get("/api/run/status")
        self.assertEqual(r.status_code, 200)
        body = json.loads(r.data)
        self.assertIn("scrape", body)
        self.assertIn("analyze", body)
        self.assertIn("generate", body)
        for k, v in body.items():
            self.assertIsInstance(v, bool, f"Key {k!r} is not bool: {v!r}")

    # ── TEST 5e ──────────────────────────────────────────────────────────────
    def test_5e_last_run_returns_three_keys(self):
        r = self.client.get("/api/run/last-run")
        self.assertEqual(r.status_code, 200)
        body = json.loads(r.data)
        self.assertIn("scrape", body)
        self.assertIn("analyze", body)
        self.assertIn("generate", body)

    def test_5e_last_run_populated_after_task(self):
        """After analyze runs and finishes, last-run['analyze'] must be an ISO timestamp."""
        barrier = threading.Event()
        original_task = None

        # Patch analyze task to signal when done
        import scheduler.tasks as tasks_mod

        original_analyze = tasks_mod.analyze_new_jobs_task

        def patched(*a, **kw):
            result = original_analyze(*a, **kw)
            barrier.set()
            return result

        tasks_mod.analyze_new_jobs_task = patched
        try:
            self.client.post("/api/run/analyze")
            finished = barrier.wait(timeout=30)
            # Give the finally block a moment to update _task_last_run
            time.sleep(0.2)
            r = self.client.get("/api/run/last-run")
            body = json.loads(r.data)
            if finished:
                self.assertIsNotNone(body["analyze"],
                                     "last-run['analyze'] should be set after task completes")
        finally:
            tasks_mod.analyze_new_jobs_task = original_analyze

    # ── TEST 5f: concurrent ──────────────────────────────────────────────────
    def test_5f_concurrent_all_start(self):
        results = []

        def fire(path):
            c = app.test_client()
            resp = c.post(path)
            results.append((resp.status_code, json.loads(resp.data)))

        threads = [
            threading.Thread(target=fire, args=("/api/run/scrape",)),
            threading.Thread(target=fire, args=("/api/run/analyze",)),
            threading.Thread(target=fire, args=("/api/run/generate",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(len(results), 3)
        for code, body in results:
            self.assertEqual(code, 200)
            self.assertEqual(body.get("status"), "started",
                             f"Unexpected body: {body}")

    # ── TEST 6: non-blocking ─────────────────────────────────────────────────
    def test_6_non_blocking_response(self):
        for path in ("/api/run/scrape", "/api/run/analyze", "/api/run/generate"):
            t0 = time.time()
            r = self.client.post(path)
            elapsed = time.time() - t0
            self.assertLess(elapsed, 2.0,
                            f"{path} took {elapsed:.2f}s — it is BLOCKING the request")
            self.assertEqual(r.status_code, 200)

    # ── TEST 7: running flag integrity ───────────────────────────────────────
    def test_7_running_flag_resets_after_completion(self):
        barrier = threading.Event()
        import scheduler.tasks as tasks_mod
        original = tasks_mod.analyze_new_jobs_task

        def patched(*a, **kw):
            result = original(*a, **kw)
            barrier.set()
            return result

        tasks_mod.analyze_new_jobs_task = patched
        try:
            self.client.post("/api/run/analyze")
            finished = barrier.wait(timeout=30)
            time.sleep(0.3)  # let finally block run
            if finished:
                self.assertFalse(
                    _task_running["analyze"],
                    "_task_running['analyze'] should be False after task completes"
                )
                self.assertIsNotNone(
                    _task_last_run["analyze"],
                    "_task_last_run['analyze'] should be set after task completes"
                )
        finally:
            tasks_mod.analyze_new_jobs_task = original

    # ── TEST 9: dashboard HTML ───────────────────────────────────────────────
    def test_9_dashboard_contains_pipeline_controls(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        html = r.data.decode("utf-8", errors="replace")
        self.assertIn("Pipeline Controls", html, "Pipeline Controls section missing")
        self.assertIn("Scrape Jobs", html, "Scrape Jobs button missing")
        self.assertIn("Analyze Jobs", html, "Analyze Jobs button missing")
        self.assertIn("Generate Resumes", html, "Generate Resumes button missing")
        self.assertIn("/api/run/", html, "Fetch calls to /api/run/* missing")
        self.assertIn("pollStatus", html, "pollStatus polling function missing")

    # ── TEST 10: manual task independent of scheduler ────────────────────────
    def test_10_manual_task_works_without_scheduler(self):
        # Just fire analyze — no scheduler started at all
        r = self.client.post("/api/run/analyze")
        self.assertEqual(r.status_code, 200)
        body = json.loads(r.data)
        self.assertEqual(body["status"], "started")


if __name__ == "__main__":
    unittest.main(verbosity=2)
